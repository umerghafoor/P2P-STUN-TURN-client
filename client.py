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
    RTCIceCandidate,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp

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
# WebSocket signaling
#
# Assumed JSON protocol (server-side may differ — adjust to match yours):
#
#   client -> server  {"type":"register",  "device_id":"...", "secret":"..."}
#   server -> client  {"type":"registered","device_id":"..."}                # ok
#   server -> client  {"type":"error",     "message":"..."}                  # rejected
#
#   client -> server  {"type":"offer",     "to":"peer-id", "sdp":"..."}
#   server -> client  {"type":"offer",     "from":"peer-id", "sdp":"..."}
#   client -> server  {"type":"answer",    "to":"peer-id", "sdp":"..."}
#   server -> client  {"type":"answer",    "from":"peer-id", "sdp":"..."}
#
#   either direction  {"type":"candidate", "to/from":"peer-id",
#                      "candidate":"candidate:...", "sdpMid":"0", "sdpMLineIndex":0}
#   either direction  {"type":"bye",       "to/from":"peer-id"}
#
# If your server uses different field names (e.g. "target" instead of "to",
# or wraps SDP as `{"sdp":{"type":"offer","sdp":"..."}}`) tweak the helpers
# `_send_sdp` / `_send_candidate` and the `dispatch` switch below.
# ---------------------------------------------------------------------------
async def _ws_send(ws, obj: dict) -> None:
    log.info("[ws] SEND %s", {k: ("…" if k == "sdp" else v) for k, v in obj.items()})
    await ws.send(json.dumps(obj))


async def _send_sdp(ws, kind: str, peer: str, desc: RTCSessionDescription) -> None:
    await _ws_send(ws, {"type": kind, "to": peer, "sdp": desc.sdp})


# Kept available for forward-trickle if you switch to a server/peer that
# expects per-candidate messages. aiortc doesn't fire per-candidate events,
# so this isn't called automatically — candidates ride along inside the SDP.
async def _send_candidate(ws, peer: str, cand: RTCIceCandidate) -> None:
    await _ws_send(ws, {
        "type": "candidate",
        "to": peer,
        "candidate": "candidate:" + candidate_to_sdp(cand),
        "sdpMid": cand.sdpMid,
        "sdpMLineIndex": cand.sdpMLineIndex,
    })


def _parse_remote_candidate(msg: dict) -> RTCIceCandidate:
    raw = msg["candidate"]
    if raw.startswith("candidate:"):
        raw = raw[len("candidate:"):]
    cand = candidate_from_sdp(raw)
    cand.sdpMid = msg.get("sdpMid")
    cand.sdpMLineIndex = msg.get("sdpMLineIndex")
    return cand


async def _ws_main(role: str, args) -> None:
    try:
        import websockets
    except ImportError:
        log.error("websockets package not installed — run: pip install websockets")
        return

    url    = args.signaling or os.environ.get("P2P_SIGNALING_URL")
    me     = args.device_id or os.environ.get("P2P_DEVICE_ID")
    secret = args.secret    or os.environ.get("P2P_DEVICE_SECRET")
    peer   = args.peer

    if not url or not me or not secret:
        log.error("missing signaling config: need P2P_SIGNALING_URL, P2P_DEVICE_ID, P2P_DEVICE_SECRET (or --signaling/--device-id/--secret)")
        return
    if role == "ws-offer" and not peer:
        log.error("ws-offer requires --peer DEVICE_ID (the answerer's device id)")
        return

    config = build_config_from_env()
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

    log.info("[ws] connecting to %s", url)
    async with websockets.connect(url, max_size=2 ** 22) as ws:
        await _ws_send(ws, {"type": "register", "device_id": me, "secret": secret})

        # offerer state: peer is known up front; answerer learns it from the offer message
        remote_peer: dict = {"id": peer}

        async def receiver():
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("[ws] non-JSON message: %r", raw[:120])
                    continue
                t = msg.get("type")
                log.info("[ws] RECV %s", {k: ("…" if k == "sdp" else v) for k, v in msg.items()})

                if t == "registered":
                    log.info("[ws] registered as %s", msg.get("device_id", me))
                    if role == "ws-offer":
                        log.info("[sdp] createOffer()")
                        offer = await pc.createOffer()
                        log.info("[sdp] setLocalDescription(offer)")
                        await pc.setLocalDescription(offer)
                        await _send_sdp(ws, "offer", remote_peer["id"], pc.localDescription)

                elif t == "offer":
                    remote_peer["id"] = msg.get("from", remote_peer["id"])
                    sdp = msg["sdp"]
                    log.info("[sdp] setRemoteDescription(offer) from %s", remote_peer["id"])
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
                    log.info("[sdp] createAnswer()")
                    answer = await pc.createAnswer()
                    log.info("[sdp] setLocalDescription(answer)")
                    await pc.setLocalDescription(answer)
                    await _send_sdp(ws, "answer", remote_peer["id"], pc.localDescription)

                elif t == "answer":
                    sdp = msg["sdp"]
                    log.info("[sdp] setRemoteDescription(answer) from %s", msg.get("from"))
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))

                elif t == "candidate":
                    try:
                        cand = _parse_remote_candidate(msg)
                        await pc.addIceCandidate(cand)
                        log.info("[ice] applied remote candidate")
                    except Exception as e:
                        log.warning("[ice] could not apply candidate: %s", e)

                elif t == "bye":
                    log.warning("[ws] peer said bye")
                    return

                elif t == "error":
                    log.error("[ws] server error: %s", msg.get("message"))

                else:
                    log.warning("[ws] unknown message type: %s", t)

        recv_task = asyncio.create_task(receiver())

        # close cleanly when PC enters a terminal state
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
            try:
                await _ws_send(ws, {"type": "bye", "to": remote_peer["id"]})
            except Exception:
                pass
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
