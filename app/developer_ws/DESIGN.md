# developer_ws — design overview

A self-contained voice-loop subsystem hung off `/ws/developer/{user_id}` on main. One
session corresponds to one mic-bearing client. Audio flows through a Pipecat
`Pipeline` of frame processors (STT → LLM → TTS) by default; a single Gemini tool can
swap the local pipeline out for a direct relay to a remote WebSocket service ("the
bridge"). The remote can mix raw audio frames with `{"type":"say","text":"..."}`
text frames; main synthesizes any text it receives via the same Piper service that
backs the assistant.

For wire-protocol details against the bridge see [BRIDGE_PROTOCOL.md](BRIDGE_PROTOCOL.md).

---

## Component map

```
                       ┌─────────────────────────── app/developer_ws ────────────────────────────┐
                       │                                                                          │
   /ws/developer/{id} ─►  endpoint.py ──► SpeechPipeline (pipeline.py)                            │
                       │       │                  │                                               │
                       │       │                  ▼                                               │
                       │       │           ┌──────────────── Pipecat Pipeline ────────────────┐   │
                       │       │           │                                                  │   │
                       │       │           │  SessionSource ─► BridgeGate ─► VoskUtteranceSTT │   │
                       │       │           │       │                              │           │   │
                       │       │           │       ▼                              ▼           │   │
                       │       │           │  (audio gate)                CustomGeminiLLM     │   │
                       │       │           │       ▲                              │           │   │
                       │       │           │       │                              ▼           │   │
                       │       │           │       │              PiperTTS ◄──── (text)       │   │
                       │       │           │       │                  │                       │   │
                       │       │           │       │                  ▼                       │   │
                       │       │           │       │           AudioIOSink                    │   │
                       │       │           └───────┼──────────────────┼────────────────────── ┘   │
                       │       ▼                   │                  ▼                           │
                       │   UtteranceBuffer      RemoteAudioBridge  AudioIO ──► downlink WS        │
                       │   (silence timer)      (bridge.py)        (audio_io.py)                  │
                       │       │                   │                  ▲                           │
                       │       └─ silence fire ────┘                  └── bridge.add_playback_pcm │
                       │           signals user-stopped                                           │
                       └──────────────────────────────────────────────────────────────────────────┘
                                            ▲
                                            │
                          POST /developer/ping/{id}      ◄── any service
```

Two flow modes coexist:

- **Local mode** — incoming audio enters the Pipecat pipeline. The STT processor
  accumulates audio between `UserStartedSpeakingFrame` and `UserStoppedSpeakingFrame`
  signals (the latter fired by `endpoint.py` when Silero VAD confirms end of
  speech (`vad.py`, primary), the fallback silence timer expires, or
  `turn_complete:true` arrives), runs Vosk, emits a `TranscriptionFrame`, and the
  rest of the pipeline runs LLM → TTS → downlink.
- **Bridge mode** — `BridgeGateProcessor` swallows uplink audio while
  `bridge.active`, and the endpoint short-circuits the pipeline entirely for those
  frames, forwarding them to the remote service. Remote frames (`audio` or
  `say`-text) flow back via `AudioIO`/the pipeline's TTS path.

---

## Per-file responsibilities

### Entry & lifecycle
- [`__init__.py`](__init__.py) — package facade. Re-exports
  `developer_websocket_endpoint`, `preload_vosk_model`, and `preload_piper_voice`
  for `app/main.py` to wire into FastAPI.
- [`endpoint.py`](endpoint.py) — the WebSocket handler. Constructs the per-session
  objects, registers the pipeline with `registry`, runs the receive loop (parses
  audio/turn-complete/interrupt JSON frames and pushes them into the pipeline),
  drains/closes on socket teardown, and dumps the scratchpad.
- [`registry.py`](registry.py) — process-local map of `user_id → SpeechPipeline`.
  Used by the HTTP ping endpoint in `main.py` to push events into a live session.

### The voice pipeline
- [`pipeline.py`](pipeline.py) — `SpeechPipeline` builds and drives a Pipecat
  `Pipeline` + `PipelineTask` + `PipelineRunner` per session. Exposes
  `feed_audio(pcm)`, `signal_user_stopped()`, `interrupt()`, `on_service_ping()`,
  and `inject_assistant_text(text)` for the endpoint and registry callers. Tool
  handlers are registered on the LLM service in `_register_tools`; the bridge's
  `on_say_text` callback is wired to `inject_assistant_text` so remote text
  frames go through the same TTS path as assistant replies.
- [`pipecat_bits.py`](pipecat_bits.py) — the five custom `FrameProcessor`s that
  make up the pipeline: `SessionSource` (passthrough at the head),
  `BridgeGateProcessor` (defensive audio gate in bridge mode),
  `VoskUtteranceSTTProcessor` (utterance-level Vosk STT triggered by
  `UserStoppedSpeakingFrame`), `PiperTTSProcessor` (synthesizes `TextFrame` and
  `TTSSpeakFrame` via Piper, emits `TTSAudioRawFrame`), and `AudioIOSinkProcessor`
  (sinks audio into `AudioIO` and translates pipeline lifecycle frames into
  AudioIO calls).
- [`pipecat_llm.py`](pipecat_llm.py) — `CustomGeminiLLMService`, a subclass of
  Pipecat's `LLMService` that owns the Gemini call (uses `agents/gemini_client.py`
  directly). Holds the per-session `LLMContext`, dispatches function calls to
  registered handlers, and exposes `on_message_added` so the scratchpad can
  mirror conversation history without being authoritative.
