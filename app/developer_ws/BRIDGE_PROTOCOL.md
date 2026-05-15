# developer-WS bridge protocol (v1)

This document describes the wire protocol between the **main** server's
`developer_ws` pipeline and any **remote service** it relays audio to. The
shipped reference implementation is [`testing/echo_server.py`](testing/echo_server.py);
this doc is what you'd implement against to build your own service.

The bridge is initiated by main (main → remote), so your service is always
the **WebSocket server**. Main is always the **WebSocket client**.

---

## URLs

Substitute your deployment values for these placeholders:

| Placeholder | What it is | Configured on |
|---|---|---|
| `<MAIN_BASE>` | HTTP base URL of main (e.g. `https://main.example.com`) | Your caller |
| `<REMOTE_BRIDGE_URL>` | The `ws://`/`wss://` URL of your service's relay endpoint | `DEVELOPER_WS_REMOTE_BRIDGE_URL` env var on main |

Both are runtime configuration — never hardcode them. Main reads
`DEVELOPER_WS_REMOTE_BRIDGE_URL` from its environment (or `.env`); the value
shipped today is `ws://localhost:8001/relay` for local development.

---

## Lifecycle

```
                ┌──────────────────────────┐                ┌────────────────────────┐
                │  main (developer_ws)     │                │  remote service        │
                └──────────────────────────┘                └────────────────────────┘
                              │                                         │
              (optional)      │   POST <MAIN_BASE>/developer/ping/{uid} │
                              │ ◄─────────────────────────────────────  │   server-initiated call
                              │                                         │
                              │   WS connect → <REMOTE_BRIDGE_URL>      │
                              │ ──────────────────────────────────────► │
                              │   {"type":"hello", ...}                 │
                              │ ──────────────────────────────────────► │
                              │   {"type":"ack", "accept":true, ...}    │
                              │ ◄─────────────────────────────────────  │
                              │                                         │
                              │   {"audio":"...", "sr":16000, ...}      │  uplink (mic)
                              │ ──────────────────────────────────────► │
                              │   {"audio":"...", "sr":24000}           │  downlink (speaker)
                              │ ◄─────────────────────────────────────  │
                              │           ...                           │
                              │   {"type":"bye", "reason":"..."}        │
                              │ ──────────────────────────────────────► │
                              │   {"type":"bye", "reason":"ack"}        │
                              │ ◄─────────────────────────────────────  │
                              │              close 1000                 │
```

A session has four phases: **handshake**, **audio**, **goodbye**, **close**.
Audio cannot flow until the ack returns `accept:true`.

---

## Phase 1 — Handshake

### `hello` (main → remote)

```json
{
  "type": "hello",
  "user_id": "<uuid string identifying the end user>",
  "version": "1"
}
```

Sent immediately after the WebSocket upgrades. Main expects an `ack` reply
within `DEVELOPER_WS_BRIDGE_ACK_TIMEOUT_S` seconds (default 5). If the
remote sends nothing in that window, main treats it as **no pickup**, closes
the socket, and tells the end user "The remote service didn't pick up."

### `ack` (remote → main)

Accept:

```json
{
  "type": "ack",
  "accept": true,
  "service_id": "<your service's identifier — free-form>",
  "version": "1"
}
```

Reject:

```json
{
  "type": "ack",
  "accept": false,
  "reason": "<short human-readable reason>"
}
```

After sending a reject, close the WebSocket with close code **4403**.
After sending an accept, simply stay open and proceed to the audio phase.

**Version negotiation:** today there is only `version: "1"`. If your service
returns a different version, main logs a warning but proceeds. Behave the
same way if you receive a hello with a different version.

---

## Phase 2 — Audio

After a successful accept, audio frames flow in both directions as text
WebSocket messages (JSON-encoded).

### Uplink (main → remote)

```json
{
  "audio": "<base64-encoded PCM>",
  "sr": 16000,
  "turn_complete": false
}
```

- `audio` is **PCM int16, little-endian, mono**, base64-encoded.
- `sr` is always 16000 (uplink sample rate).
- `turn_complete` is always `false` while the bridge is active — turn
  segmentation is handled inside main, not over the bridge.
- Frames arrive in roughly 1.5s batches but you should not depend on a
  fixed batch size.

### Downlink (remote → main)

```json
{
  "audio": "<base64-encoded PCM>",
  "sr": 24000
}
```

- Same encoding (PCM int16 LE mono base64).
- `sr` **should be 24000** (downlink sample rate). Main does not resample;
  any other rate will play back at the wrong speed/pitch.
