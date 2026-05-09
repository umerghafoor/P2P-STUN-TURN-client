# P2P STUN/TURN Client — three implementations

A minimal but complete WebRTC peer-to-peer DataChannel demo, implemented three
times so you can compare APIs side by side. All three speak the same wire
format (a single JSON line: `{"type":"offer|answer","sdp":"…"}`), so any pair
can interoperate: HTML ↔ HTML, HTML ↔ Python, HTML ↔ C++, Python ↔ C++, etc.

| File         | Language     | Library                                                                |
| ------------ | ------------ | ---------------------------------------------------------------------- |
| `index.html` | JS (browser) | native `RTCPeerConnection`                                             |
| `client.py`  | Python 3.9+  | [`aiortc`](https://github.com/aiortc/aiortc)                           |
| `client.cpp` | C++17        | [`libdatachannel`](https://github.com/paullouisageneau/libdatachannel) |

> Why libdatachannel for C++? It is a small, standalone, MIT-licensed WebRTC
> implementation that exposes the same conceptual API surface as Google's
> libwebrtc (PeerConnection, DataChannel, observer callbacks, ICE configuration)
> but builds in minutes instead of hours and is a single dependency.

## Quick start (`run.sh`)

`run.sh` sources `.env` from the project root if present, so the Python and
C++ clients pick up the same `P2P_*` config you'd use in production.

```bash
./run.sh              # interactive menu
./run.sh py-offer     # manual offerer (stdin/stdout SDP)
./run.sh py-answer    # manual answerer
./run.sh ws-offer     # WebSocket-signaling offerer (uses .env)
./run.sh ws-answer    # WebSocket-signaling answerer (uses .env)
./run.sh build        # configure + build C++ client
./run.sh cpp-offer
./run.sh cpp-answer
./run.sh html         # open index.html in your browser
```

`.env` keys consumed: `P2P_SIGNALING_URL`, `P2P_DEVICE_ID`, `P2P_DEVICE_SECRET`,
`P2P_STUN_SERVERS` (comma-separated), `P2P_TURN_URL`, `P2P_TURN_USERNAME`,
`P2P_TURN_CREDENTIAL`. Any of these can be overridden on the CLI
(`--signaling`, `--device-id`, `--secret`, `--stun`, `--turn`, `--turn-user`,
`--turn-pass`, `--peer`).

For `ws-offer` you also need a peer device id to call:
`PEER=edge-device-2 ./run.sh ws-offer`, or the menu will prompt for it.

## WebSocket signaling protocol (assumed)

The `ws-*` modes connect to `P2P_SIGNALING_URL` and assume this JSON shape
(adjust `_send_sdp` / `_send_candidate` / the dispatch switch in
[client.py](client.py) if your server differs):

```text
client → server  {"type":"register","device_id":"...","secret":"..."}
server → client  {"type":"registered","device_id":"..."}
client → server  {"type":"offer","to":"peer-id","sdp":"..."}
server → client  {"type":"offer","from":"peer-id","sdp":"..."}
client → server  {"type":"answer","to":"peer-id","sdp":"..."}
server → client  {"type":"answer","from":"peer-id","sdp":"..."}
either side      {"type":"candidate","to|from":"peer-id",
                  "candidate":"candidate:...","sdpMid":"...","sdpMLineIndex":0}
either side      {"type":"bye","to|from":"peer-id"}
server → client  {"type":"error","message":"..."}
```

Install the extra dependency for WS mode: `pip install websockets`.

## Signaling

There is no signaling server. The two peers exchange exactly two JSON
messages — an **offer** and an **answer** — by **copy-paste**. Each program
prints its local SDP to stdout (or its textarea, in the browser) once ICE
gathering finishes, and reads the remote SDP from stdin (or a textarea).

Flow (any two clients):

1. **Peer A**: produce an **offer**, give it to peer B.
2. **Peer B**: paste the offer, produce an **answer**, give it back to A.
3. **Peer A**: paste the answer. The DataChannel opens; you can chat.

## 1. HTML client — `index.html`

A self-contained file. Open it in a modern browser (file:// works for STUN;
TURN over TLS may need https:// depending on the server). The page has:

- ICE-server config (STUN URL, TURN URL + creds, transport policy)
- Offer / Answer / Set-Remote buttons (manual signaling)
- Live status panel: signalingState, iceGatheringState, iceConnectionState,
  connectionState, DataChannel state, and the **selected candidate pair**
- A verbose log of every event the spec exposes:
  `signalingstatechange`, `icegatheringstatechange`, `iceconnectionstatechange`,
  `connectionstatechange`, **every** `icecandidate` (with full attributes),
  `icecandidateerror`, `negotiationneeded`, `datachannel`, plus full SDP dumps
  and `getStats()` snapshots
- Per-level filter checkboxes, autoscroll, clear, and download-as-text

Two browser windows on the same machine work fine for testing: open the file
twice, click "Create Offer" in window 1, copy its local SDP to window 2,
"Create Answer" there, copy that back to window 1, "Set Remote".

## 2. Python client — `client.py`

```bash
pip install aiortc
python client.py offer    # peer A
python client.py answer   # peer B
```

Each side prints its local SDP between `===== LOCAL SDP =====` markers; copy
the JSON line to the other side's stdin and press ENTER. Once the channel
opens, anything you type is sent over the DataChannel.

CLI flags: `--stun`, `--turn`, `--turn-user`, `--turn-pass`.

Logging is at INFO by default. Set `PYTHONLOGLEVEL=DEBUG` (or change the
`logging.basicConfig` line) for the full SDP and ICE detail.

## 3. C++ client — `client.cpp`

```bash
cmake -B build && cmake --build build -j
./build/p2p_client offer    # peer A
./build/p2p_client answer   # peer B
```

The first `cmake` configure will fetch and build libdatachannel if you don't
already have it installed. Same flags as Python: `--stun`, `--turn`,
`--turn-user`, `--turn-pass`.

The wire format is identical to the other two clients, so a C++ offerer and
an HTML answerer (or vice versa) will connect.

## Interop quick-test (HTML ↔ Python)

```bash
python client.py offer
# copy the JSON line it prints, paste into index.html's "remote SDP" box,
# click "Create Answer", copy the textarea contents,
# paste that into the python process and press ENTER.
```

The browser log will show every ICE candidate and state transition, and both
sides will exchange chat messages over the DataChannel.

## What each implementation maps to in the spec

| Concept                 | HTML                            | Python (aiortc)                            | C++ (libdatachannel)                                          |
| ----------------------- | ------------------------------- | ------------------------------------------ | ------------------------------------------------------------- |
| Peer connection         | `new RTCPeerConnection(config)` | `RTCPeerConnection(configuration=…)`       | `rtc::PeerConnection(rtc::Configuration)`                     |
| ICE servers (STUN/TURN) | `config.iceServers`             | `RTCConfiguration(iceServers=[…])`         | `rtc::Configuration::iceServers`                              |
| DataChannel (offerer)   | `pc.createDataChannel("chat")`  | `pc.createDataChannel("chat")`             | `pc->createDataChannel("chat")`                               |
| DataChannel (answerer)  | `pc.ondatachannel`              | `@pc.on("datachannel")`                    | `pc->onDataChannel(...)`                                      |
| ICE candidate event     | `pc.onicecandidate`             | (carried in SDP after setLocalDescription) | `pc->onLocalCandidate(...)`                                   |
| Offer / Answer          | `createOffer/createAnswer`      | `pc.createOffer/createAnswer`              | implicit (driven by createDataChannel / setRemoteDescription) |
| Apply local SDP         | `setLocalDescription`           | `setLocalDescription`                      | `setLocalDescription` (called automatically)                  |
| Apply remote SDP        | `setRemoteDescription`          | `setRemoteDescription`                     | `pc->setRemoteDescription(...)`                               |
| Connection state events | `connectionstatechange`, etc.   | `@pc.on("connectionstatechange")`          | `pc->onStateChange(...)`                                      |
| External signaling      | textareas + buttons             | stdin/stdout JSON                          | stdin/stdout JSON                                             |

## Notes & limitations

- No TURN credentials are baked in; bring your own for NAT-restricted networks.
  Free public STUN works for most home connections.
- aiortc gathers ICE during `setLocalDescription`, so it does not expose a
  per-candidate event the way the browser does — the candidates appear
  embedded in the SDP. The other two clients log them as they arrive.
- libdatachannel emits the local description **after** gathering finishes
  (trickle-off mode), which is the simplest model for copy-paste signaling.
