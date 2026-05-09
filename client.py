"""
P2P WebRTC client (Python / aiortc).

Manual signaling: starts an offerer or answerer, prints its local SDP to stdout
as a single JSON line, then reads the remote SDP from stdin (also a single JSON
line). Pairs cleanly with the HTML client: copy the JSON between the two.

Covers:
  - RTCPeerConnection setup with RTCConfiguration (STUN + optional TURN)
  - DataChannel creation (offerer) / handler (answerer)
  - ICE candidate event handler (aiortc gathers candidates as part of
    setLocalDescription, so we log them after the description is set)
  - Offer/Answer creation flow + SDP exchange
  - Async event loop for connection control
  - Connection state events + verbose logging

Run:
  pip install aiortc
  python client.py offer
  python client.py answer
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
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


def main() -> None:
    p = argparse.ArgumentParser(description="P2P WebRTC client (aiortc)")
    p.add_argument("role", choices=["offer", "answer"], help="who creates the offer")
    p.add_argument("--stun", default="stun:stun.l.google.com:19302")
    p.add_argument("--turn", default=None, help="e.g. turn:turn.example.com:3478")
    p.add_argument("--turn-user", default=None)
    p.add_argument("--turn-pass", default=None)
    args = p.parse_args()

    runner = run_offer if args.role == "offer" else run_answer
    try:
        asyncio.run(runner(args))
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