- If your audio source is a different rate, resample before sending. The
  reference echo server uses `audioop.ratecv` to upsample 16k → 24k.

Frames with unknown or missing `type` and no `audio` field are silently
dropped on both sides — safe to add new fields without breaking older peers.

---

## Phase 3 — Goodbye

Either side may initiate a graceful close:

```json
{ "type": "bye", "reason": "<short string>" }
```

The receiver SHOULD respond with its own `bye` and then close the WebSocket
with close code **1000**. After a `bye` is sent or received, no further
audio frames will be sent in either direction.

When main initiates teardown (the user said "stop", the bridge call ended,
etc.), it sends `bye` with `reason: "local_close"` before closing.

When the remote terminates a call (its session ended, a timeout fired,
etc.), send `bye` so main can announce the disconnect to the user with the
correct message (`"The remote service disconnected."`).

If a peer disappears without sending `bye` (process killed, network drop),
the other side logs an "abrupt" disconnect — still safe, just noisier.

---

## Phase 4 — Close codes

| Code | Meaning | Sent by |
|------|---------|---------|
| `1000` | Normal closure (after a clean `bye` exchange) | either side |
| `1002` | Protocol error (missing/malformed hello or ack, etc.) | either side |
| `4403` | Call rejected (ack `accept:false`) | remote |

These are advisory — the logs on each side identify what happened with more
detail than the close code alone.

---

## Server-initiated calls (HTTP ping)

If your service wants main to call it (rather than waiting for the end user
to ask), POST to:

```
POST <MAIN_BASE>/developer/ping/{user_id}
Content-Type: application/json

{
  "service_id": "<your service identifier>",
  "version": "1"
}
```

- The body is optional. If you omit it, main records `service_id="unknown"`.
- `service_id` should be the same identifier you'll return in the
  WebSocket-handshake `ack`. Main reads it *before* dialing the bridge and
  logs it; later, after the WS ack arrives, main compares the two and warns
  if they don't match (e.g., the wrong service answered the bridge URL).
- `version` is the protocol version you intend to speak on the WS.

### Response

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"ok": true, "user_id": "<id>", "service_id": "<id>"}` | Main accepted; the bridge will dial `<REMOTE_BRIDGE_URL>` next. |
| `200` | `{"ok": false, "reason": "no active session", "user_id": "<id>", "service_id": "<id>"}` | No live WS session for that user. |
| `200` | `{"ok": false, "user_id": "<id>", "service_id": "<id>"}` | Main tried but the bridge handshake failed. See main's log for the specific outcome. |

When the ping is accepted, main first speaks an announcement to the user
("Your service wants to speak with you. Connecting you now.") and then
performs the standard `hello → ack` handshake against your service. So your
service must already be listening at `<REMOTE_BRIDGE_URL>` *before* it
sends the ping.

---

## Building a compatible service — minimum checklist

A spec-compliant remote service needs to:

1. Accept a WebSocket connection at `<REMOTE_BRIDGE_URL>`.
2. Read the first text frame and verify it's a valid `hello`.
3. Send an `ack` within ~5 seconds (either `accept:true` or `accept:false`).
4. On accept: read audio frames as JSON `{audio, sr}`, write audio frames
   in the same shape (PCM int16 LE mono base64, sr=24000 preferred).
5. Honor `{"type":"bye"}` from main (respond with bye, close 1000).
6. Optionally: POST to `<MAIN_BASE>/developer/ping/{user_id}` to have main
   initiate a call to your service.

The reference implementation in
[`testing/echo_server.py`](testing/echo_server.py) is ~150 lines and does
all of this, including the optional ping path. Use it as a starting point.

---

## Configuration knobs (on main)

| Env var | Default | Meaning |
|---|---|---|
| `DEVELOPER_WS_REMOTE_BRIDGE_URL` | `ws://localhost:8001/relay` | URL main dials for the bridge |
| `DEVELOPER_WS_BRIDGE_ACK_TIMEOUT_S` | `5.0` | Seconds main waits for `ack` before declaring "no pickup" |
| `DEVELOPER_WS_VAD_RMS` | `20` | Voice-activity RMS gate for utterance segmentation (unrelated to the bridge but tunable per environment) |
| `DEVELOPER_WS_END_SILENCE_SEC` | `2.0` | Seconds of silence after speech to end an utterance |

All read at process start via `python-dotenv` on `<repo>/.env`.
