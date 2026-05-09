#!/usr/bin/env bash
# Interactive launcher for the three P2P WebRTC clients.
#
# Sources .env from this directory if present (P2P_*, STUN, TURN, etc.) so
# the Python and C++ clients pick up the same configuration as production.
#
# Usage:
#   ./run.sh             # interactive menu
#   ./run.sh py-offer    # python manual offerer
#   ./run.sh py-answer   # python manual answerer
#   ./run.sh ws-offer    # python WS-signaling offerer (uses .env, calls --peer)
#   ./run.sh ws-answer   # python WS-signaling answerer (uses .env)
#   ./run.sh build       # configure + build C++ client
#   ./run.sh cpp-offer
#   ./run.sh cpp-answer
#   ./run.sh html        # open index.html in default browser

set -euo pipefail
cd "$(dirname "$0")"

# ---- load .env ------------------------------------------------------------
if [[ -f .env ]]; then
    # parse KEY=VALUE lines, strip inline `# comment` and surrounding quotes,
    # without using `set -a` (which trips on inline comments).
    while IFS= read -r line; do
        # skip blanks and full-line comments
        [[ -z "${line// }" || "$line" =~ ^[[:space:]]*# ]] && continue
        # strip inline comment
        clean="${line%%#*}"
        clean="${clean%"${clean##*[![:space:]]}"}"   # rtrim
        [[ -z "$clean" || "$clean" != *=* ]] && continue
        key="${clean%%=*}"
        val="${clean#*=}"
        key="${key#"${key%%[![:space:]]*}"}"          # ltrim key
        val="${val#"${val%%[![:space:]]*}"}"          # ltrim val
        # strip surrounding quotes
        [[ "$val" =~ ^\".*\"$ ]] && val="${val:1:-1}"
        [[ "$val" =~ ^\'.*\'$ ]] && val="${val:1:-1}"
        export "$key=$val"
    done < .env
    echo ">> loaded .env (P2P_DEVICE_ID=${P2P_DEVICE_ID:-unset}, signaling=${P2P_SIGNALING_URL:-unset})"
fi

PY=${PYTHON:-./venv/bin/python}
[[ -x "$PY" ]] || PY=python

# Default STUN comes from .env (P2P_STUN_SERVERS), with override via STUN env var.
STUN=${STUN:-${P2P_STUN_SERVERS%%,*}}      # take the first comma-separated entry
STUN=${STUN:-stun:stun.l.google.com:19302}

# manual-mode TURN passthrough (only used by py-* and cpp-* modes)
EXTRA=()
if [[ -n "${P2P_TURN_URL:-}" ]]; then
    EXTRA+=(--turn "$P2P_TURN_URL")
    [[ -n "${P2P_TURN_USERNAME:-}"   ]] && EXTRA+=(--turn-user "$P2P_TURN_USERNAME")
    [[ -n "${P2P_TURN_CREDENTIAL:-}" ]] && EXTRA+=(--turn-pass "$P2P_TURN_CREDENTIAL")
fi

py_run() {
    local role=$1
    echo ">> $PY client.py $role --stun $STUN ${EXTRA[*]:-}"
    exec "$PY" client.py "$role" --stun "$STUN" "${EXTRA[@]}"
}

ws_run() {
    local role=$1            # ws-offer | ws-answer
    local peer_arg=()
    if [[ "$role" == "ws-offer" ]]; then
        local peer="${PEER:-}"
        if [[ -z "$peer" ]]; then
            read -rp "peer device_id to call: " peer
        fi
        peer_arg=(--peer "$peer")
    fi
    echo ">> $PY client.py $role ${peer_arg[*]:-}"
    exec "$PY" client.py "$role" "${peer_arg[@]}"
}

cpp_build() {
    if [[ ! -x build/p2p_client ]]; then
        echo ">> cmake -B build"
        cmake -B build
        echo ">> cmake --build build -j"
        cmake --build build -j
    else
        echo ">> p2p_client already built (delete build/ to rebuild)"
    fi
}

cpp_run() {
    cpp_build
    local role=$1
    echo ">> ./build/p2p_client $role --stun $STUN ${EXTRA[*]:-}"
    exec ./build/p2p_client "$role" --stun "$STUN" "${EXTRA[@]}"
}

open_html() {
    local f
    f="$(pwd)/index.html"
    echo ">> opening $f"
    if command -v xdg-open >/dev/null 2>&1; then xdg-open "$f"
    elif command -v open    >/dev/null 2>&1; then open "$f"
    else echo "open this in a browser manually: file://$f"
    fi
}

menu() {
    cat <<'EOF'

  P2P WebRTC client launcher
  --------------------------
  1) Python  — offer       (manual signaling, stdin/stdout)
  2) Python  — answer      (manual signaling, stdin/stdout)
  3) Python  — ws-offer    (WebSocket signaling via .env)
  4) Python  — ws-answer   (WebSocket signaling via .env)
  5) C++     — build       (cmake configure + build)
  6) C++     — offer
  7) C++     — answer
  8) HTML    — open index.html in browser
  q) quit

EOF
    read -rp "choice: " choice
    case "$choice" in
        1) py_run offer ;;
        2) py_run answer ;;
        3) ws_run ws-offer ;;
        4) ws_run ws-answer ;;
        5) cpp_build ;;
        6) cpp_run offer ;;
        7) cpp_run answer ;;
        8) open_html ;;
        q|Q|"") exit 0 ;;
        *) echo "unknown choice: $choice"; exit 1 ;;
    esac
}

case "${1:-}" in
    py-offer)    py_run offer ;;
    py-answer)   py_run answer ;;
    ws-offer)    ws_run ws-offer ;;
    ws-answer)   ws_run ws-answer ;;
    build)       cpp_build ;;
    cpp-offer)   cpp_run offer ;;
    cpp-answer)  cpp_run answer ;;
    html)        open_html ;;
    "")          menu ;;
    -h|--help)   sed -n '2,17p' "$0" ;;
    *)
        echo "unknown command: $1"
        sed -n '2,17p' "$0"
        exit 1
        ;;
esac
