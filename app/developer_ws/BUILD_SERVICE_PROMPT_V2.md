# Build a voice-relay service that integrates with an upstream orchestrator (v2)

You are building a standalone WebSocket server that joins a voice-call flow driven
by an upstream **orchestrator** server. The orchestrator handles the end user's
microphone and speaker; when a call is bridged, audio flows between the user and
your service through the orchestrator.

Your service is the **WebSocket server** — the orchestrator is the client and
always initiates the connection. What your service does with the audio (transcribe,
classify, transform, route, generate replies, etc.) is your design. This document
specifies only the wire contract, the configuration surface, the public-exposure
mechanism, and the operational expectations the orchestrator depends on.

**What changed from v1:**

- The bind address and the orchestrator-facing public URL are now **separate
  macros** (`BRIDGE_LISTEN_HOST` + `BRIDGE_LISTEN_PORT` + `BRIDGE_PATH` for the
  local listener; `PUBLIC_BRIDGE_URL` for the URL the orchestrator dials).
- A new **"Public exposure (REQUIRED)"** section makes the service reachable from
  the orchestrator with **zero manual signup**, using `localtunnel` over a
  reserved subdomain name. A sibling `start.sh` script wraps the whole thing so
  the deliverable is genuinely one-prompt-and-run.
- All other protocol/handshake/audio rules are unchanged from v1 — orchestrators
  that speak v1 speak v2.

---

## Configuration macros (REQUIRED at the top of the file)

Put a clearly-fenced `CONFIG` block at the very top of the generated file. The
user of your service MUST fill these in before running; if any are still at their
placeholder value at startup, your server MUST print a clear error and exit
non-zero (do not start the listener).

Minimum required macros:

| Macro | Value | Notes |
|---|---|---|
| `BRIDGE_LISTEN_HOST` | `"0.0.0.0"` | Interface the WebSocket server binds to. Almost always `"0.0.0.0"`; `"127.0.0.1"` if you intentionally want loopback-only. |
| `BRIDGE_LISTEN_PORT` | `8001` (int) | Local TCP port to bind. Pick anything free in the 8000–9000 range. |
| `BRIDGE_PATH` | `"/relay"` | URL path the WebSocket server serves. Leading slash required. |
| `PUBLIC_SUBDOMAIN` | `"<unique-name>"` | Subdomain to claim on the public tunnel (see "Public exposure" below). Must be globally unique on `loca.lt`; use a long random suffix to avoid collisions (e.g. `voice-relay-jason-7a3f29`). |
| `PUBLIC_BRIDGE_URL` | derived: `f"wss://{PUBLIC_SUBDOMAIN}.loca.lt{BRIDGE_PATH}"` | The URL the **orchestrator** dials. Computed from the two macros above — do not hardcode. Print this prominently at startup so the operator can copy it into the orchestrator's config. |
| `SERVICE_ID` | `"<short identifier>"` | This service's name, reported in HTTP pings and the WebSocket ack. Any non-empty string; a UUID suffix per process helps distinguish runs in the orchestrator's logs. |
| `ORCHESTRATOR_HTTP_BASE` | `"http(s)://<host>:<port>"` | Base URL of the orchestrator, used by `request_orchestrator_call(user_id)` to POST `/developer/ping/{user_id}`. Local-dev example: `"http://localhost:8000"`. |

Use a sentinel placeholder (e.g. `"REPLACE_ME"`) as the initial value of each
free-form macro so the startup check can detect that the user hasn't edited the
file yet. Numeric ports may default to a real value (`8001`) since they have no
sensible "unset" sentinel.

Example shape (Python; adapt to whatever language/style you produce):

