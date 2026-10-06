# developer-WS bridge protocol (v1, with v2 draft)

This document describes the wire protocol between the **main** server's
`developer_ws` pipeline (the orchestrator) and any **remote service** (an
agent) it talks to. The shipped reference implementation is
[`testing/echo_server.py`](testing/echo_server.py); this doc is what you'd
implement against to build your own service.

> **Status.** **v1 (bridge mode) is live**, and everything not marked *DRAFT*
> describes what main does today. Sections marked **DRAFT (v2)** specify
> the planned task mode and the v2 additions to bridge mode. Main does **not**
> implement them yet, and their details may change before they ship. The
> design rationale is in
> [`ORCHESTRATOR_V2_TOOL_CALLS.md`](../../ORCHESTRATOR_V2_TOOL_CALLS.md) §10.8. A v1
> service keeps working unchanged after v2 ships.

The WebSocket is always initiated by main (main → remote), so your service is
always the **WebSocket server** and main is always the **WebSocket client**.
Your service reaches main over plain HTTP (`/developer/register`,
`/developer/ping` and, in v2, the task-event callback).

---

## Versions and modes

| Mode | What it is | Connection | Version |
|---|---|---|---|
| `bridge` | **Live call.** The user's mic is relayed to you, and your audio or `say` text is played back | One WebSocket per call, per user | v1 (live). v2 additions are DRAFT |
| `task` | **Background task.** Main sends you a structured job (JSON), you ack or nack it, and you report progress, questions and a result later. Main can query, update, cancel or close the task at any time | **The task outlives the connection.** Connections are short-lived or pooled, and every message is keyed by `task_id` | **DRAFT (v2)** |

Both modes use your **single registered WebSocket URL**. The `mode` field in
the `hello` tells them apart. A v1 `hello` has no `mode` and means `bridge`.

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

## Registration — DRAFT (v2) additions

Services already register with `POST <MAIN_BASE>/developer/register` (see
`BUILD_SERVICE_PROMPT.md`). In v2, **registration is where you declare
capabilities**. Main needs them before it can reach you, and they don't depend
on any particular connection.

```json
{
  "service_id": "tabletop-3a7f",
  "version": "2",
  "binding": "ws",
  "public_url": "wss://…/relay",
  "modes": ["bridge", "task"],
  "task_ops": ["dispatch", "status", "update", "cancel", "input", "close", "delivered"],
  "events": ["callback", "ws"],
  "max_concurrency": 8,
  "max_reply_latency_s": 5,
  "default_deadline_s": null,
  "side_effects": true,
  "open_task_ids": []
}
```