- [`vad.py`](vad.py) — Silero VAD endpointing, the *primary* end-of-turn signal.
  Wraps pipecat's `SileroVADAnalyzer` (ONNX model bundled with pipecat-ai);
  `endpoint.py` feeds it every non-bridge batch and closes the turn on the
  STOPPED transition — `DEVELOPER_WS_SILERO_STOP_SECS` (default 0.8 s) of
  confirmed non-speech instead of the 2 s RMS silence window. Tunables:
  `DEVELOPER_WS_SILERO_{CONFIDENCE,START_SECS,STOP_SECS,MIN_VOLUME}`; disable
  with `DEVELOPER_WS_USE_SILERO_VAD=0` (or when onnxruntime is missing, it
  degrades to timer-only automatically).
- [`utterance.py`](utterance.py) — end-of-utterance silence-timer state machine
  (`DEVELOPER_WS_END_SILENCE_SEC`). Re-arms on every batch that clears the VAD
  threshold (`DEVELOPER_WS_VAD_RMS`). When the gap expires, fires
  `pipeline.signal_user_stopped`. Audio accumulation lives in the STT processor
  now; this file is just the timer — kept as the fallback endpointing path
  behind `vad.py` (fires if the VAD never confirms speech in an utterance).
- [`stt.py`](stt.py) — Vosk STT helper, preloaded during app startup. Called from
  `VoskUtteranceSTTProcessor`.
- [`tts.py`](tts.py) — Piper TTS helper, preloaded during app startup. Called from
  `PiperTTSProcessor`.
- [`tools.py`](tools.py) — OpenAI-shaped tool schemas. Currently only
  `start_remote_audio_bridge`. Forwarded to Gemini via `tools_schema=`; handlers
  live in `pipeline._register_tools`.

### The bridge
- [`bridge.py`](bridge.py) — outbound WebSocket to a remote service. Owns the
  hello/ack handshake (see `BRIDGE_PROTOCOL.md`), frame counters (`sent`, `recv`,
  `say`), and two callbacks the pipeline wires up: `on_remote_close` (fires when
  the remote — not us — closes the WS) and `on_say_text` (fires per remote
  `{"type":"say","text":...}` frame, routed to `pipeline.inject_assistant_text`).
  `bridge.start(...)` returns a `BridgeStartResult` describing exactly what
  happened (no pickup, rejected, protocol error, ok, ...).

### Bookkeeping
- [`scratchpad.py`](scratchpad.py) — per-session turn log printed to stdout on
  socket close. **Not** authoritative — the LLMContext inside
  `CustomGeminiLLMService` is. The pipeline mirrors message-additions into the
  scratchpad via the `on_message_added` callback so the stdout dump is independent
  of log level.
- [`audio_io.py`](audio_io.py) — uplink Opus decode (or raw passthrough), downlink
  Opus encode with frame coalescing, `mark_turn_complete` to flush residuals on
  `BotStoppedSpeakingFrame`.

### Testing
- [`testing/echo_server.py`](testing/echo_server.py) — reference remote service.
  Speaks the bridge protocol, optionally pings main on startup
  (`--ping <user_id>`), can reject for testing the rejection path
  (`ECHO_REJECT_ALL=1`).
- [`testing/run_full_test.py`](testing/run_full_test.py) — orchestrator. Brings
  up main → client → echo in order, each in its own console, with a watchdog
  scoped to the client process (main/echo can die without ending the run).

---

## Session lifecycle

