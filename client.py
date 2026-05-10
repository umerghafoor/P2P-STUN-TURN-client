"""
P2P WebRTC client (Python / aiortc).

Two signaling modes:

  1) Manual (default): role=offer|answer
     Prints local SDP to stdout as one JSON line; reads remote SDP from stdin.
     Pairs with index.html / client.cpp.

  2) WebSocket signaling: role=ws-offer|ws-answer
     Connects to P2P_SIGNALING_URL, registers as P2P_DEVICE_ID using
     P2P_DEVICE_SECRET, and exchanges SDP/ICE via JSON messages.
     Loads .env automatically from the script directory if python-dotenv
     is not installed (a tiny shim parses simple KEY=VALUE lines).

Covers:
  - RTCPeerConnection setup with RTCConfiguration (STUN + optional TURN)
  - DataChannel creation (offerer) / handler (answerer)
  - ICE candidate event handler (aiortc gathers candidates as part of
    setLocalDescription, so we log them after the description is set)
  - Offer/Answer creation flow + SDP exchange
  - Async event loop for connection control
  - Connection state events + verbose logging

Run:
  pip install aiortc websockets
  python client.py offer
  python client.py answer
  python client.py ws-offer  --peer edge-device-2
  python client.py ws-answer
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from aiortc import (
    RTCConfiguration,
    RTCDataChannel,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("p2p")


# ---------------------------------------------------------------------------
# .env loader (no python-dotenv dependency: tiny KEY=VALUE parser).
# Comments after `#` are stripped. Quoted values are unquoted.
# ---------------------------------------------------------------------------
def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        # strip trailing inline comment
        if "#" in val:
            val = val.split("#", 1)[0]
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def build_config_from_env() -> RTCConfiguration:
    """Build RTCConfiguration from P2P_STUN_SERVERS / P2P_TURN_* env vars."""
    servers = []
    stun_csv = os.environ.get("P2P_STUN_SERVERS", "stun:stun.l.google.com:19302")
    for url in (u.strip() for u in stun_csv.split(",") if u.strip()):
        servers.append(RTCIceServer(urls=[url]))

    turn_url = os.environ.get("P2P_TURN_URL", "").strip()
    if turn_url:
        servers.append(
            RTCIceServer(
                urls=[turn_url],
                username=os.environ.get("P2P_TURN_USERNAME") or None,
                credential=os.environ.get("P2P_TURN_CREDENTIAL") or None,
            )
        )
    log.info("RTCConfiguration ICE servers: %s", [s.urls for s in servers])
    return RTCConfiguration(iceServers=servers)


def build_config(stun: str, turn: Optional[str], turn_user: Optional[str], turn_pass: Optional[str]) -> RTCConfiguration:
    servers = []
    if stun:
        servers.append(RTCIceServer(urls=[stun]))
    if turn:
        servers.append(RTCIceServer(urls=[turn], username=turn_user, credential=turn_pass))
    log.info("RTCConfiguration ICE servers: %s", [s.urls for s in servers])
    return RTCConfiguration(iceServers=servers)


def attach_pc_handlers(pc: RTCPeerConnection) -> None:
    @pc.on("signalingstatechange")
    async def _():
        log.info("[pc] signalingState -> %s", pc.signalingState)

    @pc.on("iceconnectionstatechange")
    async def _():
        log.info("[pc] iceConnectionState -> %s", pc.iceConnectionState)

    @pc.on("icegatheringstatechange")
    async def _():
        log.info("[pc] iceGatheringState -> %s", pc.iceGatheringState)

    @pc.on("connectionstatechange")
    async def _():
        log.info("[pc] connectionState -> %s", pc.connectionState)


def attach_dc_handlers(dc: RTCDataChannel, send_loop_task_holder: list) -> None:
    log.info("[dc] attached label=%s id=%s ordered=%s", dc.label, dc.id, dc.ordered)

    @dc.on("open")
    def _():
        log.info("[dc] OPEN — type messages and press ENTER to send (Ctrl+D to quit)")
        send_loop_task_holder.append(asyncio.create_task(stdin_send_loop(dc)))

    @dc.on("close")
    def _():
        log.warning("[dc] CLOSED")

    @dc.on("message")
    def _(message):
        if isinstance(message, bytes):
            log.info("[dc] RECV (%d bytes binary)", len(message))
        else:
            log.info("[dc] RECV: %s", message)


async def stdin_send_loop(dc: RTCDataChannel) -> None:
    loop = asyncio.get_event_loop()
    while dc.readyState == "open":
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:  # EOF
            log.info("[dc] stdin closed")
            return
        msg = line.rstrip("\n")
        if not msg:
            continue
        dc.send(msg)
        log.info("[dc] SEND: %s", msg)


async def read_remote_sdp(prompt: str) -> RTCSessionDescription:
    log.info(prompt)
    log.info("Paste remote SDP JSON (single line) and press ENTER:")
    loop = asyncio.get_event_loop()
    line = await loop.run_in_executor(None, sys.stdin.readline)
    obj = json.loads(line)
    return RTCSessionDescription(sdp=obj["sdp"], type=obj["type"])


def print_local_sdp(pc: RTCPeerConnection) -> None:
    desc = pc.localDescription
    payload = json.dumps({"type": desc.type, "sdp": desc.sdp})
    # printed with a clear marker so it's easy to copy
    print("\n===== LOCAL SDP (copy the line below to the peer) =====", flush=True)
    print(payload, flush=True)
    print("===== END LOCAL SDP =====\n", flush=True)
    # also dump candidates we can see from the SDP for visibility
    n = sum(1 for ln in desc.sdp.splitlines() if ln.startswith("a=candidate:"))
    log.info("[sdp] local %s contains %d ICE candidate(s)", desc.type, n)


async def run_offer(args) -> None:
    config = build_config(args.stun, args.turn, args.turn_user, args.turn_pass)
    pc = RTCPeerConnection(configuration=config)
    attach_pc_handlers(pc)

    dc = pc.createDataChannel("chat", ordered=True)
    log.info("[dc] createDataChannel(chat, ordered=True)")
    send_task_holder: list = []
    attach_dc_handlers(dc, send_task_holder)

    log.info("[sdp] createOffer()")
    offer = await pc.createOffer()
    log.info("[sdp] setLocalDescription(offer)  (this also gathers ICE)")
    await pc.setLocalDescription(offer)
    print_local_sdp(pc)

    answer = await read_remote_sdp("Waiting for remote ANSWER from peer.")
    log.info("[sdp] setRemoteDescription(answer)")
    await pc.setRemoteDescription(answer)

    await wait_until_done(pc)


async def run_answer(args) -> None:
    config = build_config(args.stun, args.turn, args.turn_user, args.turn_pass)
    pc = RTCPeerConnection(configuration=config)
    attach_pc_handlers(pc)

    send_task_holder: list = []

    @pc.on("datachannel")
    def _(channel: RTCDataChannel):
        log.info("[pc] datachannel event (label=%s)", channel.label)
        attach_dc_handlers(channel, send_task_holder)

    offer = await read_remote_sdp("Waiting for remote OFFER from peer.")
    log.info("[sdp] setRemoteDescription(offer)")
    await pc.setRemoteDescription(offer)

    log.info("[sdp] createAnswer()")
    answer = await pc.createAnswer()
    log.info("[sdp] setLocalDescription(answer)  (this also gathers ICE)")
    await pc.setLocalDescription(answer)
    print_local_sdp(pc)

    await wait_until_done(pc)


async def wait_until_done(pc: RTCPeerConnection) -> None:
    done = asyncio.Event()

    @pc.on("connectionstatechange")
    async def _():
        if pc.connectionState in ("failed", "closed", "disconnected"):
            done.set()

    try:
        await done.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await pc.close()
        log.info("[pc] closed")


# ---------------------------------------------------------------------------
# WebSocket signaling — Render server protocol (probed live, not assumed):
#
#   client -> server  {"type":"register","device_id":"<id>","secret":"<sec>"}
#   server -> client  {"type":"registered","device_id":"<id>"}
#
#   offerer -> server {"type":"offer","device_id":"<peer_device_id>",
#                      "sdp":"...","sdp_type":"offer"}
#   server -> peer    {"type":"offer","from":"<offerer_conn_id>",
#                      "sdp":"...","sdp_type":"offer"}
#
#   answerer -> server {"type":"answer","to":"<from-of-offer>",
#                       "sdp":"...","sdp_type":"answer"}
#   server -> offerer  {"type":"answer","from":"<answerer_conn_id>",
#                       "sdp":"...","sdp_type":"answer"}
#
#   server -> client   {"type":"error","message":"..."}
#
# No trickle-ICE; aiortc embeds candidates in the SDP, so this is fine.
# ---------------------------------------------------------------------------
async def _ws_send(ws, obj: dict) -> None:
    safe = {k: (f"<{len(v)} bytes>" if k == "sdp" else v) for k, v in obj.items()}
    log.info("[ws] SEND %s", safe)
    await ws.send(json.dumps(obj))


async def _send_offer(ws, peer_device_id: str, desc: RTCSessionDescription, relay_only: bool) -> None:
    sdp = _strip_non_relay_candidates(desc.sdp) if relay_only else desc.sdp
    await _ws_send(ws, {
        "type": "offer",
        "device_id": peer_device_id,
        "sdp": sdp,
        "sdp_type": "offer",
    })


async def _send_answer(ws, peer_conn_id: str, desc: RTCSessionDescription, relay_only: bool) -> None:
    sdp = _strip_non_relay_candidates(desc.sdp) if relay_only else desc.sdp
    await _ws_send(ws, {
        "type": "answer",
        "to": peer_conn_id,
        "sdp": sdp,
        "sdp_type": "answer",
    })


def _strip_non_relay_candidates(sdp: str) -> str:
    """Drop every `a=candidate:` line that isn't `typ relay`."""
    out = []
    dropped = 0
    for line in sdp.splitlines():
        if line.startswith("a=candidate:") and " typ relay " not in line + " ":
            dropped += 1
            continue
        out.append(line)
    if dropped:
        log.info("[sdp] stripped %d non-relay candidate(s)", dropped)
    return "\r\n".join(out) + "\r\n"


