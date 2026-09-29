# Orchestrator ↔ agent protocol (v2)

This document describes the wire protocol between the **main** server (the
orchestrator) and any **remote service** (an agent). The shipped reference
implementation is [`testing/echo_server.py`](testing/echo_server.py); frame
names and constants live in [`app/agent_protocol.py`](../agent_protocol.py).

An agent can serve one or both **modes**:

| Mode | What happens | Since |
|---|---|---|
| `bridge` | The user's microphone is relayed to the agent and the agent's audio/text is played back. A live, hands-off conversation. | v1 |
| `task` | The orchestrator sends the agent a structured task, keeps talking to the user, and speaks the result when the agent reports back. Many tasks, from many users, are multiplexed over one connection. | v2 |

**v2 is backward compatible.** Bridge mode is unchanged from v1. The v2 `hello`
adds fields a v1 agent ignores; a v1 agent's ack (no `modes`) is read as
"bridge only". Task mode is opt-in: an agent that never declares `task` never
receives a task.

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
                              │   {"audio":"...", "sr":24000}           │  downlink — audio
                              │ ◄─────────────────────────────────────  │     OR
                              │   {"type":"say", "text":"..."}          │  downlink — text→TTS
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
  "version": "2",
  "mode": "bridge",
  "session_id": "sess_<hex>"
}
```

- `mode` is `"bridge"` or `"task"`. v1 hellos have no `mode`; treat a missing
  `mode` as `"bridge"`.
- In task mode `user_id` is `"orchestrator"`: the connection is shared, and
  each `task.dispatch` carries its own `user_id`.

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
  "version": "2",
  "modes": ["bridge", "task"],
  "max_concurrency": 8
}
```

- `modes` lists every mode the agent supports. Omit it (v1) to mean `["bridge"]`.
- `max_concurrency` is the most tasks the agent will run at once on this
  connection. The orchestrator never exceeds the smaller of this and the value
  registered for the agent. Default 8.
- If the hello asks for a mode you don't support, reject with
  `accept:false` and close with code **4405**.

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

**Version negotiation:** main speaks `"2"` and accepts acks carrying `"1"` or
`"2"`; anything else logs a warning but proceeds. Behave the same way if you
receive a hello with a version you don't know.

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

Two payload shapes are accepted; choose whichever fits your service.

**Audio frame** (you've already synthesized speech locally):

```json
{
  "audio": "<base64-encoded PCM>",
  "sr": 24000
}
```

- Same encoding as uplink (PCM int16 LE mono base64).
- `sr` **should be 24000** (downlink sample rate). Main does not resample;
  any other rate will play back at the wrong speed/pitch.
- If your audio source is a different rate, resample before sending. The
  reference echo server uses `audioop.ratecv` to upsample 16k → 24k.

**Text frame** (let main synthesize for you):

```json
{
  "type": "say",
  "text": "Hello, your order has shipped."
}
```

- Main runs the text through its TTS service (Piper, same voice as the
  built-in assistant) and plays the result to the user.
- Useful for services that produce text but don't ship TTS — notifications,
  status updates, scripted responses.
- The `text` field is required and must be non-empty after trimming;
  whitespace-only frames are dropped silently.
- Side-effects: agent text bypasses the assistant's conversation history.
  Future LLM turns won't know what the agent said. If the user replies,
  the assistant has no record. Wire it back yourself if you need that.
- You can freely mix `audio` and `say` frames in the same session.

Frames with unknown or missing `type` and no `audio`/`text` field are
silently dropped on both sides — safe to add new fields without breaking
older peers.

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
| `4405` | Requested mode not supported | remote |

These are advisory — the logs on each side identify what happened with more
detail than the close code alone.

---

## Task mode (v2)

After a `hello` with `"mode":"task"` and an accepting ack that lists `"task"`,
the socket carries task frames in both directions, multiplexed by `task_id`.
The orchestrator opens **one connection per agent** and reuses it for every
user's tasks; it closes the socket after ~60 s idle (`bye`, reason `idle`), so
expect to be re-dialed. Handle frames for different `task_id`s concurrently.

```
 orchestrator                                       agent
     │  task.dispatch {task_id, user_id, intent, ...}  │
     │ ──────────────────────────────────────────────► │
     │  task.accepted {task_id}                        │   within ~5 s, or the
     │ ◄────────────────────────────────────────────── │   orchestrator moves on
     │  task.progress {task_id, message}   (0..n)      │
     │ ◄────────────────────────────────────────────── │
     │  task.input_required {task_id, question} (opt.) │   asked to the user
     │ ◄────────────────────────────────────────────── │   out loud
     │  task.input {task_id, answer}                   │
     │ ──────────────────────────────────────────────► │
     │  task.result {task_id, status, say, output}     │   spoken to the user
     │ ◄────────────────────────────────────────────── │
```

### Orchestrator → agent

**`task.dispatch`**

```json
{
  "type": "task.dispatch",
  "task_id": "task_<hex>",
  "user_id": "<end user>",
  "intent": "Book a table for two at Nopa at 7pm tonight",
  "input": {"details": "window seat if possible"},
  "deadline_ms": 118000,
  "attempt": 1,
  "idempotency_key": "task_<hex>"
}
```

- `intent` is one imperative sentence written by the orchestrator's LLM from
  what the user said. `input` is optional structured context.
- `deadline_ms` is how long the orchestrator will wait for `task.result`
  from now. After it passes the orchestrator sends `task.cancel` and tells the
  user the task timed out.
- `idempotency_key` is stable across retries. The orchestrator only retries
  on a different agent, and only before any agent accepted the task; if your
  agent is reached twice with the same key, do the work once.

**`task.cancel`** `{"type":"task.cancel","task_id":"...","reason":"user_cancelled|deadline|orchestrator_shutdown"}`
Stop work if you can. No reply is required.

**`task.input`** `{"type":"task.input","task_id":"...","answer":"..."}`
The user's answer to your `task.input_required` question, as transcribed.

**`ping`** `{"type":"ping","ts":...}` — reply `{"type":"pong","ts":...}`.
Either side may ping.

### Agent → orchestrator

| Frame | Fields | Meaning |
|---|---|---|
| `task.accepted` | `task_id`, optional `eta_ms` | You're working on it. Send within ~5 s of the dispatch. |
| `task.rejected` | `task_id`, `reason`, `retryable` (bool, default `true`) | You won't do it. `retryable:true` lets the orchestrator try another capable agent (only when the user didn't name you). `retryable:false` ends the task, and `reason` is told to the user. |
| `task.progress` | `task_id`, `message`, optional `pct` | Optional status. Stored for status queries and not spoken. Throttled if you flood it. |
| `task.input_required` | `task_id`, `question` | You need something from the user. The question is spoken; the deadline is extended while the user answers. Keep it one short question. |
| `task.result` | `task_id`, `status` (`succeeded`\|`failed`), `say`, `output`, `error` | Final. `say` (≤ 2000 chars) is spoken to the user verbatim, so write it for the ear. `output` is arbitrary JSON (≤ 256 KB) returned to API callers. On `failed`, `error` is told to the user. |

