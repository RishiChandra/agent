# Build a voice-relay service that integrates with an upstream orchestrator

You are building a standalone WebSocket server that joins a voice-call flow driven
by an upstream **orchestrator** server. The orchestrator handles the end user's
microphone and speaker; when a call is bridged, audio flows between the user and
your service through the orchestrator.

Your service is the **WebSocket server** — the orchestrator is the client and
always initiates the connection. What your service does with the audio (transcribe,
classify, transform, route, generate replies, etc.) is your design. This document
specifies only the wire contract, the configuration surface, and the operational
expectations the orchestrator depends on.

---

## Configuration macros (REQUIRED at the top of the file)

Put a clearly-fenced `CONFIG` block at the very top of the generated file. The
user of your service MUST fill these in before running; if any are still at their
placeholder value at startup, your server MUST print a clear error and exit
non-zero (do not start the listener).

Minimum required macros:

| Macro | Value | Notes |
|---|---|---|
| `BRIDGE_LISTEN_URL` | `"ws://<host>:<port>/<path>"` | The WebSocket URL this server listens on. Must match the URL the orchestrator is configured to dial. Local-dev example: `"ws://0.0.0.0:8001/relay"`. |
| `SERVICE_ID` | `"<short identifier>"` | This service's name, reported in HTTP pings and the WebSocket ack. Any non-empty string; a UUID suffix per process helps distinguish runs in the orchestrator's logs. |
| `ORCHESTRATOR_HTTP_BASE` | `"http(s)://<host>:<port>"` | Base URL of the orchestrator, used by `request_orchestrator_call(user_id)` to POST `/developer/ping/{user_id}`. Local-dev example: `"http://localhost:8000"`. |

Use a sentinel placeholder (e.g. `"REPLACE_ME"`) as the initial value of each
macro so the startup check can detect that the user hasn't edited the file yet.
Example shape (Python; adapt to whatever language/style you produce):

```python
# === CONFIG — fill these in before running ===
# WebSocket URL this server listens on. The orchestrator will dial this URL.
# Local-dev example: ws://0.0.0.0:8001/relay
BRIDGE_LISTEN_URL = "REPLACE_ME"

# Identifier for this service. Free-form. Example: my-service-3a7f
SERVICE_ID = "REPLACE_ME"

# Base URL of the orchestrator, used by request_orchestrator_call().
# Local-dev example: http://localhost:8000
ORCHESTRATOR_HTTP_BASE = "REPLACE_ME"
# === END CONFIG ===

def _check_macros() -> None:
    missing = [name for name, val in {
        "BRIDGE_LISTEN_URL": BRIDGE_LISTEN_URL,
        "SERVICE_ID": SERVICE_ID,
        "ORCHESTRATOR_HTTP_BASE": ORCHESTRATOR_HTTP_BASE,
    }.items() if not val or val == "REPLACE_ME"]
    if missing:
        raise SystemExit(
            f"ERROR: set these macros at the top of this file before running: "
            f"{', '.join(missing)}"
        )
```

Call the check during startup, before binding the listener.

If your business logic needs additional configuration, add those to the `CONFIG`
block with the same placeholder-then-check pattern. **Do not put API keys,
passwords, or other secrets in this block.** Required secrets must come from
environment variables and the server must refuse to start if they are unset.

---

## Hard requirements

Your service MUST:

1. Listen on the WebSocket endpoint defined by `BRIDGE_LISTEN_URL`.
2. **Accept inbound WebSocket connections.** The orchestrator initiates; your
   service only accepts. Do not attempt to open WebSocket connections outbound.
3. Complete the `hello → ack` handshake described below before any audio flows.
4. Handle audio frames in both directions using the JSON envelopes specified
   below.
5. Handle the `bye` control frame for graceful closure, and close with the
   correct WebSocket close codes.
6. Log every connection lifecycle event clearly (connect, hello, ack, audio frame
   counters, bye, disconnect with reason). Operators must be able to triage
   misconnects from logs alone.