| Field | Meaning | Default |
|---|---|---|
| `binding` | How main reaches you. Only `ws` (main dials your WebSocket) in v2. `pull` and `a2a` are reserved for future bindings | `ws` |
| `public_url` | Your WebSocket URL. **Required when `binding` is `ws`** | none |
| `modes` | `bridge` and/or `task` | `["bridge"]` |
| `task_ops` | Task requests you handle. `dispatch` is required for task mode | `["dispatch", "cancel", "input"]` |
| `events` | How you deliver task events. **`callback` is required for task mode**. `ws` is optional | `[]` |
| `max_concurrency` | Max tasks you'll run at once (1–1000) | 8 |
| `max_reply_latency_s` | How long main waits for your `task.ack`/`task.nack` before resending (1–30 for `ws`) | 5 |
| `default_deadline_s` | Deadline applied when the user states none (see [Deadlines](#deadlines)) | null |
| `side_effects` | Your tasks act in the world (book, buy, send). Main reads requests back to the user before dispatching | false |
| `open_task_ids` | On heartbeats: tasks you're still working on (see [Liveness](#liveness)) | omitted |

Every heartbeat may resend these fields. Main keeps the latest.

---

## Lifecycle (bridge mode)

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

Task mode uses the same handshake, then exchanges task messages instead of
audio. See [Task mode](#task-mode--draft-v2).

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

#### DRAFT (v2): `hello` additions

```json
{
  "type": "hello",
  "version": "2",
  "mode": "bridge",
  "user_id": "<end user id>",
  "session_id": "<id of this connection, for logs>",
  "context_id": "<conversation thread id; see Identifiers>",
  "task_id": "<present only when a task was escalated to a live call>"
}
```

| Field | Bridge mode | Task mode |
|---|---|---|
| `mode` | `"bridge"` (absent in v1, which means bridge) | `"task"` |
| `user_id` | The end user | Always `"orchestrator"`, because one task connection carries tasks for many users. Each `task.dispatch` carries its own `user_id` |
| `context_id` | The user's thread with your service. It matches the `context_id` of their background tasks, so a live call can see what was asked before | Absent (sent per task message) |
| `task_id` | Present when main escalated a task to a live call ("Tabletop has a few questions, want me to put you through?"). Pick up where the task left off | Absent |

If you receive a `mode` you don't support, reply `ack {accept:false}` and
close with **4405**.

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

After sending a reject, close the WebSocket with close code **4403**
(**4405** if the reason is an unsupported mode, in v2). After sending an
accept, simply stay open and proceed to the audio phase (or, in task mode, the
task-message phase).

#### DRAFT (v2): `ack` additions

A v2 ack repeats your capabilities for this connection. It may **narrow**
what you registered (for example a lower `max_concurrency` while you're under
load), but never add to it. Main uses your registration wherever the two
differ upward.

```json
{
  "type": "ack",
  "accept": true,
  "service_id": "tabletop-3a7f",
  "version": "2",
  "modes": ["bridge", "task"],
  "max_concurrency": 8,
  "task_ops": ["dispatch", "status", "update", "cancel", "input", "close", "delivered"],
  "events": ["callback", "ws"]
}
```

| Field | Meaning | If absent |
|---|---|---|
| `modes` | Modes you accept on this URL. Must include the `mode` from the hello | Your registered `modes` (`["bridge"]` for v1) |
| `max_concurrency` | Max tasks right now. Main uses the smaller of this and your registered value | Registered value |
| `task_ops` | Task requests you handle on this connection | Registered value |
| `events` | How you deliver task events. **`callback` is required for task mode** | Registered value |

Main's fallbacks when an op is missing:

| Missing | Main does |
|---|---|
| `update` | Cancels the task and dispatches a new one, after reading the change back to the user |
| `status` | Nothing changes. Main answers "is it done?" from the last event either way |
| `close`, `delivered` | Skips those notices. They are informational |
| `callback` in `events` | Treats you as bridge-only. Task mode needs push delivery, because main never polls |

**Version negotiation:** today there is only `version: "1"`. If your service
returns a different version, main logs a warning but proceeds. Behave the
same way if you receive a hello with a different version. In v2, a v1 ack to a
`mode:"bridge"` hello is fine: main reads it as `modes:["bridge"]`. A v1 ack to
a `mode:"task"` hello is treated as "task not supported", and main closes the
connection.

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
  **DRAFT (v2):** main records `say` text in the history, marked as quoted
  agent output, so a follow-up like "change that booking" can be resolved.
  Agent text is never treated as an instruction to main.
- You can freely mix `audio` and `say` frames in the same session.

Frames with unknown or missing `type` and no `audio`/`text` field are
silently dropped on both sides — safe to add new fields without breaking
older peers.

### DRAFT (v2): handing off a task during a live call

If the call produces ongoing work ("I'll confirm the booking and let you
know"), send a `task.created` request over the transport you're on (during a live
call, the bridge socket), so main tracks it
as a background task. Without it, the promise is lost when the call ends.

```json
{
  "type": "task.created",
  "msg_id": "a-19",
  "context_id": "<from the hello>",
  "body": {
    "intent": "Confirm the Nopa booking for 7pm, 2 people",
    "slots": { "restaurant": "Nopa", "time": "19:00", "party_size": 2 },
    "agent_task_ref": "bk-551",
    "notify_hint": "normal"
  }
}
```

Main replies `task.ack {task_id}` with a main-minted `task_id` (or
`task.nack`). From then on it is an ordinary task: report on it with
`task.event` (see [Events](#events-you--main)), including after the bridge
closes.

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

In task mode (DRAFT), `bye` ends the **connection only**. Tasks continue, and
main sends `bye` with `reason: "idle"` when it reaps an idle pooled
connection.

---

## Phase 4 — Close codes

| Code | Meaning | Sent by |
|------|---------|---------|
| `1000` | Normal closure (after a clean `bye` exchange) | either side |
| `1002` | Protocol error (missing/malformed hello or ack, etc.) | either side |
| `4403` | Call rejected (ack `accept:false`) | remote |
| `4405` | **DRAFT (v2).** Mode not supported (e.g. `mode:"task"` to a bridge-only service) | remote |

These are advisory — the logs on each side identify what happened with more
detail than the close code alone.

---

## Task mode — DRAFT (v2)

> Not implemented by main yet. This section is the spec agents will build
> against. Feedback welcome before it ships.

### Model

Task dispatch is **main pinging your service with a JSON request, and your
service acking or nacking it**. Every later operation (status, update, cancel,
answer a question, close) is another request referring to the same `task_id`.
Your service reports progress and the result as **events**, whenever they
happen.

The key rule: **a task is not tied to a connection.** Main may open a
connection, dispatch, receive the ack and close. It may ask about the same
task an hour later on a new connection. You may deliver the result over any
open connection, or by HTTP callback. Neither side treats a disconnect as a
task failure.

```
main                                               your service
  │ connect + hello(task) ─────────────────────────────►│
  │◄──────────────────────────────── ack(task_ops, …)    │
  │ task.dispatch {task_id:T, …} ──────────────────────►│  persist T, then:
  │◄──────────────────────── task.ack {status:accepted}  │
  │ (connection closed)                                  │  …work…
  │◄──── POST /developer/tasks/T/events {seq:1, succeeded, result}
  │ 200 {ack_seq:1} ────────────────────────────────────►│
  │                                                      │
  │ … later: "move it to eight" …                        │
  │ connect + hello(task) ─────────────────────────────►│
  │ task.update {task_id:T, changes{time:"20:00"}} ────►│
  │◄──────────────── task.ack {applied{time:"20:00"}}    │   or task.nack {code:"too_late"}
```

### Identifiers

| ID | Minted by | Meaning | Your obligations |
|---|---|---|---|
| `task_id` | **Main** (UUID) | The task, for its whole life | Key your stored task by it. A repeat `task.dispatch` with a `task_id` you already have must **not** create a second task (see [Idempotency](#idempotency)) |
| `agent_task_ref` | You (optional) | Your internal job ID | Return it in the dispatch ack if you have one. Main echoes it back on every later message for that task |
| `context_id` | Main | One user's conversation thread with your service. Several tasks, and live calls, can share it | Optional. Use it to keep conversational memory across follow-ups ("book dinner", then "and a taxi there") |
| `msg_id` | Sender | One message | Unique per sender. Use something short and random |
| `reply_to` | Responder | The `msg_id` being answered | Required on every `task.ack`, `task.nack` and `task.event_ack` |
| `seq` | You | Order of events for one task: 1, 2, 3, … | Increase by one per event per task. Persist it, and never reuse a number |

### Envelope

Every task message, in either direction and over either transport, has this
shape:

```json
{
  "type": "task.dispatch",
  "msg_id": "m-7f3a",
  "task_id": "6a1e2c44-…",
  "context_id": "c-2b90…",
  "agent_task_ref": null,
  "sent_at": "2026-10-05T19:02:11Z",
  "body": { }
}
```

- `task_id` is present on every message except `task.created` (which you send
  before main has minted one).
- Unknown fields must be ignored. An unknown `type` gets
  `task.nack {code:"unsupported"}`.
- Times are ISO 8601 UTC.

### Requests (main → you)

Reply to **every** request with exactly one `task.ack` or `task.nack`, within
your registered **`max_reply_latency_s`** (default 5 s), over the same
transport. For `ws` that means any open task connection from main, not
necessarily the one the request came on. `reply_to` does the matching:

```json
{ "type": "task.ack",  "reply_to": "m-7f3a", "task_id": "6a1e…", "body": { "status": "accepted" } }
{ "type": "task.nack", "reply_to": "m-7f3a", "task_id": "6a1e…",
  "body": { "code": "missing_input", "fields": ["party_size"], "message": "How many people?", "retryable": false } }
```

An ack means "received, valid and recorded". If the work takes longer than
that, ack first and report the outcome later as events.

#### `task.dispatch`: start a task

```json
{
  "type": "task.dispatch",
  "msg_id": "m-7f3a",
  "task_id": "6a1e…",
  "context_id": "c-2b90…",
  "body": {
    "user_id": "4dd16650-…",
    "intent": "Book a table at Nopa for 2 at 7pm tonight",
    "slots": { "restaurant": "Nopa", "time": "19:00", "party_size": 2 },
    "input": {},
    "deadline_at": null,
    "notify_hint_allowed": true,
    "callback": {
      "url": "<MAIN_BASE>/developer/tasks/6a1e…/events",
      "token": "<opaque per-task bearer token>"
    }
  }
}
```

- `intent` is the user's request in plain words. `slots` holds the structured
  values main extracted. Main only sends values the user actually said, so
  if a slot you need is missing, **nack with `missing_input`; don't guess**.
- `input` holds extra structured data, if any.
- `deadline_at` is **optional**, and usually `null`. Without it, a task runs
  until you send a terminal event, or until main sends `task.cancel` or
  `task.close` because the user ended it. A task may legitimately run for
  minutes or for days (watch-style tasks: "tell me when it drops below
  $200").
- When `deadline_at` is set it is **advisory** to you, so you can plan, or
  nack with `cannot_meet_deadline` if you already know you can't make it.
  **Main enforces it.** Main schedules the deadline once you ack. At that
  time it tells the user the task is late and lets you carry on (the
  default), or sends `task.cancel {reason:"deadline"}` if the user said to
  drop the task. See
  [Deadlines](#deadlines). Never treat the deadline as a reason to stop
  reporting: a result you finish late is still delivered.
- `callback` is always present. Store the URL and token with the task. The
  token is valid until 24 h after the task ends.

Ack body: `{"status": "accepted" | "running", "eta_s"?: number, "agent_task_ref"?: string}`.

**You must persist the task (at least `task_id`, its status and next `seq`)
before acking**, so you can still deliver its events, and answer later
requests about it, after you restart.
Don't persist anything when you nack.

#### `task.status`: report current state (optional)

Main **never polls**. Your pushed events are the source of truth. Main
sends `task.status` only as a single attempt to reach you when the user asks
about a task while you look dead (see [Liveness](#liveness)). Supporting it
is optional. Body: `{}`. Ack body:

```json
{ "status": "input_required", "question": "Which restaurant?", "question_seq": 3,
  "progress": { "message": "Checking availability", "pct": 40 },
  "result": null, "last_seq": 3 }
```

Include `result` (or `error`) if the task has finished. Nack with
`unknown_task` if you have no record of it.

#### `task.update`: change a running task

Body: `{"changes": {"time": "20:00"}}`. Ack body: `{"status": "...",
"applied": {"time": "20:00"}}`. Apply all of the changes or none. Nack with
`too_late` if the task already finished or the change can no longer be made
(the booking is confirmed), with `invalid_input`, or with `unsupported`.

#### `task.cancel`: stop a task

Body: `{"reason": "user_cancelled" | "deadline" | "escalated_to_bridge" |
"accept_timeout"}`. Main never cancels tasks because it restarted: tasks are
durable on both sides. Ack body: `{"status":
"cancelled" | "cancelling" | "already_finished", "result"?: {...}}`.

- `cancelling` means you will send a terminal `cancelled` event when done.
- `already_finished` means it completed first. Include the `result`, because
  main still tells the user. This also covers finishing just after a
  deadline: main records the result and tells the user "finished after all".
- Cancel is best-effort. If you already made a side effect (charged a card),
  say so in the result `say`.

#### `task.input`: the user's answer to your question

Body: `{"answer": "Nopa", "question_seq": 3}`, where `question_seq` is the `seq`
of the `input_required` event being answered. Ack body: `{"status":
"running"}`. Nack with `no_question_pending` if you weren't waiting for one.

#### `task.close`: the task is over from main's side

Body: `{"reason": "completed_by_user" | "deleted" | "agent_lost", "by": "user" |
"orchestrator"}`. Sent when the **user** ends a task (marks a watch-style task
done, or deletes it), or with `agent_lost` when main has concluded you lost
the task (see [Liveness](#liveness)). Stop any work and release
resources. Ack body: `{}`. `unknown_task` is fine here, and main treats the
task as closed.

#### `task.delivered`: the user heard your result

Body: `{"via": "live" | "device_wake" | "next_session", "at": "<time>"}`.
Informational: lets you know your result reached the user (live in a call,
after main woke their device, or at their next session). Ack body: `{}`.

#### How main delivers requests

Main writes **every** request to a durable outbox before sending it, so
requests survive dropped connections and main restarts:

- Main sends immediately if it can reach you, then drains any queued requests
  whenever it next connects, retrying with backoff (5 s up to 5 min).
- **No reply within `max_reply_latency_s`** → main resends the **same message,
  with the same `msg_id`**. Expect duplicates, and dedupe by `msg_id` or
  `task_id`.
- **Per task, requests arrive in order.** Main doesn't send the next request
  for a task until you've replied to the previous one, so an `update` never
  overtakes its `dispatch`. Different tasks don't wait on each other.
- A `task.dispatch` nobody acks after 3 sends is abandoned. Main tells the
  user, or tries another service, and queues `task.cancel
  {reason:"accept_timeout"}` in case you got it but your ack was lost. Other
  requests (`update`, `cancel`, `input`, `close`, `delivered`) stay queued
  until you reply, for up to 24 h.

#### Nack codes

| `code` | Meaning | Main does |
|---|---|---|
| `busy` | At capacity right now. Set `retryable: true` | Tries another agent if the user didn't name one, otherwise "Tabletop is busy" |
| `unsupported_intent` | You can't do this kind of task | Declines: "Tabletop can't do that" |
| `missing_input` | Required info is missing. Include `fields[]`, and optionally a `message` phrased as a question | **Asks the user**, then re-dispatches with the **same** `task_id` |
| `invalid_input` | A value is wrong. Include `field` and `message` | Asks the user to correct it |
| `cannot_meet_deadline` | You can't finish by `deadline_at`. Optionally include `earliest_at` | Tells the user, and offers a later deadline or no deadline |
| `unauthorized` | The user or orchestrator isn't allowed | Declines |
| `unknown_task` | No record of `task_id` | For status: marks the task failed ("Tabletop lost track of this") and tells the user. For close: ignores it |
| `too_late` | Update can't be applied any more | Tells the user, and offers cancel and re-dispatch |
| `no_question_pending` | `task.input` when not waiting | Logs it, no user impact |
| `unsupported` | Op or message type not supported | Uses the fallback from the handshake table |

### Events (you → main)

Report every state change as a `task.event`:

```json
{
  "type": "task.event",
  "msg_id": "e-88",
  "task_id": "6a1e…",
  "agent_task_ref": "bk-551",
  "seq": 4,
  "body": {
    "status": "succeeded",
    "result": {
      "say": "Booked Nopa for 2 at 7pm. Confirmation 4471.",
      "output": { "confirmation": "4471" }
    },
    "notify_hint": "normal"
  }
}
```

| `status` | Required body fields | Main does |
|---|---|---|
| `running` | Optional `progress{message, pct}` | Records it. Progress is not spoken |
| `input_required` | `question` (written to be spoken) | Asks the user. The answer comes back as `task.input` with `question_seq` = this event's `seq` |
| `succeeded` | `result{say, output?}` | Tells the user per their notify setting |
| `failed` | `error` (written to be spoken) | Tells the user: "Tabletop couldn't complete that. <error>" |
| `cancelled` | none | Records it. Speaks only if the user didn't cancel it themselves |

- `succeeded`, `failed` and `cancelled` are **terminal**. Send nothing after
  one, and main ignores anything that arrives later.
- `result.say` is spoken verbatim (≤ 2,000 characters, written for the ear).
  `result.output` is structured (≤ 256 KB).
- `notify_hint` is `normal` or `urgent`. Main may use `urgent` to wake the
  user's device, but **only if the user asked to be told**. You can't wake the
  device on your own.

#### Delivery: you push, main never polls

**You ping main when something changes**, above all when the task finishes.
Main doesn't poll, so if you don't push an event, main never learns about it.

1. **The HTTP callback (required).** Use it for every event unless (2)
   applies:

   ```
   POST <MAIN_BASE>/developer/tasks/{task_id}/events
   Authorization: Bearer <callback.token>
   Content-Type: application/json

   <the task.event JSON above>
   ```

   | Status | Body | Meaning |
   |---|---|---|
   | `200` | `{"ok": true, "ack_seq": 4}` | Recorded, **or already recorded** (a duplicate `seq`). Stop resending up to `ack_seq` |
   | `400` | `{"ok": false, "reason": "..."}` | Malformed. Fix it, don't resend unchanged |
   | `401` | | Wrong or expired token |
   | `404` | | Unknown task. Stop resending |

2. **An open WebSocket from main (optional).** If main is connected to you
   anyway (any task connection, not only the one that dispatched the task),
   you may send the event there. Main replies
   `{"type":"task.event_ack","reply_to":"e-88","task_id":"6a1e…","body":{"ack_seq":4}}`.

**Resend every event until it is acked** (`ack_seq` ≥ its `seq`), backing off
from 1 s up to 5 min, for at least 24 h. Store un-acked events durably so they
survive your restart. Main drops duplicates by `seq`, so resending is always
safe. If main is down, keep retrying: the event is delivered when it comes
back.

### Deadlines

Main handles deadlines. You don't need to do anything, but here is what to
expect:

1. Main sends `deadline_at` in the dispatch (or `null`). After **your ack**,
   main schedules a job for that time. Nothing is scheduled for a nacked or
   unacked dispatch.
2. A `task.update` may change or remove `deadline_at`. Ack it as usual, and
   main reschedules.
3. If you send a terminal event first, main cancels the scheduled deadline.
4. If the deadline passes first, by default main **leaves the task running**
   and tells the user it's late. Keep working and keep pushing events as
   normal. Only if the user said to drop the task at the deadline does main
   send `task.cancel {reason:"deadline"}` (reply as for any cancel, with
   `already_finished` and the `result` if you just finished).
5. Waiting on the user (`input_required`) doesn't pause the deadline.
6. You may declare `default_deadline_s` when you register. Main applies it
   to dispatches where the user didn't state a deadline.

### Liveness


Main doesn't poll, and most tasks have no deadline, so main detects a dead service from your
**registration heartbeat**:

- **Heartbeat is required in task mode.** Re-POST `/developer/register` every
  5 min (optional for bridge-only services). If main sees no heartbeat for
  15 min while you have open tasks, it marks them *stalled*, without changing
  their status, and tells the user if they ask (or proactively, once, per
  their notify setting). When your heartbeat resumes the flag clears, and
  your re-sent events catch the tasks up.
- **Reconciliation (SHOULD).** Include the tasks you are still working on:

  ```json
  { "service_id": "tabletop-3a7f", "public_url": "wss://…", "version": "2",
    "open_task_ids": ["6a1e…", "91c0…"] }
  ```

  If main has a task open for you that isn't listed, **and** isn't covered by
  an un-acked event you're still sending, main treats it as lost. It marks the
  task `failed`, tells the user, and sends `task.close {reason:"agent_lost"}`.
  So send the terminal event *before* you drop a task from the list.
- `/developer/unregister` (graceful shutdown) does **not** fail your open
  tasks. They stall until you register again.

### Idempotency

- **Dispatch.** If main doesn't get an ack within your `max_reply_latency_s`
  (for example the connection dropped), it resends the **same**
  `task.dispatch` (same `msg_id` and `task_id`), up to 2 more times. If you already have that `task_id`, reply
  with the same ack and current `status`, and **do not start the work
  again**.
- **Other requests** may also be repeated after a lost reply. Make `cancel`,
  `close`, `delivered` and `input` (same `question_seq`) safe to apply twice.
  Apply `update` only if it changes something.
- Main never re-dispatches a task to a **different** service after you ack it,
  so an acked task never runs on two services at once.

### Timing

| What | Limit |
|---|---|
| Handshake `ack` | 5 s |
| Reply to any task request | Your `max_reply_latency_s` (default 5 s, max 30 s for `ws`) |
| Dispatch resends without an ack | 2 more, then main gives up (or tries another service, if the user didn't name one) |
| Other requests without a reply | Resent with backoff (5 s → 5 min) for up to 24 h |
| Task duration | **No limit by default.** The task ends on your terminal event, or on the user's cancel, complete or delete. An optional `deadline_at` is enforced by main |
| Event resend | Backoff 1 s → 5 min, for at least 24 h |
| Heartbeat (task mode) | Every 5 min. Tasks are flagged stalled after 15 min without one |
| Callback token validity | Until 24 h after the task ends |
| Idle task connection | Closed by main after 60 s (`bye`, `reason:"idle"`) |

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

**Use a ping for "I need the user live now", not for results.** To report a
finished task, send a `task.event` (DRAFT v2). Main then decides how to reach
the user, which may mean waking their device if they asked to be told.

---

## Building a compatible service — minimum checklist

**Bridge mode (v1, live).** A spec-compliant remote service needs to:

1. Accept a WebSocket connection at `<REMOTE_BRIDGE_URL>`.
2. Read the first text frame and verify it's a valid `hello`.
3. Send an `ack` within ~5 seconds (either `accept:true` or `accept:false`).
4. On accept: read audio frames as JSON `{audio, sr}`, write either
   audio frames in the same shape (PCM int16 LE mono base64, sr=24000
   preferred) **or** text frames `{"type":"say","text":"..."}` to have
   main synthesize speech for you. Mix freely.
5. Honor `{"type":"bye"}` from main (respond with bye, close 1000).
6. Optionally: POST to `<MAIN_BASE>/developer/ping/{user_id}` to have main
   initiate a call to your service.

**Task mode (DRAFT v2).** Additionally:

7. Declare your capabilities at registration (`binding`, `modes`,
   `task_ops`, `events` with `callback`, `max_concurrency`,
   `max_reply_latency_s`). Accept `hello {mode:"task"}`, with an ack that may
   only narrow them. Reject unsupported modes with 4405.
8. Answer every task request with one `task.ack` or `task.nack` within your
   `max_reply_latency_s`, with `reply_to` set. Dedupe resent requests by
   `msg_id`.
9. **Persist a task before acking its dispatch**, keyed by main's `task_id`.
10. Treat a repeated `task.dispatch` for a known `task_id` as a no-op that
    returns the same ack.
11. Nack with `missing_input {fields}` instead of guessing a missing value.
12. **Push** every state change, above all completion, as a `task.event` with
    an increasing `seq` to the HTTP callback, and resend until acked. Main
    never polls.
13. Heartbeat via `/developer/register` every 5 min, ideally with
    `open_task_ids`.
14. Handle `task.cancel` and `task.close`, even for tasks dispatched on an
    earlier connection.

The reference implementation in
[`testing/echo_server.py`](testing/echo_server.py) is ~150 lines and does
all of the bridge-mode items, including the optional ping path. Use it as a
starting point. It will gain task mode when main implements v2.

---

## Configuration knobs (on main)

| Env var | Default | Meaning |
|---|---|---|
| `DEVELOPER_WS_REMOTE_BRIDGE_URL` | `ws://localhost:8001/relay` | URL main dials for the bridge |
| `DEVELOPER_WS_BRIDGE_ACK_TIMEOUT_S` | `5.0` | Seconds main waits for `ack` before declaring "no pickup" |
| `DEVELOPER_WS_VAD_RMS` | `20` | Voice-activity RMS gate for utterance segmentation (unrelated to the bridge but tunable per environment) |
| `DEVELOPER_WS_END_SILENCE_SEC` | `2.0` | Seconds of silence after speech to end an utterance |

All read at process start via `python-dotenv` on `<repo>/.env`.

**DRAFT (v2), planned:**

| Env var | Default | Meaning |
|---|---|---|
| `DISPATCH_DEFAULT_REPLY_LATENCY_S` | `5` | Reply wait for services that don't register `max_reply_latency_s` |
| `DISPATCH_OUTBOX_SWEEP_S` | `30` | How often queued requests are retried |
| `DISPATCH_OUTBOX_TTL_H` | `24` | How long non-dispatch requests stay queued |
| `DISPATCH_DISPATCH_RESENDS` | `2` | Same-`task_id` resends before giving up |
| `DISPATCH_STALL_AFTER_S` | `900` | Heartbeat silence before a service's open tasks are flagged stalled |
| `DISPATCH_IDLE_CLOSE_S` | `60` | Idle task-connection reaping |
| `DISPATCH_CALLBACK_TOKEN_TTL_H` | `24` | Callback token lifetime after the task ends |
