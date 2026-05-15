# developer_ws — design overview

A self-contained voice-loop subsystem hung off `/ws/developer/{user_id}` on main. One
session corresponds to one mic-bearing client. Audio flows through a fixed pipeline
(STT → LLM → TTS) by default; a single Gemini tool can swap the local pipeline out for
a direct relay to a remote WebSocket service ("the bridge").

For wire-protocol details against the bridge see [BRIDGE_PROTOCOL.md](BRIDGE_PROTOCOL.md).

---

## Component map

```
                       ┌──────────────────────────────────────────────┐
                       │              app/developer_ws                │
                       │                                              │
   /ws/developer/{id} ─►  endpoint.py  ─► UtteranceBuffer  ─► Pipeline ─► STT (stt.py)
                       │      │           (utterance.py)        │          │
                       │      │                                 │          ▼
                       │      ▼                                 │       Scratchpad
                       │   AudioIO                              │       (scratchpad.py)
                       │   (audio_io.py)                        │          │
                       │      ▲                                 │          ▼
                       │      │                                 │        LLM (llm.py + tools.py)
                       │      │                                 │          │
                       │      │  ◄────── TTS (tts.py) ◄─────────┤          ▼
                       │      │                                 │       Tool dispatch
                       │      │                                 ▼
                       │      └────── RemoteAudioBridge (bridge.py) ◄── handshake ──► remote WS
                       │                                                              service
                       └──────────────────────────────────────────────┘
                                            ▲
                                            │
                          POST /developer/ping/{id}      ◄── any service
```

Two flow modes live in this subsystem:

- **Local mode** — incoming audio is segmented into utterances and run through STT → LLM →
  TTS. The user "talks to" the assistant.
- **Bridge mode** — STT/LLM/TTS are bypassed. Frames are forwarded verbatim to a remote
  WebSocket service, and the remote's frames are queued for playback. Gemini opens the
  bridge by calling `start_remote_audio_bridge` (or via the HTTP ping path); main closes
  it on a `bye` from the remote, on disconnect, or when the user says "stop".

---

## Per-file responsibilities

### Entry & lifecycle
- [`__init__.py`](__init__.py) — package facade. Re-exports `developer_websocket_endpoint`
  and `preload_vosk_model` for `app/main.py` to wire into FastAPI.
- [`endpoint.py`](endpoint.py) — the WebSocket handler. Constructs the per-session objects,
  registers the pipeline with `registry`, runs the receive loop, drains/flushes on close,
  and dumps the scratchpad.
- [`registry.py`](registry.py) — process-local map of `user_id → SpeechPipeline`. Used by
  the HTTP ping endpoint in `main.py` to push events into a live session.

### The voice pipeline
- [`pipeline.py`](pipeline.py) — single-flight `SpeechPipeline.flush()`. Guards STT → LLM →
  TTS with an asyncio lock and short-circuits at each stage if the WebSocket has died.
- [`utterance.py`](utterance.py) — speech buffer + end-of-utterance silence timer
  (`DEVELOPER_WS_END_SILENCE_SEC`). Re-arms on every batch that clears the VAD threshold
  (`DEVELOPER_WS_VAD_RMS`). When the gap exceeds the threshold, fires `pipeline.flush()`.
- [`stt.py`](stt.py) — Vosk STT, preloaded during app startup.
- [`llm.py`](llm.py) — `gemini_reply(...)`. Wraps the sync Gemini SDK in `to_thread`. The
  system prompt teaches Gemini how to read bridge state out of scratchpad history.
- [`tools.py`](tools.py) — OpenAI-shaped tool schemas. Currently only
  `start_remote_audio_bridge`.
- [`tts.py`](tts.py) — pyttsx3 + resample to 24 kHz int16 mono.

### The bridge
- [`bridge.py`](bridge.py) — outbound WebSocket to a remote service. Owns the
  hello/ack handshake (see `BRIDGE_PROTOCOL.md`), frame counters, and the
  `_on_remote_close` callback that the pipeline uses to notify the user when the remote
  hangs up. `bridge.start(...)` returns a `BridgeStartResult` describing exactly what
  happened (no pickup, rejected, protocol error, ok, ...).

### Bookkeeping
- [`scratchpad.py`](scratchpad.py) — per-session turn log. Feeds prior turns into each
  Gemini call (`history_messages()`) and is printed on socket close.
- [`audio_io.py`](audio_io.py) — uplink Opus decode (or raw passthrough), downlink Opus
  encode with frame coalescing, `mark_turn_complete` to flush residuals.

### Testing
- [`testing/echo_server.py`](testing/echo_server.py) — reference remote service. Speaks the
  bridge protocol, optionally pings main on startup (`--ping <user_id>`), can reject for
  testing the rejection path (`ECHO_REJECT_ALL=1`).
