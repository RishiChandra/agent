# Build a voice-relay service that integrates with an upstream orchestrator

You are building a standalone WebSocket server that joins a voice-call flow driven
by an upstream **orchestrator** server. The orchestrator handles the end user's
microphone and speaker; when a call is bridged, audio flows between the user and
your service through the orchestrator.

Your service is the **WebSocket server** — the orchestrator is the client and
always initiates the connection. What your service does with the audio (transcribe,
classify, transform, route, generate replies, etc.) is your design. This document
specifies the wire contract, the configuration surface, the self-publishing
mechanism, and the operational expectations the orchestrator depends on.

**Operating principle.** The deliverable must run end-to-end after a single
`python service.py` invocation. No browser signup, no API token paste, no DNS
record, no manual URL copy. The service spawns its own public tunnel, learns
its own dial URL at runtime, and registers that URL with the orchestrator
before accepting calls.

---

## Configuration macros (REQUIRED at the top of the file)

Put a clearly-fenced `CONFIG` block at the very top of the generated file. The
user MUST fill these in before running; if any are still at their placeholder at
startup, the server MUST print a clear error and exit non-zero (do not start the
listener).

| Macro | Value | Notes |
|---|---|---|
| `BRIDGE_LISTEN_HOST` | `"0.0.0.0"` | Interface the WebSocket server binds to. Almost always `"0.0.0.0"`; `"127.0.0.1"` if you intentionally want loopback-only. |
| `BRIDGE_LISTEN_PORT` | `8001` (int) | Local TCP port to bind. Pick anything free in the 8000–9000 range. |
| `BRIDGE_PATH` | `"/relay"` | URL path the WebSocket server serves. Leading slash required. |
| `SERVICE_ID` | `"<short identifier>"` | This service's name, sent to the orchestrator on every register/ping and reported in the WebSocket ack. Free-form string; a UUID suffix per process helps distinguish runs in the orchestrator's logs. |
| `ORCHESTRATOR_HTTP_BASE` | `"http(s)://<host>:<port>"` | Base URL of the orchestrator. Used by `register_with_orchestrator()` and `request_orchestrator_call()`. Local-dev example: `"http://localhost:8000"`. |

Notice what is **not** in the CONFIG block: there is no `PUBLIC_BRIDGE_URL`
macro. The dial URL is **discovered at runtime** from the tunnel process, not
hand-typed by the user. Hardcoding a public URL would defeat the entire
self-publishing design.

Use a sentinel placeholder (e.g. `"REPLACE_ME"`) as the initial value for each
free-form string macro so the startup check can detect that the user hasn't
edited the file. Numeric ports may default to a real value (`8001`) since they
have no sensible "unset" sentinel.

Example shape (Python; adapt to whatever language/style you produce):

```python
# === CONFIG — fill these in before running ===
BRIDGE_LISTEN_HOST = "0.0.0.0"
BRIDGE_LISTEN_PORT = 8001
BRIDGE_PATH        = "/relay"

# Identifier for this service. Free-form. Example: my-service-3a7f
SERVICE_ID = "REPLACE_ME"

# Base URL of the orchestrator (no trailing slash). Local-dev example:
# http://localhost:8000
ORCHESTRATOR_HTTP_BASE = "REPLACE_ME"
# === END CONFIG ===

def _check_macros() -> None:
    missing = [
        name for name, val in {
            "SERVICE_ID":             SERVICE_ID,
            "ORCHESTRATOR_HTTP_BASE": ORCHESTRATOR_HTTP_BASE,
        }.items()
        if not val or val == "REPLACE_ME"
    ]
    if missing:
        raise SystemExit(
            f"ERROR: set these macros at the top of this file before running: "
            f"{', '.join(missing)}"
        )
```

Call the check during startup, before binding the listener.

If your business logic needs additional configuration, add it to the `CONFIG`
block with the same placeholder-then-check pattern. **Do not put API keys,
passwords, or other secrets in this block.** Required secrets must come from
environment variables and the server must refuse to start if they are unset.

---

## Public exposure (REQUIRED)

The orchestrator dials your service over the public Internet. Your service does
not assume a static IP, a domain, or a cloud account — it self-publishes a
reachable URL on every run with **zero signup, zero token, zero browser step**.
The deliverable standardizes on **`cloudflared` quick tunnels** for this.

### Why cloudflared quick tunnels

| Property | Why it matters |
|---|---|
| Zero account / zero token | Truly autonomous — `cloudflared tunnel --url http://localhost:<port>` works on a clean machine |
| Free, no rate-limit signup wall | Forever-free for development use |
| HTTPS + WSS native | Orchestrator dials `wss://…` directly; TLS terminated at Cloudflare's edge |
| Reliable | Cloudflare's global edge, not volunteer infra |
| Single binary | Install once (`brew install cloudflared` on macOS, GitHub releases on Linux/Windows); openclaw can install if missing |

**Trade-off:** the URL is **random every restart** (`https://<random-words>.trycloudflare.com`). This is fine — we eliminate URL drift on the orchestrator side via the registration step (see next section). Static URLs would require an account; we are explicitly trading URL stability for zero-friction autonomy.