7. **Non-secret configuration lives in the CONFIG macros at the top of the file.**
   Secrets (API keys, passwords, DB connection strings, model credentials, etc.)
   MUST come from environment variables and never be committed. If a required
   secret env var is unset at startup, exit non-zero with a clear message.
8. **Expose a public function (e.g. `request_orchestrator_call(user_id)`) that
   pings the orchestrator over HTTP so the orchestrator initiates the WebSocket
   handshake back to your service.** See "Outbound calls" below — this function
   MUST exist and MUST be reachable from the rest of your service's code so
   business logic can trigger a callback to a live user.

Your service MAY:

- Reject calls based on whatever business rules you choose, using the ack
  rejection protocol.

---

## Wire protocol (v1)

All messages are **text** WebSocket frames carrying a JSON body. All audio
payloads are **PCM int16, little-endian, mono, base64-encoded**.

### 1. Hello (orchestrator → you)

The first text frame after the WebSocket upgrade. Read it and verify
`type == "hello"`. You have ~5 seconds to respond with an ack before the
orchestrator times out and tears the socket down.

```json
{ "type": "hello", "user_id": "<uuid string identifying the end user>", "version": "1" }
```

If the first frame is malformed, missing, or has a `type` other than `"hello"`,
close the WebSocket with code **1002**.

### 2. Ack (you → orchestrator)

Accept:
```json
{ "type": "ack", "accept": true, "service_id": "<your service identifier>", "version": "1" }
```

Reject:
```json
{ "type": "ack", "accept": false, "reason": "<short human-readable reason>" }
```
After sending a reject, close the WebSocket with code **4403**.

### 3. Audio frames (bidirectional, after a successful accept)

Uplink (orchestrator → you), the end user's mic audio:
```json
{ "audio": "<base64>", "sr": 16000, "turn_complete": false }
```
- `sr` is always 16000 (16 kHz).
- Frames arrive in roughly 1.5-second batches but you should not depend on a
  fixed batch size.

Downlink (you → orchestrator), audio you want the end user to hear:
```json
{ "audio": "<base64>", "sr": 24000 }
```
- `sr` **should be 24000** (24 kHz). The orchestrator does not resample — any
  other rate plays back at the wrong speed/pitch. If your audio source is a
  different rate, resample before sending (e.g. with `audioop.ratecv`).
- Mono int16 LE base64, same as uplink.

Frames that are missing both `type` and `audio` should be silently dropped on
either side. This keeps the protocol forward-compatible — new fields can be
added later without breaking older peers.

### 4. Bye (bidirectional)

Either side may send a `bye` to signal graceful closure:
```json
{ "type": "bye", "reason": "<short string>" }
```
The receiver should respond with its own `bye` and close the WebSocket with
code **1000**. After a `bye` is sent or received, no further audio frames
should be sent.

### 5. Close codes

| Code | Meaning |
|------|---------|
| `1000` | Normal closure (after a clean `bye` exchange) |
| `1002` | Protocol error (missing/malformed hello or ack, etc.) |
| `4403` | Call rejected (ack with `accept: false`) |

---

## Outbound calls (REQUIRED method)

Your service must expose a named, callable function that asks the orchestrator
to place a call to it. The end user only hears audio that flows over the
WebSocket; if your service produces a message, alert, or any output the user
should hear, **the only way that output reaches the user is by first calling
this function to trigger the bridge**. Without it, your service is a one-way
receiver.

Implement it like this (Python sketch; adapt names/style to your codebase but
keep the function easily findable):

```python
import httpx

# IMPORTANT FOR USERS OF THIS SERVICE:
# Call this function whenever your service has something to say to a connected
# end user. It asks the orchestrator to open a WebSocket back to us so audio
# can flow. Without calling this, the orchestrator never knows we want to
# speak — even though our WebSocket listener is running. Pass the user_id of
# the end user you want to reach.
async def request_orchestrator_call(user_id: str) -> dict:
    url = f"{ORCHESTRATOR_HTTP_BASE.rstrip('/')}/developer/ping/{user_id}"
    payload = {"service_id": SERVICE_ID, "version": "1"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(url, json=payload)
        return r.json()
```