def _candidates_from_sdp(sdp: str) -> list:
    """Parse `a=candidate:` lines and return a list of (type, proto, addr, port)."""
    out = []
    for line in sdp.splitlines():
        line = line.strip()
        if not line.startswith("a=candidate:"):
            continue
        # a=candidate:foundation component proto priority addr port typ <type> [...]
        toks = line[len("a=candidate:"):].split()
        try:
            proto = toks[2].lower()
            addr  = toks[4]
            port  = toks[5]
            typ_idx = toks.index("typ")
            ctype = toks[typ_idx + 1]
            out.append((ctype, proto, addr, port))
        except (ValueError, IndexError):
            continue
    return out


async def report_selected_pair(pc: RTCPeerConnection) -> None:
    """Print a TURN-vs-direct verdict.

    aiortc doesn't expose `candidate-pair` stats, so we inspect both
    descriptions: we report what each side *offered* and whether any side
    restricted itself to relay-only (the strict TURN proof).
    """
    local_sdp  = pc.localDescription.sdp  if pc.localDescription  else ""
    remote_sdp = pc.remoteDescription.sdp if pc.remoteDescription else ""
    local_cands  = _candidates_from_sdp(local_sdp)
    remote_cands = _candidates_from_sdp(remote_sdp)

    def summarise(cands):
        if not cands: return "(none)"
        types = {}
        for t, *_ in cands:
            types[t] = types.get(t, 0) + 1
        return ", ".join(f"{n}× {t}" for t, n in sorted(types.items()))

    local_types  = {t for t, *_ in local_cands}
    remote_types = {t for t, *_ in remote_cands}

    # Strict proof: if either side advertises *only* relay candidates,
    # ICE has no choice but to use the TURN path.
    local_relay_only  = local_types  == {"relay"}
    remote_relay_only = remote_types == {"relay"}
    any_relay_offered = "relay" in local_types or "relay" in remote_types

    if local_relay_only or remote_relay_only:
        verdict = "RELAY-ONLY (TURN ✅ — proven)"
    elif any_relay_offered:
        verdict = "RELAY OFFERED (TURN may be used; not proven without --relay-only)"
    else:
        verdict = "NO RELAY (TURN not used)"

    log.info("=" * 72)
    log.info("[verdict] %s", verdict)
    log.info("[verdict]   local  candidates: %s", summarise(local_cands))
    log.info("[verdict]   remote candidates: %s", summarise(remote_cands))
    if "relay" in local_types:
        for t, p, a, port in local_cands:
            if t == "relay":
                log.info("[verdict]   local  relay: %s://%s:%s", p, a, port)
    if "relay" in remote_types:
        for t, p, a, port in remote_cands:
            if t == "relay":
                log.info("[verdict]   remote relay: %s://%s:%s", p, a, port)
    log.info("=" * 72)