Unlike bridge-mode `say` frames, task results **are** recorded in the
assistant's conversation history, so the user can follow up ("great, now add
it to my calendar").

### Failure semantics the orchestrator applies

- Connect or handshake failure, no `task.accepted` in time, or a
  `retryable` rejection: the next-best capable agent is tried if the user
  asked for an outcome rather than naming an agent. Up to 3 attempts.
- Disconnect **after** `task.accepted`: the task fails and is **not**
  retried, because your agent may already have acted.
- Repeated dial failures park your agent for a cool-down so it stops
  receiving traffic, and one success clears it.

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
3. Send an `ack` within ~5 seconds (either `accept:true` or `accept:false`),
   listing your `modes`.
4. On accept: read audio frames as JSON `{audio, sr}`, write either
   audio frames in the same shape (PCM int16 LE mono base64, sr=24000
   preferred) **or** text frames `{"type":"say","text":"..."}` to have
   main synthesize speech for you. Mix freely.
5. Honor `{"type":"bye"}` from main (respond with bye, close 1000).
6. Optionally: POST to `<MAIN_BASE>/developer/ping/{user_id}` to have main
   initiate a call to your service.
7. Optionally (task mode): declare `"task"` in `modes` both when you register
   and in your ack, and implement `task.dispatch` → `task.accepted` →
   `task.result`. `task.cancel`, `task.input_required` and `task.progress`
   are recommended but optional.

The reference implementation in
[`testing/echo_server.py`](testing/echo_server.py) is ~150 lines and does
all of this, including the optional ping path. Use it as a starting point.

---

## Configuration knobs (on main)

| Env var | Default | Meaning |
|---|---|---|
| `DEVELOPER_WS_REMOTE_BRIDGE_URL` | `ws://localhost:8001/relay` | URL main dials for the bridge |
| `DEVELOPER_WS_BRIDGE_ACK_TIMEOUT_S` | `5.0` | Seconds main waits for `ack` before declaring "no pickup" |
| `DEVELOPER_WS_BRIDGE_CONNECT_TIMEOUT_S` | `5.0` | Seconds main waits for the bridge TCP/TLS/WebSocket upgrade |
| `DISPATCH_ACCEPT_TIMEOUT_S` | `5.0` | Task mode: seconds to wait for `task.accepted` |
| `DISPATCH_DEFAULT_DEADLINE_S` | `120` | Task mode: default overall deadline per task |
| `DISPATCH_INPUT_WAIT_S` | `120` | Task mode: deadline extension while waiting on the user's answer |
| `DISPATCH_IDLE_CLOSE_S` | `60` | Task mode: idle pooled connections are closed after this |
| `DISPATCH_MAX_ATTEMPTS` | `3` | Task mode: agents tried per task (intent-routed only) |
| `DEVELOPER_WS_VAD_RMS` | `20` | Voice-activity RMS gate for utterance segmentation (unrelated to the bridge but tunable per environment) |
| `DEVELOPER_WS_END_SILENCE_SEC` | `2.0` | Seconds of silence after speech to end an utterance |

All read at process start via `python-dotenv` on `<repo>/.env`.