This function MUST:

1. Take a `user_id` as its argument (the end-user session the orchestrator
   should call).
2. POST to `<ORCHESTRATOR_HTTP_BASE>/developer/ping/{user_id}` with a JSON body
   containing your `service_id` and `version`.
3. Return the orchestrator's JSON response so callers can branch on
   `{"ok": true|false, "reason": ...}`.
4. Be importable / callable from elsewhere in your service (don't bury it
   inside a startup-only closure).
5. Carry a **clear, prominent comment in the source file** explaining that the
   user of your service must call it whenever the service has something to send
   to a connected end user. The comment template above is fine; rephrase if you
   prefer, but the intent must be unambiguous to whoever reads the file next.

After the orchestrator accepts the ping it will dial your service over
WebSocket using the standard `hello → ack` handshake described above. The
response body is JSON: `{"ok": true|false, "user_id": "...", "service_id": "..."}`.
An `ok: false` with `reason: "no active session"` means there is no live
end-user session for that `user_id`; surface this back to your caller so they
can retry later.

### Calling it on startup vs on demand

For an always-on broadcaster (e.g. a one-shot demo or notification service),
call `request_orchestrator_call(...)` once during startup or shortly after.

For an event-driven service (e.g. "ping the user when a job finishes"), call it
from your event handler. The function is plain Python; treat it like any other
async API call your business logic makes.

Wire it up to a CLI flag for local testing (e.g. `--ping <user_id>` that calls
the function on startup with that user id) so callers can exercise the full
round-trip without writing extra code.

---

## Logging expectations

Every WebSocket session should produce, at minimum, these log lines (format is
your choice but stay structured-ish):

- On accept: `connected peer=<host:port>`
- After hello: `hello OK peer=<peer> user_id=<id> client_version=<v>`
- After ack: `ACK sent (accept|reject) peer=<peer> user_id=<id>` (with the
  reason if reject)
- On `bye`: `bye received peer=<peer> user_id=<id> reason=<r>`
- On disconnect (any reason): `session ended peer=<peer> user_id=<id>
  frames_in=<N> frames_out=<N> reason=<r>` — frame counters are essential for
  diagnosing audio-flow problems.

Log warnings (not errors) on protocol violations from the peer: missing hello,
malformed JSON, send failures, version mismatch.

The same applies on the outbound HTTP ping path:
- Before the request: log `pinging orchestrator user_id=<id>`.
- After the response: log the status code and body.
- On exception: log a warning with the error.

---

## Deliverable

A single Python file (or a small self-contained package) that:

- Runs standalone: `python <your_file>.py` with no positional args.
- Starts with a clearly-fenced `CONFIG` block at the top defining
  `BRIDGE_LISTEN_URL`, `SERVICE_ID`, `ORCHESTRATOR_HTTP_BASE` (plus any
  business-logic macros it needs), each initialised to a placeholder sentinel.
- Refuses to start if any required macro is still at its placeholder — print
  the list of missing macros and exit non-zero.
- Listens at `BRIDGE_LISTEN_URL` and speaks the protocol above end-to-end.
- Exposes a public `request_orchestrator_call(user_id)` (or equivalently named)
  async function as described in "Outbound calls". Its comment must clearly
  tell future readers: "call this whenever the service has audio/output that
  should reach a connected end user — without it, the orchestrator can't
  initiate the WebSocket".
- Includes a module docstring and short docstrings on key functions describing
  caller/callee relationships (`Called by: …`, `Calls: …`).
- Contains no hardcoded URLs or hostnames outside the `CONFIG` block, and no
  secrets anywhere — secrets come from env vars and fail loudly if unset.
- Logs every connection lifecycle event as described above.