### How the service spawns its own tunnel

Inside `service.py` (no separate shell wrapper required):

1. On startup, after the macro check, the service launches `cloudflared tunnel
   --url http://localhost:<BRIDGE_LISTEN_PORT>` as a subprocess. Capture both
   stdout and stderr.
2. Read the subprocess output line-by-line until you match a
   `https://[a-z0-9-]+\.trycloudflare\.com` URL (typically appears within
   2–5 s). Hard timeout after 30 s — exit non-zero with a clear error if no
   URL appears (most likely cloudflared is not installed or the network is
   blocked).
3. Construct `PUBLIC_BRIDGE_URL = "wss://" + <hostname> + BRIDGE_PATH`.
4. Print `PUBLIC_BRIDGE_URL` once, prominently — it is useful for debugging
   even though the orchestrator learns it via registration, not from the user.
5. Install a SIGINT / SIGTERM / `atexit` handler that kills the cloudflared
   subprocess. No orphaned tunnels after Ctrl-C.

If `cloudflared` is not on `PATH`, exit non-zero with an install hint:

```
ERROR: 'cloudflared' not found. Install it before running:
  macOS:   brew install cloudflared
  Linux:   https://github.com/cloudflare/cloudflared/releases (download binary, chmod +x, move to /usr/local/bin)
  Windows: winget install --id Cloudflare.cloudflared
```

Optionally: if the env var `PUBLIC_BRIDGE_URL` is already set when the service
starts, skip the cloudflared subprocess and use the provided URL directly.
Useful for tests that run their own tunnel, or for production swaps to a
self-hosted tunnel (frp / rathole / chisel) without modifying `service.py`.

---

## Registering with the orchestrator (REQUIRED)

Because the dial URL changes every restart, the orchestrator must learn the
current URL from the service rather than reading it from a config file. Before
accepting any WebSocket connection, the service registers itself.

### Registration handshake

`POST {ORCHESTRATOR_HTTP_BASE}/developer/register`

Request body:
```json
{ "service_id": "<your service id>", "public_url": "wss://<host>/<path>", "version": "1" }
```

Response body:
```json
{ "ok": true,  "service_id": "..." }
```
or
```json
{ "ok": false, "reason": "<short human-readable string>" }
```

The orchestrator stores the `service_id → public_url` mapping. On any subsequent
`request_orchestrator_call(user_id)` (see "Outbound calls" below), the
orchestrator dials whatever URL is currently registered for that `service_id`.

### Service obligations

1. **Register exactly once before binding the listener.** If registration
   returns `ok: false`, exit non-zero with the reason — do not start the
   listener with a stale mapping.
2. **Re-register on URL change.** If the cloudflared subprocess dies and
   restarts (rare but possible), re-discover the URL and POST `/developer/register`
   again with the new URL.
3. **Unregister on graceful shutdown.** On SIGTERM / SIGINT, before killing
   cloudflared and exiting, send:
   `POST {ORCHESTRATOR_HTTP_BASE}/developer/unregister` with body
   `{ "service_id": "<id>" }`. Best-effort — log warnings on failure but do
   not block shutdown.
4. **Heartbeat (optional but recommended).** Every 5 minutes, re-POST
   `/developer/register` with the current URL. Lets the orchestrator detect
   dead services that crashed without unregistering.

Implement registration alongside the existing `request_orchestrator_call()`
in the same Python module so both share one `httpx.AsyncClient`.

```python
import httpx

async def register_with_orchestrator(public_url: str) -> dict:
    """Tell the orchestrator how to reach us. MUST succeed before we bind."""
    url = f"{ORCHESTRATOR_HTTP_BASE.rstrip('/')}/developer/register"
    payload = {"service_id": SERVICE_ID, "public_url": public_url, "version": "1"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()

async def unregister_with_orchestrator() -> None:
    """Best-effort cleanup on shutdown. Failures are logged, not raised."""
    url = f"{ORCHESTRATOR_HTTP_BASE.rstrip('/')}/developer/unregister"
    payload = {"service_id": SERVICE_ID}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception as e:
        print(f"WARN: unregister failed: {e}", flush=True)
```

---

## Hard requirements

Your service MUST:

1. Listen on `BRIDGE_LISTEN_HOST:BRIDGE_LISTEN_PORT` and serve the WebSocket at
   `BRIDGE_PATH`.
2. **Spawn its own public tunnel** as described in "Public exposure" and learn
   `PUBLIC_BRIDGE_URL` at runtime. Do not require the user to provide a public
   URL.
3. **Register the discovered `PUBLIC_BRIDGE_URL` with the orchestrator** as
   described above, before accepting any WebSocket connection.
4. **Accept inbound WebSocket connections.** The orchestrator initiates; your
   service only accepts. Do not attempt to open WebSocket connections outbound.
5. Complete the `hello → ack` handshake described below before any audio flows.
6. Handle audio frames in both directions using the JSON envelopes specified
   below.
7. Handle the `bye` control frame for graceful closure, and close with the
   correct WebSocket close codes.