async def _ws_main(role: str, args) -> None:
    try:
        import websockets
    except ImportError:
        log.error("websockets package not installed — run: pip install websockets")
        return

    url    = args.signaling or os.environ.get("P2P_SIGNALING_URL")
    me     = args.device_id or os.environ.get("P2P_DEVICE_ID")
    secret = args.secret    or os.environ.get("P2P_DEVICE_SECRET")
    peer_device_id = args.peer  # only used by ws-offer

    # Auto-upgrade ws:// to wss:// — Render closes plaintext WS quickly
    if url and url.startswith("ws://") and "onrender.com" in url:
        log.warning("[ws] upgrading %s to wss:// (Render requires TLS)", url)
        url = "wss://" + url[len("ws://"):]

    if not url or not me or not secret:
        log.error("missing signaling config: need P2P_SIGNALING_URL, P2P_DEVICE_ID, P2P_DEVICE_SECRET")
        return
    if role == "ws-offer" and not peer_device_id:
        log.error("ws-offer requires --peer DEVICE_ID (the answerer's device id)")
        return

    config = build_config_from_env()
    if args.relay_only:
        config.iceTransportPolicy = "relay"
        log.info("[pc] iceTransportPolicy = relay (TURN-only)")
        # aiortc doesn't strictly enforce relay-only when assembling the local
        # SDP, so we post-process and strip non-relay candidates ourselves.
        log.info("[pc] will strip non-relay candidates from local SDP")

    pc = RTCPeerConnection(configuration=config)
    attach_pc_handlers(pc)
    send_task_holder: list = []

    if role == "ws-offer":
        dc = pc.createDataChannel("chat", ordered=True)
        log.info("[dc] createDataChannel(chat, ordered=True)")
        attach_dc_handlers(dc, send_task_holder)
    else:
        @pc.on("datachannel")
        def _(channel: RTCDataChannel):
            log.info("[pc] datachannel event (label=%s)", channel.label)
            attach_dc_handlers(channel, send_task_holder)

    @pc.on("connectionstatechange")
    async def _():
        if pc.connectionState == "connected":
            await report_selected_pair(pc)

    log.info("[ws] connecting to %s", url)
    async with websockets.connect(url, max_size=2 ** 22, open_timeout=30) as ws:
        await _ws_send(ws, {"type": "register", "device_id": me, "secret": secret})

        # answerer learns the offerer's connection id from the relayed offer
        offerer_conn_id: dict = {"id": None}

        async def receiver():
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("[ws] non-JSON message: %r", raw[:120])
                    continue
                t = msg.get("type")
                safe = {k: (f"<{len(v)} bytes>" if k == "sdp" else v) for k, v in msg.items()}
                log.info("[ws] RECV %s", safe)

                if t == "registered":
                    log.info("[ws] registered as %s", msg.get("device_id", me))
                    if role == "ws-offer":
                        log.info("[sdp] createOffer()")
                        offer = await pc.createOffer()
                        log.info("[sdp] setLocalDescription(offer)")
                        await pc.setLocalDescription(offer)
                        await _send_offer(ws, peer_device_id, pc.localDescription, args.relay_only)

                elif t == "offer":
                    offerer_conn_id["id"] = msg["from"]
                    log.info("[sdp] setRemoteDescription(offer) from %s", offerer_conn_id["id"])
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=msg["sdp"], type="offer"))
                    log.info("[sdp] createAnswer()")
                    answer = await pc.createAnswer()
                    log.info("[sdp] setLocalDescription(answer)")
                    await pc.setLocalDescription(answer)
                    await _send_answer(ws, offerer_conn_id["id"], pc.localDescription, args.relay_only)

                elif t == "answer":
                    log.info("[sdp] setRemoteDescription(answer) from %s", msg.get("from"))
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=msg["sdp"], type="answer"))

                elif t == "error":
                    log.error("[ws] server error: %s", msg.get("message"))

                else:
                    log.warning("[ws] unknown message type: %s", t)

        recv_task = asyncio.create_task(receiver())

        done = asyncio.Event()

        @pc.on("connectionstatechange")
        async def _():
            if pc.connectionState in ("failed", "closed", "disconnected"):
                done.set()

        try:
            await done.wait()
        except asyncio.CancelledError:
            pass
        finally:
            recv_task.cancel()
            await pc.close()
            log.info("[pc] closed")