```python
# === CONFIG — fill these in before running ===
# Local listener (the WebSocket server binds here):
BRIDGE_LISTEN_HOST = "0.0.0.0"
BRIDGE_LISTEN_PORT = 8001
BRIDGE_PATH        = "/relay"

# Public-facing URL the orchestrator dials. PUBLIC_SUBDOMAIN must be a
# globally-unique name on loca.lt — pick something with a random suffix
# so nobody else has claimed it. Example: voice-relay-jason-7a3f29
PUBLIC_SUBDOMAIN  = "REPLACE_ME"
PUBLIC_BRIDGE_URL = f"wss://{PUBLIC_SUBDOMAIN}.loca.lt{BRIDGE_PATH}"

# Identifier for this service. Free-form. Example: my-service-3a7f
SERVICE_ID = "REPLACE_ME"

# Base URL of the orchestrator, used by request_orchestrator_call().
# Local-dev example: http://localhost:8000
ORCHESTRATOR_HTTP_BASE = "REPLACE_ME"
# === END CONFIG ===

def _check_macros() -> None:
    missing = [
        name for name, val in {
            "PUBLIC_SUBDOMAIN":       PUBLIC_SUBDOMAIN,
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

If your business logic needs additional configuration, add those to the `CONFIG`
block with the same placeholder-then-check pattern. **Do not put API keys,
passwords, or other secrets in this block.** Required secrets must come from
environment variables and the server must refuse to start if they are unset.

---

## Public exposure (REQUIRED)

The orchestrator dials your service over the public Internet. Your service does
not assume a static IP, a domain, or a cloud account — the deliverable must
self-publish a reachable URL on every run with **no signup, no API token, no
manual dashboard step**. v2 standardizes on **localtunnel** (open-source, MIT,
free public broker at `loca.lt`, no account required) for this.

### Why localtunnel

| Property | Why it matters |
|---|---|
| OSS, MIT licensed | No vendor lock; auditable; free forever |
| No account / no token | Truly autonomous — the prompt + the file is enough |
| HTTPS + WSS native | Orchestrator can dial `wss://…` directly; no plaintext fallback |
| Claim-by-name (`--subdomain`) | Same `PUBLIC_SUBDOMAIN` ⇒ same URL across restarts (as long as no one else has squatted it; mitigate with a long random suffix) |
| Single command | `npx localtunnel --port <p> --subdomain <name>` |

**Trade-off:** the free `loca.lt` broker runs on volunteer infra, so occasional
drops happen and a stranger could in principle squat your subdomain while you
are offline. For development and demo use this is fine. For a production
deployment, swap localtunnel for a self-hosted equivalent (frp, rathole,
chisel) on your own VPS — the protocol does not change, only the public-URL
provider does.

### `start.sh` (REQUIRED sibling script)

Ship a `start.sh` (or platform-equivalent) **alongside the Python file** that
boots the listener and the tunnel together, with a single command:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Preflight: localtunnel needs npx (Node ≥ 18). Fail loudly if missing.
if ! command -v npx >/dev/null 2>&1; then
  echo "ERROR: 'npx' not found. Install Node.js ≥ 18 (https://nodejs.org)." >&2
  exit 1
fi

# Extract the bind port + subdomain straight from the Python CONFIG block so
# this script and the server can never drift apart.
PORT=$(python3 -c "import re,pathlib; \
    print(re.search(r'BRIDGE_LISTEN_PORT\s*=\s*(\d+)', \
    pathlib.Path('service.py').read_text()).group(1))")
SUBDOMAIN=$(python3 -c "import re,pathlib; \
    print(re.search(r'PUBLIC_SUBDOMAIN\s*=\s*\"([^\"]+)\"', \
    pathlib.Path('service.py').read_text()).group(1))")

# Start the WebSocket server in the background.
python3 service.py &
SERVER_PID=$!

# Start the tunnel. --print-requests is optional but useful while debugging.
npx --yes localtunnel --port "$PORT" --subdomain "$SUBDOMAIN" &
TUNNEL_PID=$!

# Make sure both die together on Ctrl-C / kill.
trap "kill $SERVER_PID $TUNNEL_PID 2>/dev/null || true" INT TERM EXIT

echo
echo "================================================================"
echo "  ORCHESTRATOR DIAL URL: wss://${SUBDOMAIN}.loca.lt/<BRIDGE_PATH>"
echo "  (copy the line above into the orchestrator's config)"
echo "================================================================"
echo