8. Log every connection lifecycle event clearly (startup, tunnel ready,
   registration, connect, hello, ack, audio frame counters, bye, disconnect with
   reason, unregister, shutdown). Operators must be able to triage misconnects
   from logs alone.
9. **Non-secret configuration lives in the CONFIG macros at the top of the
   file.** Secrets (API keys, passwords, DB connection strings, model
   credentials, etc.) MUST come from environment variables and never be
   committed. If a required secret env var is unset at startup, exit non-zero
   with a clear message.
10. **Expose a public function (e.g. `request_orchestrator_call(user_id)`) that
    pings the orchestrator over HTTP so the orchestrator initiates the WebSocket
    handshake back to your service.** See "Outbound calls" below.
11. **Clean up the tunnel subprocess on shutdown** (SIGINT / SIGTERM / atexit).
    No orphaned `cloudflared` processes after Ctrl-C.

Your service MAY:

- Reject calls based on whatever business rules you choose, using the ack
  rejection protocol.

---

## Wire protocol

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

After the orchestrator accepts the ping it looks up the currently-registered
`PUBLIC_BRIDGE_URL` for the given `service_id` and dials your service over
WebSocket using the standard `hello → ack` handshake. The response body is
JSON: `{"ok": true|false, "user_id": "...", "service_id": "..."}`. An `ok:
false` with `reason: "no active session"` means there is no live end-user
session for that `user_id`; surface this back to your caller so they can retry
later. `ok: false` with `reason: "service not registered"` means the
orchestrator has no current URL for your `service_id` — this should never
happen during normal operation (registration runs before the listener accepts),
but if it does, re-register and retry.

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

- On tunnel ready: `tunnel ready public=<PUBLIC_BRIDGE_URL>`
- On register: `registered service_id=<id> public=<url> ok=<true|false> reason=<r if any>`
- On startup: `listening bind=<host>:<port>${BRIDGE_PATH} public=${PUBLIC_BRIDGE_URL}`
- On accept: `connected peer=<host:port>`
- After hello: `hello OK peer=<peer> user_id=<id> client_version=<v>`
- After ack: `ACK sent (accept|reject) peer=<peer> user_id=<id>` (with the
  reason if reject)
- On `bye`: `bye received peer=<peer> user_id=<id> reason=<r>`
- On disconnect (any reason): `session ended peer=<peer> user_id=<id>
  frames_in=<N> frames_out=<N> reason=<r>` — frame counters are essential for
  diagnosing audio-flow problems.
- On heartbeat: `heartbeat registered=<true|false>` (only if heartbeat is on)
- On shutdown: `unregister sent`, then `tunnel killed pid=<n>`, then `bye`

Log warnings (not errors) on protocol violations from the peer: missing hello,
malformed JSON, send failures, version mismatch.

The same applies on the outbound HTTP paths (`/developer/register`,
`/developer/unregister`, `/developer/ping/<user_id>`):
- Before the request: log the call with the user_id / public_url being sent.
- After the response: log the status code and body.
- On exception: log a warning with the error.

---

## Deliverable

A single Python file `service.py` (or a small self-contained package) that:

- Runs standalone: `python service.py` with no positional args. Spawns its
  own cloudflared tunnel, registers with the orchestrator, then binds the
  WebSocket listener. No shell wrapper, no separate tunnel command, no
  manual URL handoff.
- Starts with a clearly-fenced `CONFIG` block at the top defining
  `BRIDGE_LISTEN_HOST`, `BRIDGE_LISTEN_PORT`, `BRIDGE_PATH`, `SERVICE_ID`,
  `ORCHESTRATOR_HTTP_BASE` (plus any business-logic macros it needs), each
  initialised to a placeholder sentinel where appropriate.
- Refuses to start if any required macro is still at its placeholder — print
  the list of missing macros and exit non-zero.
- Refuses to start if `cloudflared` is not on `PATH` — print the install hint
  and exit non-zero.
- Refuses to start if `register_with_orchestrator()` returns `ok: false` —
  print the orchestrator's reason and exit non-zero.
- Discovers `PUBLIC_BRIDGE_URL` at runtime from cloudflared's output and
  prints it once, prominently, on startup.
- Honours `PUBLIC_BRIDGE_URL` from the environment if it is already set
  (skips the cloudflared subprocess) — supports swapping in a self-hosted
  tunnel for production without code changes.
- Exposes public async functions `register_with_orchestrator(public_url)`,
  `unregister_with_orchestrator()`, and `request_orchestrator_call(user_id)`
  as described above. The `request_orchestrator_call` comment must clearly
  tell future readers: "call this whenever the service has audio/output that
  should reach a connected end user — without it, the orchestrator can't
  initiate the WebSocket".
- Includes a module docstring and short docstrings on key functions describing
  caller/callee relationships (`Called by: …`, `Calls: …`).
- Contains no hardcoded URLs or hostnames outside the `CONFIG` block, and no
  secrets anywhere — secrets come from env vars and fail loudly if unset.
- Cleans up the cloudflared subprocess and unregisters from the orchestrator on
  SIGINT / SIGTERM / atexit.
- Logs every connection lifecycle event as described above.