- [`testing/run_full_test.py`](testing/run_full_test.py) — orchestrator. Brings up main →
  client → echo in order, each in its own console, with a watchdog scoped to the client
  process (main/echo can die without ending the run).

---

## Session lifecycle

```
 1.  Client opens /ws/developer/{user_id}
 2.  endpoint.py creates AudioIO, UtteranceBuffer, Scratchpad, RemoteAudioBridge, Pipeline
 3.  registry.register(user_id, pipeline)
 4.  Receive loop:
       - incoming {audio} → utterance.extend(); if has_speech, arm silence timer
       - incoming {turn_complete: true} → flush immediately
       - incoming {interrupt} or text containing "stop" → tear down bridge if active,
         clear utterance, interrupt audio playback
       - bridge.active → uplink bytes go to bridge, not the utterance buffer
 5.  Silence timer fires → pipeline.flush() runs STT → LLM → TTS (or a tool)
       - On tool call: pipeline opens the bridge, scratchpad records the open
       - On bridge remote close: pipeline records the close + speaks an announcement
 6.  Socket closes → endpoint.py drains:
       - registry.unregister(user_id)
       - any remaining buffered audio is flushed (45s hard timeout)
       - bridge is closed
       - scratchpad printed
```

---

## Concurrency model

- **One pipeline lock per session** — `pipeline._lock` ensures at most one STT/LLM/TTS
  turn is in flight. Service-ping announcements also take this lock so they can't talk
  over a turn that's mid-flight.
- **One utterance lock per session** — `utterance._lock` only protects the buffer bytes;
  it is never held across STT/LLM/TTS, so flushes don't block frame intake.
- **Silence timer is a task, not a callback** — `arm_timer` cancels any prior watcher and
  schedules a new one with a fresh `arm_id`. A stale watcher (whose arm_id changed) stands
  down without firing.
- **`schedule_flush()` is fire-and-forget** — wraps `flush()` in a task and logs any
  exception. Callers (silence timer, `turn_complete` handler) don't await it.
- **Bridge `_recv_loop` runs as its own task** — `close()` cancels it; `_self_closing`
  prevents the loop's finally block from firing `on_remote_close` when *we* initiated the
  close.

---

## Configuration

Environment variables (read at process start via `python-dotenv` on `<repo>/.env`):

| Var | Default | Effect |
|---|---|---|
| `VOSK_MODEL_PATH` | — | Path to the Vosk model directory. Required. |
| `DEVELOPER_WS_VAD_RMS` | `20` | RMS gate that decides if a batch contains speech. |
| `DEVELOPER_WS_MIN_INPUT_RMS` | `20` | Whole-utterance RMS gate that skips a low-energy flush. |
| `DEVELOPER_WS_END_SILENCE_SEC` | `2.0` | Silence duration that ends an utterance. |
| `DEVELOPER_WS_REMOTE_BRIDGE_URL` | `ws://localhost:8001/relay` | Where the bridge dials. |
| `DEVELOPER_WS_BRIDGE_ACK_TIMEOUT_S` | `5.0` | Max wait for remote ack. |
| `DEVELOPER_GEMINI_SYSTEM_INSTRUCTION` | — | Override Gemini system prompt. |

Echo-server-only knobs:

| Var | Default | Effect |
|---|---|---|
| `ECHO_SERVER_PORT` | `8001` | Listening port. |
| `ECHO_SERVICE_ID` | random `echo-server-<8 hex>` | Identifier in pings + acks. |
| `ECHO_REJECT_ALL` | `0` | If truthy, every call is rejected (4403). |
| `ECHO_REJECT_REASON` | `service unavailable` | Reason returned with the rejection ack. |
| `ECHO_HELLO_TIMEOUT_S` | `5.0` | How long echo waits for hello before closing 1002. |

---

## Where to look for things

- **A new tool Gemini can call** → add a schema to [tools.py](tools.py), then a branch in
  `pipeline._handle_tool_call`.
- **A new control message between client and main** → add a clause in
  `endpoint._receive_loop`.
- **A change to how utterances are segmented** → [utterance.py](utterance.py)
  (`has_speech`, `arm_timer`, threshold env vars).
- **A change to the bridge protocol** → [bridge.py](bridge.py) +
  [testing/echo_server.py](testing/echo_server.py) + [BRIDGE_PROTOCOL.md](BRIDGE_PROTOCOL.md).
- **A new way for external systems to trigger something on main** → add an HTTP route in
  [`app/main.py`](../main.py) and a method on `SpeechPipeline` it can call after a
  `registry.get(user_id)` lookup.