```
 1.  Client opens /ws/developer/{user_id}
 2.  endpoint.py creates AudioIO, UtteranceBuffer (silence timer), Scratchpad,
     RemoteAudioBridge, SpeechPipeline. SpeechPipeline builds the Pipecat
     Pipeline/Task/Runner and spawns the runner as a background asyncio task.
 3.  registry.register(user_id, pipeline)
 4.  Receive loop:
       - incoming {audio} → pipeline.feed_audio(pcm); first energetic batch
         in a new utterance fires UserStartedSpeakingFrame
       - has_speech batch → utterance.arm_timer(pipeline.signal_user_stopped)
       - incoming {turn_complete: true} → pipeline.signal_user_stopped()
       - incoming {interrupt} or text containing "stop" →
         pipeline.interrupt() (tear down bridge if active, queue
         InterruptionFrame, clear AudioIO playback)
       - bridge.active → uplink bytes go to bridge.send_uplink_pcm, not the
         pipeline (the BridgeGate processor would catch them too, but the
         endpoint short-circuits earlier for efficiency)
 5.  Silence timer fires (or turn_complete arrives) →
       UserStoppedSpeakingFrame → VoskUtteranceSTTProcessor transcribes →
       TranscriptionFrame → CustomGeminiLLMService calls Gemini → either:
         - LLMTextFrame → PiperTTSProcessor → TTSAudioRawFrame → AudioIOSink
         - FunctionCall → registered handler runs (e.g. opens bridge); handler
           pushes a TTSSpeakFrame with a deterministic ack and returns
           run_llm=False to skip the LLM follow-up
 6.  Socket closes → endpoint.py drains:
       - registry.unregister(user_id)
       - utterance.bump_arm_id() cancels any pending silence timer
       - pipeline.drain() fires signal_user_stopped if mid-utterance
       - pipeline.close() cancels the Pipecat task + closes the bridge
       - scratchpad printed
```

---

## Concurrency model

- **One Pipecat task per session** — the pipeline runs in a background asyncio
  task spawned by `SpeechPipeline.start()`. Frame processing within the pipeline
  is serial per-processor; the pipeline's frame queue gives ordering.
- **`_announce_lock` in SpeechPipeline** — guards multi-step service-ping flows
  (announce → bridge.start → success/fail ack) so a concurrent caller can't
  interleave with the announcement → dial sequence.
- **Silence timer is a task, not a callback** — `utterance.arm_timer` cancels
  any prior watcher and schedules a new one with a fresh `arm_id`. A stale
  watcher (whose `arm_id` changed) stands down without firing.
- **Bridge `_recv_loop` runs as its own task** — `close()` cancels it;
  `_self_closing` prevents the loop's finally block from firing
  `on_remote_close` when *we* initiated the close.
- **Tool handlers run inside the LLM service's task** — Pipecat's
  `run_function_calls` invokes registered handlers concurrently (or sequentially
  per LLMService config). Handlers can push frames via `params.llm.push_frame`
  and complete the call via `params.result_callback(...)`.

---

## Configuration

Environment variables (read at process start via `python-dotenv` on `<repo>/.env`):

| Var | Default | Effect |
|---|---|---|
| `VOSK_MODEL_PATH` | — | Path to the Vosk model directory. Required for STT. |
| `PIPER_MODEL_PATH` | `piper_voices/en_US-amy-medium.onnx` | Path to the Piper voice. |
| `GEMINI_TEXT_MODEL` | `gemini-3-flash-preview` | Gemini model id for `generateContent`. |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Auth for the Gemini SDK. |
| `DEVELOPER_WS_VAD_RMS` | `20` | RMS gate that decides if a batch contains speech (re-arms silence timer). |
| `DEVELOPER_WS_MIN_INPUT_RMS` | `20` | Whole-utterance RMS gate that skips low-energy flushes. |
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

- **A new tool Gemini can call** → add a schema to [tools.py](tools.py), then a
  handler registered via `llm.register_function(name, handler)` in
  `SpeechPipeline._register_tools` (pipeline.py).
- **A new control message between client and main** → add a clause in
  `endpoint._receive_loop`.
- **A change to how utterances are segmented** → [vad.py](vad.py) (Silero
  endpointing) and [utterance.py](utterance.py) (fallback silence timer)
  (`has_speech`, `arm_timer`, threshold env vars) and/or the
  start/stop-speaking signal-points in `endpoint._handle_audio`.
- **A new pipeline stage (e.g. translation between STT and LLM)** → add a
  `FrameProcessor` in [pipecat_bits.py](pipecat_bits.py) and insert it into the
  `Pipeline([...])` list in `pipeline.py`.
- **A change to the bridge protocol** → [bridge.py](bridge.py) +
  [testing/echo_server.py](testing/echo_server.py) +
  [BRIDGE_PROTOCOL.md](BRIDGE_PROTOCOL.md).
- **A new way for external systems to trigger something on main** → add an HTTP
  route in [`app/main.py`](../main.py) and a method on `SpeechPipeline` it can
  call after a `registry.get(user_id)` lookup.