wait
```

The script MUST:

1. Refuse to run if `npx` (Node ≥ 18) is not on PATH; print install instructions.
2. Read `BRIDGE_LISTEN_PORT`, `PUBLIC_SUBDOMAIN`, `BRIDGE_PATH` directly from the
   Python file (or whatever your CONFIG source of truth is) so the two never
   drift.
3. Start the server in the background, capture its PID.
4. Start `localtunnel` in the background with the same port + subdomain, capture
   its PID.
5. Install a SIGINT/SIGTERM/EXIT trap that kills **both** PIDs — no orphaned
   tunnels or listeners after Ctrl-C.
6. Print the orchestrator dial URL **clearly and prominently** so the operator
   can copy-paste it without grepping logs.
7. Block on `wait` so the script's exit code reflects either child's exit.

If the language you generate is not Python, produce the equivalent of the script
above for your toolchain (e.g. a `Makefile` target, an `npm` script, a
`Procfile`) — but the same six requirements apply.

---

## Hard requirements

Your service MUST:

1. Listen on `BRIDGE_LISTEN_HOST:BRIDGE_LISTEN_PORT` and serve the WebSocket at
   `BRIDGE_PATH`.
2. **Accept inbound WebSocket connections.** The orchestrator initiates; your
   service only accepts. Do not attempt to open WebSocket connections outbound.
3. Complete the `hello → ack` handshake described below before any audio flows.
4. Handle audio frames in both directions using the JSON envelopes specified
   below.
5. Handle the `bye` control frame for graceful closure, and close with the
   correct WebSocket close codes.
6. Log every connection lifecycle event clearly (connect, hello, ack, audio
   frame counters, bye, disconnect with reason). Operators must be able to
   triage misconnects from logs alone.
7. **Non-secret configuration lives in the CONFIG macros at the top of the
   file.** Secrets (API keys, passwords, DB connection strings, model
   credentials, etc.) MUST come from environment variables and never be
   committed. If a required secret env var is unset at startup, exit non-zero
   with a clear message.
8. **Expose a public function (e.g. `request_orchestrator_call(user_id)`) that
   pings the orchestrator over HTTP so the orchestrator initiates the WebSocket
   handshake back to your service.** See "Outbound calls" below — this function
   MUST exist and MUST be reachable from the rest of your service's code so
   business logic can trigger a callback to a live user.
9. **Print `PUBLIC_BRIDGE_URL` on startup** (in addition to whatever your
   `start.sh` prints) so logs alone are enough to recover the dial URL.

Your service MAY:

- Reject calls based on whatever business rules you choose, using the ack
  rejection protocol.

---

## Wire protocol (v1 — unchanged)

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

- On startup: `listening bind=<host>:<port>${BRIDGE_PATH} public=${PUBLIC_BRIDGE_URL}`
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

Two files, side by side in the same directory:

### 1. `service.py` — the server

- Runs standalone: `python service.py` with no positional args (binds locally
  only; the tunnel is a separate step — see `start.sh`).
- Starts with a clearly-fenced `CONFIG` block at the top defining
  `BRIDGE_LISTEN_HOST`, `BRIDGE_LISTEN_PORT`, `BRIDGE_PATH`,
  `PUBLIC_SUBDOMAIN`, `PUBLIC_BRIDGE_URL`, `SERVICE_ID`,
  `ORCHESTRATOR_HTTP_BASE` (plus any business-logic macros it needs), each
  initialised to a placeholder sentinel where appropriate.
- Refuses to start if any required macro is still at its placeholder — print
  the list of missing macros and exit non-zero.
- Listens at `BRIDGE_LISTEN_HOST:BRIDGE_LISTEN_PORT${BRIDGE_PATH}` and speaks
  the protocol above end-to-end.
- Prints `PUBLIC_BRIDGE_URL` on startup so the dial URL is recoverable from
  the server's own logs (not just `start.sh`'s output).
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

### 2. `start.sh` — the one-command launcher

- Boots `service.py` and the localtunnel process together, traps signals, kills
  both on exit, prints the orchestrator dial URL prominently. See the "Public
  exposure" section above for the exact requirements.
- Running `bash start.sh` is the only thing an operator should need to do after
  filling in the `CONFIG` macros. Everything else — public URL, signal
  handling, child cleanup — happens automatically.