def main() -> None:
    # Load .env from the script directory so users can set credentials there.
    load_dotenv(Path(__file__).resolve().parent / ".env")

    p = argparse.ArgumentParser(description="P2P WebRTC client (aiortc)")
    p.add_argument("role", choices=["offer", "answer", "ws-offer", "ws-answer"],
                   help="manual offer/answer (stdin/stdout) or ws-offer/ws-answer (WebSocket signaling)")
    p.add_argument("--stun", default="stun:stun.l.google.com:19302")
    p.add_argument("--turn", default=None, help="e.g. turn:turn.example.com:3478")
    p.add_argument("--turn-user", default=None)
    p.add_argument("--turn-pass", default=None)
    # ws-mode options (default to .env values)
    p.add_argument("--signaling", default=None, help="overrides P2P_SIGNALING_URL")
    p.add_argument("--device-id", default=None, help="overrides P2P_DEVICE_ID")
    p.add_argument("--secret",    default=None, help="overrides P2P_DEVICE_SECRET")
    p.add_argument("--peer",      default=None, help="ws-offer: device_id of the peer to call")
    p.add_argument("--relay-only", action="store_true",
                   help="force iceTransportPolicy=relay (TURN-only) — proves TURN is the path")
    args = p.parse_args()

    if args.role in ("ws-offer", "ws-answer"):
        runner = lambda a: _ws_main(a.role, a)
    else:
        runner = run_offer if args.role == "offer" else run_answer

    try:
        asyncio.run(runner(args))
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
