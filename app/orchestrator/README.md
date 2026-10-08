# `app/orchestrator/`

The orchestrator: the voice session the pin and the test website talk to
(`/ws/developer/{user_id}`), plus everything it uses to decide what to do with
a request. It routes to agents, starts live calls, dispatches and tracks
background tasks, and checks every tool call before it runs.

- **Spec:** [`ORCHESTRATOR_V2_TOOL_CALLS.md`](../../ORCHESTRATOR_V2_TOOL_CALLS.md)
  (routing, dispatch, tool-call checks) and
  [`ORCHESTRATOR_V2_VOICE.md`](../../ORCHESTRATOR_V2_VOICE.md) (voice
  architecture, latency).
- **Agent wire protocol:** [`BRIDGE_PROTOCOL.md`](BRIDGE_PROTOCOL.md).
- **Voice-session internals** (frame flow, lifecycle, env vars):
  [`DESIGN.md`](DESIGN.md).

The package used to be `app/developer_ws/`. For compatibility the client URL
`/ws/developer/…`, the `DEVELOPER_WS_*` env vars and the `developer_ws`
logger name are unchanged.

## Layout

```
orchestrator/
├── README.md               this file
├── DESIGN.md               voice-session design: pipeline, frames, lifecycle, env vars
├── BRIDGE_PROTOCOL.md      wire protocol for agents (live calls + background tasks)
├── BUILD_SERVICE_PROMPT.md prompt for generating a compatible agent service
├── __init__.py             lazy exports (importing a submodule never loads Pipecat)
│
│   ── voice session ──────────────────────────────────────────────
├── endpoint.py             /ws/developer/{user_id}: accept, wire the session, receive loop, VAD, echo guard, teardown
├── pipeline.py             SpeechPipeline: builds the Pipecat pipeline, and the tool handlers (routing, tasks, read-backs)
├── tools.py                the five tool schemas Gemini sees
├── pipecat_llm.py          CustomGeminiLLMService: owns the Gemini call and conversation history
├── pipecat_bits.py         custom FrameProcessors: STT, TTS, bridge gate, thinking cue, audio sink
├── audio_io.py             downlink audio (Opus/PCM), playback tracking, echo-guard horizon
├── stt.py                  Vosk speech-to-text, with a pool of warm recognizers
├── tts.py                  Piper text-to-speech
├── vad.py                  Silero VAD end-of-turn detection
├── utterance.py            silence timer and barge-in detection (VAD fallback)
├── thinking_cue.py         the soft "thinking" pulse during the reply gap
├── bridge.py               live call (Protocol 1): relay audio to an agent's WebSocket
├── registry.py             in-process map user_id → live SpeechPipeline (for HTTP pings)
├── scratchpad.py           per-session transcript dump on close
├── speech.py               every fixed line the orchestrator speaks
│
│   ── routing decisions (pure Python) ───────────────────────────
├── routing/
│   ├── router.py           in-memory agent snapshot; resolves names/intents to matched/ambiguous/none/wrong_mode
│   ├── embeddings.py       Gemini embeddings for unnamed requests (300 ms budget, lexical fallback)
│   ├── gate.py             validation gate: required slots, grounding, self-corrections
│   └── policy.py           connect vs dispatch, indirect routing, notify, quiet hours/wake limits
│
│   ── background tasks (Protocol 2, no Pipecat) ─────────────────
├── tasks/
│   ├── protocol.py         message types, A2A task states, nack codes, envelope builders
│   ├── store.py            Postgres: agent tasks in `tasks`, `agent_outbox`, `jobs`
│   ├── link.py             agent connections (on demand, one per agent) and outbox delivery/resends
│   ├── service.py          TaskService: dispatch, events, CRUD, delivery, deadlines, liveness
│   └── routes.py           HTTP: /api/dispatch/*, agent event callback, internal deadline hook, search
│
└── testing/
    ├── echo_server.py      reference agent for live calls (echoes audio)
    ├── task_agent.py       reference agent for background tasks (used by the tests)
    └── run_full_test.py    local harness: main + echo agent + mic client
```

## How a request flows

```
mic → endpoint → pipeline (VAD → stt → Gemini via pipecat_llm, with tools)
                    │
                    ├─ answer ─────────────────────────────────→ tts → speaker
                    └─ tool call → pipeline handler
                                     ├─ routing/router   which agent?
                                     ├─ routing/policy   live call or task? answer instead?
                                     ├─ routing/gate     were the details actually said?
                                     ├─ speech           what to say (fixed line)
                                     ├─ bridge           live call (Protocol 1)
                                     └─ tasks/service    background task (Protocol 2)
                                          ├─ tasks/store  rows in Postgres
                                          └─ tasks/link   outbox → agent WebSocket
agent results → tasks/routes (HTTP callback) → tasks/service → pipeline (spoken, or held)
```

## Dependencies between parts

- `routing/` and `tasks/` never import the voice session, so the HTTP routes,
  the agent registry (`app/agents_registry.py`) and tests can use them without
  Pipecat.
- `pipeline.py` is the only place that combines them with the voice stack.
- The schema these modules read and write comes from
  [`deploy/sql/`](../../deploy/sql) (applied by `deploy/migrate.sh`); nothing
  here creates tables.

## Tests

`test/app/orchestrator/` holds the router, gate, policy, task service,
pipeline-handler and worker tests (Postgres via `pgserver`).
`test/app/developer/` holds the voice-session unit tests (STT pool, VAD,
thinking cue).
