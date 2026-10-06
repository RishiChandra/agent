# Orchestrator v2: voice architecture and reliable tool calls

**Status:** proposal for review. Refines the earlier
`ORCHESTRATOR_V2_DESIGN.md` draft. That draft covered eight proposals. This
version narrows the review to the **two decisions that set the orchestrator's
quality ceiling** and moves the rest (router, protocol v2, dispatcher,
registry) to an appendix as the foundation both decisions build on.

| # | Decision | Question | Part |
|---|---|---|---|
| **1** | Voice architecture | Should the orchestrator's voice loop stay a text LLM between STT and TTS, or move to a multimodal speech LLM (MMLLM)? If an MMLLM, which shape: hosted realtime, frontend/backend split, or a self-hosted Qwen Thinker–Talker? | I |
| **2** | Tool-call reliability | How do we make sure the orchestrator calls a tool only when it should, with the right agent and real arguments, and otherwise asks, declines or answers? | II |

The two decisions are coupled. The orchestrator's whole job is tool decisions:
route, dispatch, ask, decline, answer. The voice architecture decides **where**
that decision is made and **whether we can intercept it** before anything is
spoken or executed. Part III covers how they interact. The recommended order
is to build the Part II evaluation first, then use it to choose the Part I
option.

**Sourcing.** The When2Call claims in Part II come from the paper itself (arXiv
HTML version, read October 2026). They replace the earlier draft's *(recall)*
items. Qwen3-Omni architecture facts come from its technical report. Hosted-API
facts come from vendor documentation and secondary write-ups, and are marked
*(verify)* where only a secondary source was available. All sources are listed
at the end.

---

## Contents

0. Summary of recommendations
1. Context: the orchestrator today

**Part I: Decision 1, MMLLM vs LLM + STT/TTS**

2. What the orchestrator needs from its voice loop
3. The option space
4. MMLLM options in detail
5. Deep dive: Qwen Omni Thinker–Talker
6. Scoring the options
7. Recommendation and decisions

**Part II: Decision 2, reliable tool calls**

8. What When2Call found
9. What it means for the orchestrator
10. Orchestrator modes and the tool calls behind them
11. The optimisation space: a layered defence
12. Evaluation: a When2Call-style voice eval set
13. Recommendation and decisions

**Part III and appendices**

14. How the two decisions interact
15. Appendix A: the foundation (router, protocol v2, dispatcher, registry)
16. Appendix B: full decision checklist
17. Sources

---

## 0. Summary of recommendations

1. **Measure before choosing.** Add per-stage timestamps to the current loop
   (VAD stop, STT final, LLM first and last token, TTS first audio, first
   downlink byte). Nothing has been measured, and every Part I trade-off
   depends on which hop dominates.
2. **Build the tool-call eval first (§12).** It is the yardstick for both
   decisions. When2Call shows that benchmark scores on "correct calls" don't
   predict "should I call at all", and Full-Duplex-Bench-v3 shows speech models
   lose accuracy on real spoken input. We need our own numbers.
3. **Decision 1:** stream the current cascade now (cheap, keeps exact speech).
   Then prototype a **frontend/backend split** in which a speech model holds the
   conversation and a text LLM backend owns every tool decision. Keep
   **self-hosted Qwen3-Omni** as the option that uniquely lets us edit the
   Thinker's text before the Talker speaks it, if exact in-voice acks or data
   residency become requirements.
4. **Decision 2:** don't rely on the model. Layer the defences: a smaller tool
   surface, an explicit four-way prompt, a **deterministic validation gate**
   between the model's tool call and its execution (router decisions, schema
   checks, argument grounding, read-back for side effects), then model
   selection by eval. Use preference training (RPO/DPO, not plain SFT) only if
   we ever self-host the decision model.
5. **Orchestrator modes (§10):** direct and indirect routing over Protocol 1,
   a code-side policy for connecting versus dispatching, and Protocol 2 tasks
   with full CRUD. Tasks live in the single `tasks` table (`agent_tasks` dropped; scheduling optional via `is_scheduled`), carry a `notify` flag
   (`device` wakes the ESP32, `next_session`, `silent`), and the owning agent
   is always told how its task ended.

---

## 1. Context: the orchestrator today

The orchestrator is the `/ws/developer/{user_id}` voice session in
`app/developer_ws/`. It is not Kairos (`/ws/{user_id}`), which already runs on
a native-audio Gemini model and is just another agent behind the router.

```
mic ─► Opus decode ─► Silero VAD (end of turn after ~0.8 s silence) ─► Vosk STT (finalize)
    ─► Gemini text (gemini-3-flash-preview, one non-streamed generateContent call, with tools)
    ─► whole reply text ─► Piper TTS ─► Opus encode ─► downlink
```

Facts from the code that matter for both decisions:

- **The Gemini call isn't streamed.** `CustomGeminiLLMService._call_gemini`
  waits for the full response, so Piper can't start until Gemini finishes. The
  in-progress "thinking cue" (`thinking_cue.py`) masks this gap with a soft
  pulse but doesn't shorten it.
- **Handlers speak deterministically.** Tool handlers emit fixed sentences
  ("Connecting you to *Name* now.", "Did you mean A or B?") with
  `run_llm=False`. Some of these strings carry state that the system prompt
  keys off.
- **Tool results never reach Gemini.** `agents/gemini_client._messages_to_contents`
  drops `tool`-role messages, so the model can't reason over a tool result.
- **Vosk garbles agent names.** "Kairos" becomes "cut in", "cairo's" or "kai
  ross". The router's phonetic matching exists because of this (Appendix A).
- **Only text leaves the VM.** Vosk and Piper run locally on a 2-OCPU, 12 GB
  ARM VM with no GPU, in `us-sanjose-1` (`OCI_INFRASTRUCTURE.md`).

The foundation built on branch `claude/orchestrator-upgrade-routing-o7cnph`
(router, protocol v2, task dispatcher; Appendix A) is assumed by both parts. It
turns "every agent is a tool" into a bounded tool surface: `find_agents`,
`dispatch_task`, `start_remote_audio_bridge`, plus task management.

---

# Part I: Decision 1, MMLLM vs LLM + STT/TTS

## 2. What the orchestrator needs from its voice loop

An orchestrator has requirements a single conversational agent doesn't. These
are the criteria in §6.

| # | Requirement | Why it is orchestrator-specific |
|---|---|---|
| R1 | **Tool-decision accuracy on spoken input** | A wrong call means a real task dispatched to a real agent, possibly with side effects (Part II) |
| R2 | **Interceptable decisions** | We want to validate a tool call, or rewrite what will be said, *before* it is executed or spoken (§11) |
| R3 | **Exact speech for state-carrying lines** | Bridge acks, disambiguation questions, refusals and task results are fixed strings, and the prompt keys off some of them |
| R4 | **Bridge handoff** | Mic audio must be redirected to a remote agent and then resumed, with the model told what happened |
| R5 | **Async result delivery** | Task results arrive minutes later and must be spoken when the user is idle, held while bridged |
| R6 | **Name recognition** | Agent names are out of vocabulary for general ASR |
| R7 | **Latency** | Time from end of user turn to first audio |
| R8 | **Turn-taking** | Endpointing, barge-in, ignoring backchannels ("uh-huh") |
| R9 | **One voice** | Assistant speech and relayed agent `say` text ideally share a voice |
| R10 | **Cost, privacy, infra** | Today: free local STT/TTS, only text leaves the VM, no GPU |

## 3. The option space

Five architectures, ordered from "most text" to "most speech":

| | Option | Speech in | Decision made in | Speech out | Who speaks fixed lines |
|---|---|---|---|---|---|
| **A** | **Streamed cascade** | Streaming STT (Vosk, or Voxtral/Kyutai STT with a name-biased vocabulary) | Text LLM | TTS, sentence by sentence as tokens stream | TTS, exact |
| **B** | **Half-cascade** | Audio-in LLM (Ultravox, or an Omni model with text-only output) | Same model, in text | TTS | TTS, exact |
| **C** | **Native speech-to-speech (hosted)** | Provider session | Provider model | Provider model | The model, which may paraphrase |
| **D** | **Frontend/backend split** | Speech model (frontend) | Text LLM backend that owns router and dispatcher | Speech model | Frontend, with backend text injected |
| **E** | **Self-hosted Thinker–Talker** (Qwen3-Omni) | Thinker's audio encoder | Thinker, in text, editable by us | Talker | Talker, exact if we replace the Thinker's text (§5.3) |

A and B are "LLM + STT/TTS". C, D and E are MMLLM options. E is a special case
of B and C combined: one model, but with a text seam we control.

## 4. MMLLM options in detail

### 4.1 Hosted realtime speech-to-speech APIs (option C, or the frontend of D)

| Provider / model | Tool calling | Notable for an orchestrator | Concerns |
|---|---|---|---|
| **Gemini Live** (`gemini-3.8-live`; `gemini-3.1-flash-live-preview` is used by Kairos) | Yes, including **non-blocking** calls with response scheduling (`SILENT`, `WHEN_IDLE`, `INTERRUPTED`) | Scheduling maps directly onto R5. Same SDK and vendor as today. Session resumption and context compression help R4 | Gemini Live 3.1 had the lowest turn-take rate in FDB-v3 (78%). Session length limits *(verify)* |
| **OpenAI Realtime** (`gpt-realtime`, `-mini`) | Yes, plus remote MCP tools | **Best FDB-v3 Pass@1 (0.600)** and lowest interruption rate. WebRTC, WebSocket and SIP | New vendor. 60-minute sessions |
| **Amazon Nova 2 Sonic** (Bedrock) | Yes, async *(verify)* | Text and voice switching in one session | Bedrock bidirectional streaming API. Not benchmarked here |
| **Qwen Omni Realtime** (`qwen3.8-omni-flash-realtime`, `qwen3.5-omni-plus-realtime`) | 3.8 Flash Realtime: custom function calling and MCP. 3.5 Plus Realtime: function calling and remote MCP | OpenAI-Realtime-style events; `semantic_vad` that filters backchannels; sessions up to 120 min; 16 kHz PCM in, 24 kHz out | **Regions are Singapore and Beijing only**, so a trans-Pacific round trip from `us-sanjose-1`. Context keeps up to 100 audio turns and 600 s of audio. The legacy `qwen3-omni-flash-realtime` is capped at 8 turns |

### 4.2 Open-weight models (option B, D or E; all need a GPU we don't have)

| Model | Shape | Fit |
|---|---|---|
| **Qwen3-Omni-30B-A3B** (Instruct, Thinking) | Thinker–Talker, speech out, Apache 2.0 | Option E. See §5 |
| **MiniCPM-o 4.5** (9B) | Full duplex, speech out | Small enough for one GPU. Tool use not evaluated here |
| **Moshi** (Kyutai) | Full-duplex speech-text | ~200 ms latency but weak reasoning and tools. A frontend for D at most |
| **Step-Audio R1.1 Realtime** | Separate reasoning and speech components | Apache 2.0 *(verify benchmarks)* |
| **Ultravox 0.7** | Audio in, text out (MIT) | Option B. In FDB-v3 |
| **Voxtral Mini 4B Realtime**, **Kyutai STT** | Streaming STT | Vosk replacements for option A |

### 4.3 Frontend/backend split (option D) is now a published pattern

- **Hu et al., "A frontend-backend architecture for tool calls in full-duplex
  speech models"** (arXiv 2609.19334). A speech frontend emits a delegation
  token, a text LLM backend makes the tool calls, and results are fed back via
  "prefill-and-repeat" before being spoken. Reported: **92–97% tool-call
  recall** single-turn and **81.2% irrelevant-call rejection**. With a large
  backend (Qwen3-235B-A22B) it is competitive on FDB-v3 and beats
  GPT-realtime-mini and Qwen3-Omni-30B-A3B-Instruct on EVA-Bench.
- **Qwen-Live-Harness** does the same at the application level: a realtime
  Omni session stays in the foreground while a coordinator delegates
  background work and queues results for announcement. This is structurally
  our dispatcher plus held announcements (Appendix A).

The point for us: D keeps **the tool decision in text**, where Part II's
validation gate applies unchanged, while gaining native turn-taking and
latency.

### 4.4 Two caveats that apply to every MMLLM option

1. **Audio in doesn't automatically fix names.** A February 2026 study
   (arXiv 2602.17598) found that many speech LLMs behave like an implicit ASR
   followed by an LLM. Ultravox was statistically indistinguishable from its
   matched cascade, and Qwen2-Audio was the exception. Keep the router's
   phonetic matching regardless, since tool arguments are still text.
2. **Spoken tool use is hard for everyone.** Full-Duplex-Bench-v3 (arXiv
   2604.04847) used real disfluent human audio with chained API calls. The best
   Pass@1 was 0.600 (GPT-Realtime). Every system struggled with
   **self-corrections** ("Tuesday… no, Wednesday") and multi-step reasoning.
   The Whisper → GPT-4o → TTS cascade had perfect turn-taking but the highest
   latency (10.12 s task latency, against 4.25 s for Gemini Live 3.1). These
   are whole-task latencies, not time to first audio.

## 5. Deep dive: Qwen Omni Thinker–Talker

### 5.1 Architecture (Qwen3-Omni technical report)

```
 audio ─► AuT audio encoder ─┐
 image/video ─► vision enc ──┼─► THINKER (MoE, 30B total / 3B active) ──► text tokens ──► text out / tool calls
 text ───────────────────────┘          │                                   │
                                        │ multimodal features +             │  ◄── external modules can
                                        │ shared conversation history       │      intervene on this text
                                        ▼                                   ▼      (RAG, function calling,
                              TALKER (MoE, 3B total / 0.3B active) ◄────────┘       safety filters)
                                        │ one codec frame per step (12.5 Hz);
                                        │ MTP module (80M) fills residual codebooks
                                        ▼
                              Code2Wav (200M, causal ConvNet) ──► streamed waveform
```

- **Thinker:** a multimodal MoE LLM. It reasons and writes the reply text,
  including any tool call.
- **Talker:** a smaller MoE model that generates multi-codebook speech codec
  tokens, one frame per step, with a multi-token-prediction module filling the
  residual codebooks. A causal ConvNet (Code2Wav) turns those into audio
  starting from the first frame. Qwen2.5-Omni used a block-wise DiT plus
  BigVGAN instead.
- **The key design change in Qwen3-Omni.** The report says the Talker *no
  longer consumes the Thinker's high-level text representations*. It conditions
  on audio and visual features and the shared conversation history, and the
  decoupling exists so that external modules (function calling, RAG, safety
  filters) can intervene on the Thinker's text output before it is spoken. The
  Thinker and Talker can also take **separate system prompts**, one for
  response content and one for voice style.

This corrects the earlier draft, which described the Talker as reading the
Thinker's hidden states, and labelled text interception as unverified
inference. Interception is the stated purpose of the design.

### 5.2 Lineage

| Version | Date | What changed | Tool calling |
|---|---|---|---|
| Qwen2.5-Omni (7B, 3B) | Mar 2025 | Introduced Thinker–Talker. Whisper-style encoder, TMRoPE time alignment, DiT + BigVGAN decoder | No |
| **Qwen3-Omni (30B-A3B)** | Sep 2025 | Both halves MoE, AuT encoder, 12.5 Hz multi-codebook codec, causal ConvNet decoder, decoupled Talker. Open weights, Apache 2.0 | Benchmarked on BFCL-v3, but the report lists "enhanced support for agent-based workflows and function calling" as **future work** |
| Qwen3.5-Omni (Plus, Flash, Light) | Mar 2026 | Hybrid-attention MoE, 256k context, ARIA text–speech alignment (fewer dropped words and garbled numbers), semantic interruption, voice cloning | Plus Realtime API: function calling and MCP. Light is reportedly the open-weight tier *(verify)* |
| Qwen3.8-Omni-Flash | Sep 2026 | Built around agentic tool use, 1M context, adjustable reasoning effort | Function calling, web search, MCP in the realtime variant. **API-only** at launch |

### 5.3 Why Thinker–Talker is the interesting option for an orchestrator

1. **The decision happens in text, inside one model.** Tool calls come out of
   the Thinker as text, so the router, dispatcher and Part II validation gate
   apply as-is. It is a half-cascade with no separate ASR and TTS hops.
2. **It is the only option with a seam between deciding and speaking.** When
   self-hosted, the serving loop can:
   - run the validation gate (§11.3) on a Thinker tool call *before* the
     Talker says "Sure, booking that now";
   - **replace** the Thinker's text with a fixed handler string ("Did you mean
     Atlas or Atlas Travel?") and have the Talker voice it in the same voice.
     That solves R3 and R9 together;
   - voice relayed agent `say` text in the assistant's voice (R9).

   The report states the architecture is designed for this. Whether the
   serving stack (vLLM-Omni or `transformers`) exposes a clean hook for
   "Talker, speak this text" is **unverified** and is the first thing a
   prototype must check. The hosted realtime APIs don't expose this seam.
3. **Text-only fallback.** The Thinker can return text without speech, so we
   could fall back to Piper for anything we want fully controlled.
4. **A path to training the decision model.** With open weights, the When2Call
   preference-training recipe (§8.4) could be applied to the Thinker. No
   hosted option allows this.

### 5.4 What it costs

- **GPU infrastructure.** The 30B-A3B model needs roughly 79 GB+ of GPU memory
  in BF16 per its README. The OCI VM has no GPU.
- **Concurrency hurts latency.** The headline **234 ms** first-packet latency
  is a cold start at concurrency 1. The report gives **728 ms at 4 concurrent
  and 1,172 ms at 6** (audio). An orchestrator serving many users needs to
  plan capacity against those numbers, not the headline.
- **Tool calling in the open weights is immature.** Qwen3-Omni lists function
  calling as future work. The strong tool-use releases (3.5 Plus, 3.8 Flash)
  are API-only. Option E today means either accepting weaker tool calls or
  putting a text backend behind it, which is option D again.
- **Hosted Qwen is far away.** Singapore or Beijing from San Jose adds a
  trans-Pacific round trip per turn, and the user's raw audio leaves the
  country.

## 6. Scoring the options

✔ good, ~ workable with effort, ✘ weak. Requirements are from §2.

| Requirement | A Streamed cascade | B Half-cascade | C Native S2S | D Frontend/backend | E Self-hosted Thinker–Talker |
|---|---|---|---|---|---|
| R1 Tool accuracy | ✔ text LLM, best-studied | ✔ | ~ FDB-v3 best 0.600 | ✔ 92–97% recall reported | ~ open weights weak at tools today |
| R2 Interceptable | ✔ | ✔ | ~ calls yes, speech before the call no | ✔ backend owns calls | ✔ calls and speech |
| R3 Exact speech | ✔ | ✔ | ✘ may paraphrase | ~ inject and ask verbatim | ✔ replace Thinker text |
| R4 Bridge handoff | ✔ gate audio | ✔ | ~ pause or resume session | ~ | ~ we own the session |
| R5 Async results | ✔ dispatcher | ✔ | ✔ Gemini `WHEN_IDLE` | ✔ by design | ✔ |
| R6 Names | ~ Vosk garbles; router backstop | ~ often implicit ASR | ~ | ~ | ~ |
| R7 Latency | ~ unmeasured; streaming helps | ~ | ✔ | ✔ | ~ depends on GPU and concurrency |
| R8 Turn-taking | ~ our VAD heuristics | ~ | ✔ native | ✔ native | ~ we build it |
| R9 One voice | ✔ Piper only | ✔ | ✘ provider plus Piper | ✘ unless relayed text is routed through the model | ✔ |
| R10 Cost, privacy, infra | ✔ free, local | ~ GPU or hosted | ~ metered, audio leaves the VM | ~ two models | ✘ GPU servers |

The pattern: **A and D dominate.** A is the cheapest and keeps every
guarantee. D gives native turn-taking and latency while keeping tool
decisions in text. E is the only option scoring well on R2, R3 and R9 at once,
but it costs infrastructure and its open weights lag on tool use.

## 7. Recommendation and decisions

1. **Measure** (§0, step 1).
2. **Ship option A first.** Stream Gemini tokens into Piper sentence by
   sentence and keep the thinking cue for the residual gap. If the LLM wait
   dominates, this probably recovers most of the latency. That is an
   assumption to confirm by measuring.
3. **Prototype option D** behind a provider-neutral `RealtimeVoiceSession`
   adapter (sketch below). Gemini Live is the default frontend (same vendor,
   `WHEN_IDLE` scheduling). OpenAI Realtime is the alternative if the eval
   shows its tool-use lead matters. The text backend stays today's
   `CustomGeminiLLMService` plus router and dispatcher.
4. **Keep option E as the targeted escape hatch** if exact in-voice acks (R3
   with R9), data residency or per-minute cost at scale become hard
   requirements. Start with a one-day spike: verify the Talker text-injection
   hook on Qwen3-Omni in vLLM-Omni.
5. **Switch only on evidence.** Run §12's eval on A and D. Move to D only if
   the tool-decision metrics hold and latency improves.

```
interface RealtimeVoiceSession:                    # one per user session, provider-neutral
    open(system_prompt, tools, voice, transcription=on) / close()
    send_audio(pcm) / pause_input() / resume_input()
    inject_text(text, role, schedule=NOW|WHEN_IDLE)
    send_tool_result(call_id, result, schedule=NOW|WHEN_IDLE|SILENT)
    events: audio_out, tool_call(id, name, args), transcript(role, text),
            turn_started/ended, interrupted, session_expiring(resume_handle)

OrchestratorVoiceLoop:
    on mic pcm:     bridge.active ? bridge.send(pcm) : session.send_audio(pcm)
    on tool_call:   verdict = validation_gate(call, transcript)        # §11.3, same for every option
                    result  = verdict.ok ? await HANDLERS[name](args) : verdict.spoken_reason
                    session.send_tool_result(id, result)
    on task update: bridge.active ? hold(rec) : session.inject_text(summary, schedule=WHEN_IDLE)
    on bridge start/end: pause_input + ding / inject "[bridge with X ended]" + resume + flush held
```

**Decisions for you**

- **1a.** Measure, then option A first (proposed)? Or go straight to an MMLLM
  prototype?
- **1b.** If MMLLM: frontend/backend split D (proposed), full native C, or
  self-hosted Thinker–Talker E?
- **1c.** Prototype frontend: Gemini Live (proposed), OpenAI Realtime, or Qwen
  Omni Realtime? Qwen's region latency counts against it.
- **1d.** Are two voices acceptable (provider voice plus Piper for relayed
  `say`)? If not, the choice narrows to A, B or E.
- **1e.** Is a GPU spend for option E on the table at all? If not, E drops out
  and Qwen is only a hosted candidate for C or D.

---

# Part II: Decision 2, reliable tool calls

## 8. What When2Call found

*When2Call: When (not) to Call Tools.* Hayley Ross, Ameya Sunil
Mahabaleshwarkar, Yoshi Suhara (NVIDIA). NAACL 2025. arXiv 2504.18851. Code and
data at `github.com/NVIDIA/When2Call` under Apache 2.0.

### 8.1 The question it asks

Tool-use benchmarks (BFCL and similar) mostly score whether a call is
*correct*. When2Call scores whether the model picks the right **kind** of
response. Each item has four options:

| | Response type | When it is correct in When2Call |
|---|---|---|
| (a) | Direct text answer | **Never.** Every question needs a tool, so a direct answer is by construction a hallucinated answer |
| (b) | Tool call | The tool fits and all required arguments are present |
| (c) | Follow-up question | A suitable tool exists but required information is missing |
| (d) | Unable to answer | No provided tool can serve the request (including when no tools are given) |

### 8.2 How it is built and scored

- **Source.** Questions from BFCL v2 Live and APIGen. Mixtral 8x22B filters to
  questions that genuinely need a tool (real-time data, database access,
  other). For each, it generates three variants: unchanged (correct answer is
  a tool call), parameter removed (correct answer is a follow-up), and a
  related question the tool can't answer (correct answer is "unable"). It also
  generates the three wrong options for each.
- **Size.** Test set: **3,652** items, with 1,295 tool call, 1,062 follow-up
  and 1,295 unable. 258 items have zero tools, 712 have one and 2,682 have two
  or more. Train set: 6,000 SFT items. Preference set: 10,500 pairs. Manually
  estimated quality is 92% for questions and 94% for answers.
- **Scoring.** Multiple choice by **length-normalised log-probability** over
  the four options (LM Eval Harness), so free-form phrasing doesn't confound
  the result. Closed models and models trained on When2Call are scored with
  **LLM-as-judge** (GPT-4-Turbo classifies the free-form answer).
- **Metrics.** Macro **F1** over the categories; length-normalised accuracy;
  **tool hallucination rate** (picking a tool call when *no tools* were
  provided); answer hallucination (picking (a)); and parameter hallucination
  (picking (b) when (c) was correct, meaning invented arguments).

### 8.3 Results

| Model | When2Call F1 | Tool halluc. | BFCL Live AST | BFCL Irrelevance |
|---|---|---|---|---|
| Llama 3.1 8B Instruct | 16.6 | **67%** | 51.6% | 40.0% |
| Llama 3.1 70B Instruct | 37.8 | 57% | 68.3% | 36.5% |
| Qwen 2.5 7B Instruct | 32.0 | 21% | 64.1% | 51.4% |
| Qwen 2.5 72B Instruct | 32.8 | 23% | 69.3% | 61.1% |
| xLAM 7B FC-R | 31.5 | 24% | 58.3% | **79.8%** |
| xLAM 8x22B R | 34.3 | 9.0% | 74.7% | 75.2% |
| GPT-4o-mini (judge) | 52.9 | 41% | 76.5% | 80.7% |
| GPT-4o (judge) | 61.3 | 26% | 79.8% | 83.8% |
| MNM 8B, baseline SFT | 31.9 | 19% | 62.2% | 36.3% |
| MNM 8B, + When2Call **SFT** | 49.4 | 7.0% | **57.5%** ↓ | 61.0% |
| MNM 8B, + When2Call **RPO** | **52.4** | **1.2%** | 62.5% | 78.1% |

MNM is Mistral-NeMo-Minitron, NVIDIA's own base model for the training
experiments. Closed-model numbers come from LLM-as-judge and the open-model
numbers from log-prob scoring, so the comparison across those groups is
approximate.

### 8.4 Takeaways, with evidence

1. **Over-calling is the default failure, even in frontier models.** Given
   *no tools at all*, GPT-4o still produced a tool call 26% of the time and
   GPT-4o-mini 41%. Llama 3.1 8B did so 67% of the time.
2. **Scale doesn't fix it.** Llama 70B is still at 57% tool hallucination.
   Qwen 2.5 is flat across 3B, 7B and 72B (F1 29.8 → 32.0 → 32.8). The
   authors call the pattern non-monotonic and unexplained.
3. **Existing irrelevance tests don't predict it.** xLAM 7B scores 79.8% on
   BFCL Irrelevance but only 31.5 F1 on When2Call. Rejecting a clearly
   irrelevant tool is easier than choosing the right *alternative* behaviour
   for a subtly mismatched one.
4. **Failure modes differ by model family.** Llama calls tools whenever any
   tool is present. Qwen and xLAM reject tools more often but are "highly
   unwilling" to say "unable to answer", picking a direct answer or a
   follow-up instead. Several models ask follow-ups correctly about half the
   time but otherwise **invent the missing argument**.
5. **Plain SFT on abstention data makes models over-cautious.** Adding
   When2Call data to SFT raised F1 (31.9 → 49.4) but cut real tool-calling
   accuracy (BFCL AST 62.2% → 57.5% at 8B, and a larger drop at 4B).
6. **Preference optimisation gets both.** RPO (reward-aware preference
   optimisation) on When2Call pairs reached the best F1 (52.4) and 1.2% tool
   hallucination while keeping BFCL AST at 62.5%. The recipe: correct option
   as *chosen*, a wrong option as *rejected*, half the pairs being tool calls
   with **corrupted arguments** (so the model learns "right tool, wrong args"
   is bad without learning "don't call"), and a low KL penalty (0.05).
7. **The paper tested no prompting interventions.** Every model got the same
   short instruction: "Only use a tool if it directly answers the user's
   question." Whether a stronger prompt closes the gap is **open**, so our
   prompt-level defence (§11.2) is unvalidated until we measure it.

### 8.5 Limits of the paper, for our use

- Synthetic, English-only, text-only. No speech, no STT noise.
- **Direct answer is always wrong** in When2Call. For the orchestrator,
  answering directly is often correct ("what's 15% of 80?"). Our taxonomy
  needs that class back (§9.1).
- Single-turn. No multi-step chains, no late self-corrections. FDB-v3 covers
  those, for speech.
- Training on When2Call can bias the model towards particular phrasings and
  distort log-prob scoring. The authors recommend LLM-as-judge for trained
  models.

### 8.6 Corrections to the earlier draft

| Earlier draft said | Paper says |
|---|---|
| *(recall)* "Models fine-tuned for tool calling tend to be worse at abstaining" | More nuanced. xLAM, a tool-calling model, had the **lowest** open-model tool hallucination (9%), but it and Qwen avoid the "unable" option. The clear finding is that BFCL-style tool-calling skill doesn't predict When2Call skill |
| *(recall)* "SFT alone made models over-cautious" | Confirmed, measured as the BFCL AST drop |
| Four behaviours include "answer directly" as a correct choice | In When2Call a direct answer is always the wrong choice. "Answer directly" is our extension |

## 9. What it means for the orchestrator

### 9.1 Our decision taxonomy

Every agent is effectively a tool, and agents can act in the world. The
orchestrator's per-turn decision has five outcomes:

| Outcome | When | Tool | Failure if wrong |
|---|---|---|---|
| **Answer** | General knowledge suffices | none | Unneeded dispatch, or an invented fact |
| **Ask** | An agent is needed but a required detail is missing, or the name is ambiguous | none (or the router returns `ambiguous`) | **Parameter hallucination**: a task dispatched with an invented date, time or count |
| **Decline** | No agent can do it, or the named agent doesn't exist or is in the wrong mode | none (router `none` or `wrong_mode`) | **Tool hallucination**: a wrong agent dialled, or "done" claimed falsely |
| **Dispatch** | Do it and report back (Protocol 2) | `route_to_agent(mode_hint=dispatch)` (§10) | Wrong agent, wrong args, duplicate side effect |
| **Bridge** | Talk to an agent live (Protocol 1) | `route_to_agent(mode_hint=connect)` (§10) | Wrong agent connected |

§10 breaks Dispatch and Bridge into the concrete orchestrator modes (direct
route, indirect route, connect vs dispatch, task dispatch, task CRUD) and
gives each its own checks.

### 9.2 Why voice makes it worse

- **STT noise creates plausible wrong arguments.** "Fifteen" heard as
  "fifty", "Kairos" as "cut in". The model sees a complete-looking request and
  calls the tool (When2Call's parameter hallucination, with the user unaware).
- **Late self-corrections.** "Book Tuesday… no, Wednesday" is where every
  FDB-v3 system failed. A cascade with utterance-level STT sees the whole turn
  and has a chance. A streaming model may already have committed.
- **No screen to confirm on.** A wrong text call shows up in a UI. A wrong
  voice call is only heard after it has happened.
- **A large and growing tool surface.** When2Call's subtle mismatches are
  the normal case with 1,000 agents, many with overlapping descriptions.

## 10. Orchestrator modes and the tool calls behind them

This section defines what the orchestrator should be able to do, in terms of
the two agent protocols, and gives each ability its own tool-call rules.

- **Protocol 1 (bridge, live).** The user's mic is relayed to the agent and the
  agent talks back. This is v1 `mode: "bridge"`.
- **Protocol 2 (task, background).** The orchestrator hands the agent a
  structured job, then monitors it, relays questions and delivers the result.
  This is protocol v2 `mode: "task"` (Appendix A).

| Mode | Protocol | User says | Router call | Main When2Call risk |
|---|---|---|---|---|
| **M1 Direct route** | 1 | "Connect me to Kairos" | `resolve(selector=name, mode=bridge)` | Wrong agent from a garbled or ambiguous name |
| **M2 Indirect route** | 1 | "How many calories in a bagel?" → MyFitnessPal. "What's on my list today?" → Kairos | `resolve(intent=utterance, mode=bridge)` | Over-routing (calling when it should answer) or under-routing |
| **M3 Connect vs dispatch** | 1 or 2 | "Have Tabletop book dinner" vs "Let me talk to Tabletop" | Policy on top of `resolve` | Wrong protocol: a live call for a one-shot job, or a background task for a conversation |
| **M4 Task dispatch and monitor** | 2 | "Book Nopa for two at seven and let me know" | `resolve(…, mode=task)` | Parameter hallucination, wrong agent, duplicate side effects |
| **M5 Task CRUD and lifecycle** | 2 | "What's still running?", "Move it to eight", "Cancel that", "Don't tell me, just do it" | Task lookup, not agent lookup | Acting on the wrong task, or claiming a status the record doesn't show |

### 10.1 M1: direct route

The user names the agent. The model passes the name **as heard**, and the
router returns a decision (Appendix A):

| Router decision | Orchestrator does | Spoken |
|---|---|---|
| `matched` | Connect | "Connecting you to Kairos now." |
| `ambiguous` | Ask, never pick | "Did you mean Atlas or Atlas Travel?" |
| `none` | Decline, never fall back to a default agent | "I couldn't find an agent called Zorblax." |
| `wrong_mode` (task-only agent) | Offer M4 | "Ledger doesn't take live calls, but I can send it a task." |

The named agent is never substituted, even if it is down. "Kairos isn't
answering right now" is the correct outcome, not a silent connection to
another calendar agent.

### 10.2 M2: indirect route

The user states a need, and the orchestrator picks the agent. The examples
are domain ownership: MyFitnessPal owns nutrition and food logging, and Kairos
owns the user's tasks and reminders.

This is exactly When2Call's hardest boundary: **should I call at all?** "How
many calories in a banana?" can be answered from general knowledge. "How many
calories have I had today?" can only be answered with the user's data.
Over-routing turns the orchestrator into a switchboard that forwards every
question. Under-routing makes it invent numbers that belong to an agent.

**Registry fields that make indirect routing deterministic:**

| Field | Example (MyFitnessPal) | Purpose |
|---|---|---|
| `domains` | `["nutrition", "food_log", "calories"]` | What the agent is the authority for |
| `intent_aliases` | `["calories", "macros", "what did I eat", "log my lunch"]` | Curated trigger phrases. A hit scores as a strong intent match, which fixes the lexical-only gap ("restaurant reservations" vs "book a table") for first-party agents |
| `routing_policy` | `owns_domain` or `on_request` | `owns_domain`: route any in-domain request, because the agent is the system of record (Kairos for tasks). `on_request`: route only when the request needs user data or an action, and otherwise let the orchestrator answer |
| `user_data` | `true` | The agent holds per-user state, so personal questions ("have I…", "my…") must go to it, never be answered by the LLM |

**Decision rule** (runs in the handler, after the model proposes a route):

```
route_indirect(utterance):
    route = router.resolve(intent=utterance, mode=…)
    if route.decision != MATCHED or route.best.score < INTENT_FLOOR (higher than the name floor):
        → ANSWER if general knowledge suffices, else ASK "Want me to check MyFitnessPal?"
    agent = route.best
    if agent.routing_policy == on_request and not needs_user_data_or_action(utterance):
        → ANSWER directly (optionally offer: "MyFitnessPal can log that if you want")
    → connect, saying which agent and why: "Let me get MyFitnessPal for that."
```

- **Say the agent's name before connecting.** On an indirect route the user
  didn't choose the agent, so the ack is also the confirmation. The user can
  barge in with "no" during the ack plus ding (about 1.5 s) to cancel before
  the dial.
- **Two-stage routing pays off here** (§11.4). Retrieve the top-k candidates
  by intent, then let the LLM pick among k with their descriptions. Indirect
  routing is where lexical scoring is weakest.

### 10.3 M3: connect or dispatch

For an agent that supports both protocols, something has to choose. The
choice between two similar tools is the kind of subtle mismatch When2Call
shows models get wrong. So the proposal is **one routing tool with a mode
hint, and a deterministic policy in the handler**:

```
route_to_agent(agent?, intent?, mode_hint: "connect" | "dispatch" | "auto", slots?, notify?)
```

The model only reports what the user's wording implies. The handler decides:

| Signal | Points to | Examples |
|---|---|---|
| Explicit wording (`mode_hint`) | Whatever the user said | "talk to", "put me through", "let me ask" → connect. "have X do", "get it done", "let me know when" → dispatch |
| Agent's registered `modes` | The only mode it supports | Task-only Ledger → dispatch. Bridge-only agent → connect |
| Request shape | Complete and one-shot → dispatch. Open-ended or multi-turn → connect | "Log a bagel for breakfast" → dispatch. "Help me plan meals this week" → connect |
| Missing slots | More than one required slot missing → connect | It is faster for the agent to ask live than to relay several `input_required` round trips |
| Time | Future or long-running → dispatch | "Remind me at five", "watch for price drops" |

```
decide_mode(agent, mode_hint, slots):
    if agent supports only one mode:                  return that mode   (wrong_mode if the hint conflicts)
    if mode_hint in (connect, dispatch):              return mode_hint
    if is_open_ended(intent) or missing_required(slots) > 1:   return CONNECT
    if all required slots grounded (§11.3):           return DISPATCH
    return ASK  "Should I connect you, or have it done and let you know?"
```

**Switching mid-flight:**

- **Task → live.** If a dispatched task raises `input_required` twice, offer
  "Tabletop has a few questions. Want me to put you through?" Accepting it
  cancels the task with reason `escalated_to_bridge` and opens a bridge.
- **Live → task.** An agent can hand back an ongoing job at bridge end (an
  optional `task.created` frame from the agent, carrying a task_id the
  orchestrator then tracks as if it had dispatched it). That's how "I'll text
  you when the booking confirms" becomes a tracked task instead of a promise.

### 10.4 M4: task dispatch and monitor

This is the dispatcher as built (Appendix A): resolve with `mode=task`, never
substitute a named agent, fail over only on intent routes and only before
acceptance, no retry after `task.accepted`, relay `input_required` to the
user, deliver results. Two additions follow from M5:

- every task carries a **`notify`** setting (§10.6);
- tasks are **persisted** (§10.5), because a result that has to wake the
  ESP32 must survive a restart and outlive the session that created it.

### 10.5 M5: task CRUD

**Two kinds of task existed, and they are now one table.** Before this change:

| | User tasks (`tasks` table) | Agent tasks (dispatcher) |
|---|---|---|
| Created by | Kairos voice tools, `POST /tasks` | `dispatch_task`, `POST /api/dispatch` |
| Stored | Postgres. `time_to_execute` → `jobs` row → MQTT wake (SCHEDULER.md) | **In memory**. An `agent_tasks` table existed in `ai_pin_db` with 0 rows and no code using it |
| Status | `pending` / `completed` | queued … succeeded / failed / cancelled / timed_out |
| User notified | ESP32 wake at `time_to_execute` | Only if a session is open, or at the next session |

**Decision (done): `tasks` is the master table** for Kairos reminders and
orchestrator agent tasks, and `agent_tasks` is dropped. This also settles C1:
Protocol 2 tasks are persisted, in `tasks`. Migration:
[`deploy/sql/002_tasks_master.sql`](deploy/sql/002_tasks_master.sql)
(idempotent and additive; applied to `ai_pin_db` 2026-10-06, schema in `DATABASE.md`).

**Not every task is scheduled.** `time_to_execute` was already nullable. The
migration adds `is_scheduled`, a generated column (`time_to_execute IS NOT
NULL`) that can't disagree with it. Only scheduled tasks get a `jobs` row. A
task with no time is never enqueued, because a job without `deliver_at` fires
immediately and would wake the device. `task_crud.create_task` and
`reenqueue_task_after_edit` enforce this. `POST /enqueue-task` still allows an
explicit "send now".

```
tasks                                          (existing columns first)
  task_id            uuid  pk                  idempotency_key = task_id
  user_id            uuid  not null            must be the id the device authenticates as (SCHEDULER.md TODO). No FK to users
  task_info          jsonb                     {"info": text} today. Agent tasks add {"intent", "slots"} (grounded values only, §11.3)
  status             text  (check)             pending | dispatching | running | input_required |
                                               completed | failed | cancelled | timed_out
  time_to_execute    timestamptz               NULL = unscheduled
  enqueue_sequence_id bigint                   → jobs.id of the pending wake (no FK)
  ── added by 002 ──
  is_scheduled       bool  generated           time_to_execute IS NOT NULL
  kind               text  not null 'reminder' reminder | agent_task
  created_by         text                      kairos | app | orchestrator | agent (NULL for older rows)
  agent_id           uuid  → agents(agent_id)  required when kind = agent_task. ON DELETE SET NULL
  notify             text  not null 'device'   device | next_session | silent (§10.6). The orchestrator sets it explicitly for agent tasks
  question           text                      set while input_required
  result             jsonb                     {say (≤ 2,000 chars, spoken verbatim), output (≤ 256 KB), error}
  deadline_at, finished_at, delivered_at, agent_informed_at   timestamptz
  delivered_via      text                      live | device_wake | next_session
  created_at, updated_at  timestamptz not null updated_at maintained by a trigger
```

Dispatcher states map onto the shared `status` vocabulary: queued → `pending`,
accepted or progress → `running`, succeeded → `completed`. Kairos's existing
`pending`/`completed` values are unchanged, so its tools and the worker's
"drop if not pending" check work as before.

**Still open (Kairos side).** `get_tasks_tool_agent` filters on a
`time_to_execute` range, so unscheduled tasks are invisible to Kairos reads.
It also creates tasks under a hard-coded user_id. Kairos should probably list
`kind = 'reminder'` tasks with `time_to_execute IS NULL` alongside the day's
scheduled ones, and filter out agent tasks unless asked.

All state changes go through the dispatcher, which writes the row and then
notifies listeners. That is one write per transition. An append-only
`task_events` table is optional, for audit and debugging.

**Operations, by voice (one `manage_task` tool) and HTTP:**

| Op | Voice | HTTP | Effect on the agent (Protocol 2) |
|---|---|---|---|
| **Create** | `route_to_agent(…, mode=dispatch)` | `POST /api/dispatch` | `task.dispatch` |
| **Read** | `manage_task(action="status", task_ref?)` | `GET /api/dispatch/{id}`, `…/user/{user_id}?status=` | none |
| **Update** | `manage_task(action="update", task_ref, changes)` | `PATCH /api/dispatch/{id}` | **New `task.update {task_id, changes}`.** Allowed before a terminal state. The agent replies `task.update_accepted` or `task.update_rejected {reason}`. Agents that don't advertise `supports_update` in their ack get cancel-and-recreate, with a read-back, because the first attempt may already have had side effects |
| **Cancel** | `manage_task(action="cancel", task_ref)` | `POST …/{id}/cancel` | `task.cancel {reason: user_cancelled}` |
| **Complete** (user says it's done) | `manage_task(action="complete", task_ref)` | `POST …/{id}/complete` | **New `task.closed {reason: completed_by_user}`.** For watch-style tasks ("tell me when it drops below $200") the user ends the task |
| **Delete** | `manage_task(action="delete", task_ref)` | `DELETE /api/dispatch/{id}` | Cancel first if active, mark it `cancelled`, then hard-delete the row once `agent_informed_at` is set (as Kairos deletes do today) |
| **Answer** | `manage_task(action="answer", task_ref?, answer)` | `POST …/{id}/input` | `task.input` |

### 10.6 Telling the agent and telling the user

"Task completed" has two audiences, and each needs its own delivery
guarantee.

**Telling the agent.** The agent that owns a task must always learn how it
ended, whoever ended it. New Protocol 2 frames:

```
orchestrator → agent
  task.update     {task_id, changes}                                         (M5 update)
  task.closed     {task_id, reason: completed_by_user | cancelled | deleted |
                   deadline | escalated_to_bridge, by: user | orchestrator}
  task.delivered  {task_id, via: live | device_wake | next_session, at}      (the user heard the result)

agent → orchestrator
  task.update_accepted / task.update_rejected {task_id, reason}
  task.created    {task_id, intent, notify?}                                 (live → task hand-back, §10.3)
```

Task connections are pooled and closed when idle, so the agent may be
unreachable when a notice is due. Notices go to an **outbox**: the
`agent_informed_at IS NULL` rows, plus a `task.delivered` once `delivered_at`
is set. The outbox is sent the next time the pool dials that agent, and a
background sweep dials agents with pending notices, with backoff, for up to
24 h. Agents must treat these frames as idempotent.

**Telling the user: the `notify` flag.**

| `notify` | Meaning | When the result arrives |
|---|---|---|
| `device` | Tell me as soon as it's done, even if I'm not in a call | Session open → speak it (held while bridged). Otherwise → insert a `jobs` row (`kind = 'task_result'`, payload `{task_id}`) → the worker publishes `start_websocket` to the ESP32 → the pin calls in → the orchestrator announces it |
| `next_session` | Tell me next time I talk to you | Session open → speak it. Otherwise → wait. Announce at the next connect |
| `silent` | Don't tell me. I'll ask | Record only. Available through `manage_task(status)`. The agent still gets `task.closed` |

```
on_task_terminal(rec):
    persist(rec)
    if session_active(rec.user_id):
        bridge.active ? hold(rec) : speak(rec) → mark_delivered(rec, via="live")
    elif rec.notify == "device" and wake_allowed(rec.user_id):
        insert_job(kind="task_result", payload={"task_id": rec.task_id}, deliver_at=now)
    # next_session, silent, or a disallowed wake: nothing now

on_session_start(user_id):                     # every connect: button press, wake or reconnect
    for rec in undelivered(user_id, notify in (device, next_session)), oldest first, max 3:
        speak(rec) → mark_delivered(rec, via = "device_wake" if a wake job is open else "next_session")
    if more remain: "You have N more task updates. Want to hear them?"
    → enqueue task.delivered to each agent
```

This also closes most of the SCHEDULER.md TODO "announce the triggering task
on a woken call", **without a firmware reflash**. Because results are read
from the database at session start, the device doesn't need to relay the
wake's `system_message`: any call from the user finds the undelivered results.
Prerequisite: the device's user_id must match the task owner (the
task-owner identity TODO in SCHEDULER.md), or the lookup comes back empty.

**Rules for the flag**

- **It is a slot like any other, so it must be grounded (§11.3).** "Let me
  know when it's done" → `device`. "No need to tell me" → `silent`. Silence on
  the subject → the agent's registered default, else `next_session`. The model
  must never set `device` on its own initiative, because waking the pin is the
  most intrusive thing the system can do.
- **Agents can lower urgency but not raise it.** An agent's `task.result` may
  include `notify_hint: urgent`. The orchestrator honours it only up to what
  the user set, so an agent can't wake the device unless the user asked to be
  told.
- **`wake_allowed`** checks quiet hours and a per-user rate limit (for example
  at most 4 task wakes per hour; several finished results go into one wake).
  The worker's existing check still applies: a wake is deferred while a
  session is active.
- **Worker change.** `listener/worker.py` learns `kind = 'task_result'`: it
  publishes the same `start_websocket` wake, and drops the job if
  `tasks.delivered_at` is already set. That mirrors today's "drop a task
  job whose task is no longer pending" safety net.
- **Failed and timed-out tasks** are announced with the same flag. "Tabletop
  couldn't book that" is as important as success.

### 10.7 Tool calls for these modes

The tool surface the model sees shrinks from seven tools to five:

| Tool | Covers | Replaces |
|---|---|---|
| `route_to_agent(agent?, intent?, mode_hint, slots?, notify?)` | M1, M2, M3, M4 create | `start_remote_audio_bridge`, `dispatch_task` |
| `find_agents(query)` | "Is there an agent for…?" | unchanged |
| `manage_task(action, task_ref?, changes?, answer?, notify?)` | M5 | `check_tasks`, `cancel_task`, `answer_agent` |
| `end_conversation`, `google_search` | unchanged | unchanged |

What the validation gate (§11.3) adds for these modes:

| Check | Mode | Rule |
|---|---|---|
| Name decision | M1 | Only `matched` connects. No default-agent fallback |
| Intent floor and policy | M2 | Intent score above `INTENT_FLOOR`, plus the agent's `routing_policy`. Personal-data questions in an owned domain must route, never be answered |
| Mode policy | M3 | `decide_mode` runs in code. The model's `mode_hint` must be grounded in the user's wording or be `auto` |
| Slot grounding | M4 | Unchanged from §11.3 |
| **Task reference resolution** | M5 | `task_ref` ("that", "the dinner booking", "the MyFitnessPal one") is resolved like an agent name: the last task mentioned, then a description match. It returns `matched`, `ambiguous` (ask "The dinner booking or the taxi?") or `none` (decline). Never act on a guessed task |
| Destructive read-back | M5 | Cancelling or deleting an **accepted** task whose agent has `side_effects: true` needs a yes: "Cancel the Nopa booking? It may already be confirmed." |
| Status from the record | M5 | Spoken status comes from the `tasks` row, never from the model's memory of the conversation. This prevents When2Call's answer hallucination ("Your table's booked" when it isn't) |
| Notify grounding | M4, M5 | `notify=device` only if the user asked to be told |

### 10.8 Decisions for you

- **M-a.** One `route_to_agent` tool with a code-side mode policy (proposed),
  or separate bridge and dispatch tools chosen by the model?
- **M-b.** Indirect routes: announce and connect with a barge-in window
  (proposed), or always ask "Want me to get MyFitnessPal?" first?
- **M-c.** Add `domains`, `intent_aliases`, `routing_policy` and `user_data`
  to registration, and backfill them for first-party agents (Kairos,
  MyFitnessPal)?
- **M-d.** ~~Persist Protocol 2 tasks in `agent_tasks`?~~ **Decided:** they go in `tasks` (§10.5). This flips C1, and
  ESP32 notification depends on it.
- **M-e.** Default `notify` when the user says nothing: `next_session`
  (proposed) or `device`?
- **M-f.** Wake limits: quiet hours and at most 4 task wakes per hour per
  user?
- **M-g.** Protocol 2 additions (`task.update`, `task.closed`,
  `task.delivered`, `task.created`). Add them all now, or start with
  `task.closed` and `task.delivered`?

## 11. The optimisation space: a layered defence

The paper's lesson is that we can't trust the model's own judgement on
whether to call. So we shrink the problem, steer the model, and then **check
its output deterministically** before acting. The layers are ordered by cost.
L0–L3 apply to every Part I option that keeps tool calls in text, which is
all of them.

```
user turn ─► [L0 tool surface] ─► [L1 prompt] ─► model proposes: answer | ask | decline | tool_call
                                                                              │
                                     ┌────────────── L2/L3 validation gate ◄──┘
                                     │  router decision · schema · grounding · side-effect read-back
                                     ▼
                           execute ─or─ convert to ask / decline (spoken deterministically)
```

### 11.1 L0: Shape the tool surface (cheap, high leverage)

- **Bounded agent list.** Don't list 1,000 agents in the prompt. Use
  `find_agents` and router resolution instead (Appendix A). Fewer visible
  choices means fewer subtle mismatches.
- **Make required fields required.** `dispatch_task` should carry structured
  slots such as `when` or `party_size` from the agent's registered schema, as
  required fields. Missing information is then visible to the gate, not buried
  in free-text `details`.
- **Merge overlapping tools.** Bridge and dispatch become one
  `route_to_agent` with a code-side mode policy, and the task-management tools
  become one `manage_task(action, …)`, for five tools in total (§10.7).
  When2Call's 2+-tool items are harder, and every tool added is a new place to
  over-call.
- **No silent fallbacks.** An unknown agent name is declined, never routed to
  a default (Appendix A, A3). The fallback was a built-in tool hallucination.

### 11.2 L1: Prompt the decision explicitly (cheap, unvalidated)

```
For each turn decide exactly one:
 ANSWER   if general knowledge is enough. No tool.
 ASK      ONE short question if an agent is needed but a required detail is missing
          or you're unsure what the user said. Never fill in a value the user didn't say.
 DECLINE  if no agent can do it. Say so plainly. Never claim something was done.
 DISPATCH dispatch_task to get something done and reported back.
 BRIDGE   start_remote_audio_bridge to talk to an agent live.
If the user corrects themselves ("no, Wednesday"), use the LAST value.
Pass agent names as heard; the orchestrator resolves them.
```

Add two or three short examples per outcome, drawn from §12 failures. The paper
tested no prompt interventions (§8.4, point 7), so this layer's value must be
measured, not assumed.

### 11.3 L2: The deterministic validation gate (the core proposal)

All checks run in the tool handler, before any side effect. A failing check
converts the call into an **Ask** or **Decline**, spoken with a fixed string.

| Check | Targets (When2Call failure) | Mechanism |
|---|---|---|
| **Router decision, not guess** | Tool hallucination (wrong agent) | `resolve()` returns `matched`, `ambiguous`, `none` or `wrong_mode`. Only `matched` proceeds (Appendix A) |
| **Schema validation** | Malformed calls | Required slots present, types and enums valid |
| **Argument grounding** | **Parameter hallucination** | Each required slot value must be traceable to the user's recent turns (normalised substring, number and date normalisation, phonetic match for names). A slot with no grounding becomes an Ask ("For what time?"). This is the follow-up behaviour, enforced rather than requested |
| **Self-correction check** | FDB-v3's main failure | If the turn contains a correction marker ("no", "actually", "I mean", "wait") after a slot value, require the call to use the later value, or re-ask |
| **Read-back for side effects** | Wrong args with real consequences | Agents declare `side_effects: true` at registration. For those, the first call becomes "Book Nopa for 2 at 7. Shall I go ahead?" and the user's yes completes it. Read-only tasks skip this |
| **No tool from agent text** | Prompt injection via results | Agent `say` and `output` are quoted as untrusted. A tool call needs a user turn in between |
| **No repeat after accept** | Duplicate side effects | The dispatcher doesn't retry after `task.accepted` and passes `idempotency_key` (Appendix A) |

Grounding has a real cost: it can over-ask, which is the over-caution failure
from §8.4 point 5. That is why §12 tracks the ask rate alongside the
hallucination rates. Thresholds start lenient and are tuned on logs.

### 11.4 L3: Model-assisted checks (more cost, use selectively)

- **Retrieve, then choose.** For intent-routed dispatch (no agent named), the
  router retrieves the top-k agents and the LLM picks among those k with
  their descriptions. That costs one extra call, and only on that path.
- **A small decision classifier.** A cheap model, or a second short prompt,
  labels the turn as answer, ask, decline or call, and the gate blocks calls
  the classifier disagrees with. This is When2Call's four-way question,
  asked directly.
- **Constrained decoding** for enums and slot formats where the provider
  supports it.

### 11.5 L4: Choose the model by measurement

When2Call shows that F1 varies a lot between model families of similar size,
and that BFCL scores don't predict it. Run §12 across the candidate text
models (current Gemini Flash, a larger Gemini, an alternative vendor) and
speech frontends. Pick by our numbers, not leaderboards.

### 11.6 L5: Train the decision model (only if we self-host one)

If option E or a self-hosted router is chosen, apply the paper's recipe:

- start from When2Call's `train_pref` set plus synthetic pairs generated from
  **our** registry (each agent's schema yields call, missing-slot, unservable
  and answerable variants);
- use **preference optimisation (RPO or DPO), not plain SFT**, with half the
  pairs being corrupted-argument tool calls, to avoid the over-caution and
  BFCL regression seen with SFT;
- regression-test on both §12 and a plain tool-calling set, since the paper's
  point is that gains on one can cost the other.

## 12. Evaluation: a When2Call-style voice eval set

This is the yardstick for Part I and Part II. It extends When2Call in three
ways: speech input, our own five outcomes (with Answer valid), and voice-
specific cases.

**Construction.** For each of ~30 representative registered agents, generate
items from the agent's name, description and slots:

| Case | Correct outcome | Example |
|---|---|---|
| Complete request | Dispatch or Bridge | "Have Tabletop book Nopa for two at seven" |
| Missing slot | Ask | "Have Tabletop book Nopa" |
| Unservable | Decline | "Have Tabletop refund my flight" |
| Answerable directly | Answer | "What's 15% of 80?" |
| Unknown agent | Decline | "Ask Zorblax to…" |
| Ambiguous name | Ask | "Call Atlas" with two Atlas agents |
| Garbled name | Dispatch to the right agent | Vosk's "kai ross" |
| Late self-correction | Dispatch with the later value | "…at seven, no, eight" |
| No-tool session | Answer or Decline, never a call | When2Call's zero-tool items |
| Indirect route, personal data | Route (M2) | "How many calories have I had today?" → MyFitnessPal |
| Indirect route, general knowledge | Answer (M2) | "How many calories in a banana?" with MyFitnessPal `on_request` |
| Connect vs dispatch | The mode `decide_mode` picks (M3) | "Log a bagel for breakfast" → dispatch. "Help me plan meals" → connect |
| Task reference | Act on the right task, or ask (M5) | "Cancel that" with two active tasks → Ask |
| Notify flag | `device` only when asked (M5) | "…and let me know" → `device`. No mention → default |
| Status question | Answer from the record (M5) | "Is my table booked?" while the task is still running → "Still in progress" |

Render each item as audio: TTS in several voices, plus a set of **real
recordings** from us (FDB-v3 shows synthetic speech hides disfluency
failures). Also include When2Call's own text test items as a text-only
control.

**Metrics** (per architecture and model):

- macro-F1 over the five outcomes;
- tool hallucination rate, parameter hallucination rate, wrong-agent rate;
- **ask rate on complete requests** (over-caution) and read-back acceptance
  rate;
- time to first audio, and end-to-end task latency.

**Scoring.** Tool calls are structured, so score them exactly. Classify free
speech into outcome classes with an LLM judge, as When2Call does for
free-form output, and spot-check by hand.

**Gate for shipping any change:** tool and parameter hallucination must not
rise, and the ask rate on complete requests must not exceed an agreed ceiling.

## 13. Recommendation and decisions

1. Build §12 first, starting with ~200 items.
2. Ship L0, L1 and L2 on the current cascade. They are needed under every
   Part I option.
3. Add L3 only where §12 shows residual failures, most likely intent-routed
   dispatch.
4. Treat L5 as conditional on choosing option E.
5. Build the §10 modes in this order: `route_to_agent` with M1/M2/M3 (no
   schema change beyond the registry fields), then agent tasks in `tasks` with
   `notify`, then `task.closed`/`task.delivered`, then the ESP32 `task_result`
   wake. The wake depends on fixing the task-owner identity mismatch first.

**Decisions for you**

- **2a.** Is the validation gate (L2) the primary defence (proposed), rather
  than relying on prompt and model choice?
- **2b.** Read-back confirmation for side-effecting agents: always, never, or
  only above a risk level the agent declares?
- **2c.** Argument grounding strictness: block ungrounded slots (proposed), or
  only log them at first?
- **2d.** The over-caution ceiling: what ask rate on complete requests is
  acceptable? 5% is a suggested starting point.
- **2e.** Who records the real-speech eval clips, and how many?

---

# Part III and appendices

## 14. How the two decisions interact

| | A Streamed cascade | B Half-cascade | C Native S2S | D Frontend/backend | E Thinker–Talker (self-hosted) |
|---|---|---|---|---|---|
| Where the call is decided | Text LLM | Audio LLM, text out | Provider model | Text backend | Thinker (text) |
| L2 gate before execution | ✔ | ✔ | ✔ | ✔ | ✔ |
| L2 gate before the model **speaks** | ✔ nothing is spoken until we choose | ✔ | ✘ the model may say "Done!" before or instead of calling | ~ the frontend may pre-announce | ✔ edit the Thinker's text |
| Deterministic Ask or Decline lines | ✔ TTS | ✔ TTS | ~ inject and hope it's verbatim | ~ | ✔ |
| L5 training possible | Only on a self-hosted text LLM | If open weights | ✘ | On the backend | ✔ |

The consequence: **the further we move toward native speech-to-speech, the
less of Part II's defence we can apply to what the user hears.** Options D and
E are the two MMLLM shapes that keep Part II fully in force. That is the main
reason this doc prefers them over C.

## 15. Appendix A: the foundation (router, protocol v2, dispatcher, registry)

Everything below is implemented on branch
`claude/orchestrator-upgrade-routing-o7cnph` (commit `1bed5a8`). None of it is
merged. The earlier draft has the full pseudocode. This is the summary both
Parts depend on.

**Why it was needed at 1,000+ agents.** v1 pasted every agent into the system
prompt (20–40k tokens, past the context window eventually). It ran a difflib
scan over all rows on the event loop, silently dialled the closest fuzzy match
or a default URL, had no health checks or connect timeout, and could only
bridge audio, not dispatch tasks.

| Piece | Module | What it does |
|---|---|---|
| **Agent router** | `app/agent_router.py` | In-memory snapshot of active agents, refreshed every 30 s or when a registry write invalidates it, never touching the DB on the hot path. Exact, prefix, token, phonetic (`"kairos" = "cairo's" = "kai ross" → krs`), difflib and IDF intent scoring. **Returns a decision**: `matched`, `ambiguous` (top two within 0.12), `none` (below 0.55) or `wrong_mode`. Circuit breaker per agent. Bounded prompt summary (≤30 agents listed; otherwise "use `find_agents`"). Lookups take milliseconds at 1,500 agents |
| **Protocol v2** | `app/agent_protocol.py`, `BRIDGE_PROTOCOL.md` | One endpoint, `mode: bridge or task` in the hello. Task frames: `dispatch`, `accepted`, `rejected`, `progress`, `input_required`, `input`, `result`, `cancel`. Backward compatible with v1 agents. New close code 4405 |
| **Task dispatcher** | `app/task_dispatcher.py` | State machine (queued → dispatching → accepted → running ⇄ input-required → terminal). **A named agent is never substituted**, while intent-routed tasks fail over among at most 3 agents. **No retry after accept**, plus an `idempotency_key`. Pooled WS per agent, bounded limits, results held while bridged and announced at the next session if the user is offline. Tasks are in memory today; §10.5 moves them to the shared `tasks` table and adds `notify` and agent closing notices |
| **Registry and API** | `agents_registry.py`, routes | JSON indexes; `modes`, `max_concurrency`, `last_seen`; `/api/agents/search`, `/api/dispatch/*`, `/api/router/stats`; optional `DISPATCH_API_TOKEN` |

**Known gaps:** heartbeats invalidate the router too often (invalidate only on
routing-relevant changes and coalesce); `last_seen` is unused; tool-role
messages are dropped by `gemini_client`; the task store is in memory only;
there are no metrics; thresholds are untuned. Real Gemini tool selection was
**never tested**, which is the gap §12 closes.

**Security notes:** open registration allows name impersonation (reserve
names per owner or require a token); the dispatch API should fail closed
without a token; agents can't verify the orchestrator (add an HMAC in the
hello); agent output is untrusted (§11.3).

## 16. Appendix B: full decision checklist

| # | Decision | Proposed |
|---|---|---|
| **1a** | Measure, then streamed cascade (A) first | Yes |
| **1b** | MMLLM shape if we move | Frontend/backend split (D) |
| **1c** | Prototype frontend | Gemini Live |
| **1d** | Two voices acceptable | Open |
| **1e** | GPU budget for self-hosted Qwen3-Omni (E) | Open. One-day hook spike first |
| **2a** | Validation gate as primary defence | Yes |
| **2b** | Read-back for side-effecting agents | Yes, per agent `side_effects` flag |
| **2c** | Block ungrounded slots | Yes, lenient thresholds, tuned on logs |
| **2d** | Over-caution ceiling | 5% ask rate on complete requests |
| **2e** | Real-speech eval clips | Open |
| A1–A3 | Lexical and phonetic routing; thresholds 0.55 / 0.12 / 0.92; decline unknown names | Yes |
| B1–B4 | One endpoint with `mode`; `user_id="orchestrator"` in task hellos; A2A-style state names; silent progress | Yes / Yes / Open / Yes |
| **M-a** | One `route_to_agent` tool plus a code-side connect/dispatch policy | Yes |
| **M-b** | Indirect routes: announce and connect with a barge-in window | Yes |
| **M-c** | Registry fields `domains`, `intent_aliases`, `routing_policy`, `user_data` | Yes, backfilled for Kairos and MyFitnessPal |
| **M-d** | Durable agent tasks | **Done:** in `tasks`, `agent_tasks` dropped (flips C1) |
| **M-e** | Default `notify` | `next_session` |
| **M-f** | Wake limits | Quiet hours, at most 4 per hour per user |
| **M-g** | Protocol 2 additions | `task.closed` and `task.delivered` first, then `task.update` and `task.created` |
| C1–C4 | ~~In-memory tasks~~ **Postgres `tasks`** (M-d); 8 per user and 120 s; never substitute a named agent; no retry after accept | Changed / Yes / Yes / Yes |
| D1–D3 | Deterministic spoken results; hold while bridged; merge tools | Yes / Yes / **Merge into `route_to_agent` and `manage_task`** (changed, §10.7) |
| E1–E2 | Dispatch token required; `/api/agents` sorted by name | **Require it (fail closed)** / Yes |

## 17. Sources

- Ross, Mahabaleshwarkar, Suhara. *When2Call: When (not) to Call Tools.*
  NAACL 2025. [arXiv 2504.18851](https://arxiv.org/abs/2504.18851) ·
  [HTML](https://arxiv.org/html/2504.18851v1) ·
  [code and data](https://github.com/NVIDIA/When2Call)
- Qwen Team. *Qwen3-Omni Technical Report.*
  [arXiv 2509.17765](https://arxiv.org/abs/2509.17765)
- Qwen3.5-Omni report: [arXiv 2604.15804](https://arxiv.org/abs/2604.15804).
  Qwen3.8-Omni-Flash report: [arXiv 2609.25611](https://arxiv.org/abs/2609.25611).
  Both carried over from the earlier draft and not re-read
- Alibaba Cloud Model Studio,
  [Qwen-Omni-Realtime](https://www.alibabacloud.com/help/en/model-studio/realtime)
  and [Qwen-Omni](https://www.alibabacloud.com/help/en/model-studio/qwen-omni)
- [Qwen3.8 Omni Flash Realtime API overview](https://empiriolabs.ai/blog/qwen3-8-omni-flash-realtime-api) ·
  [DataNorth on Qwen3.8 Omni](https://datanorth.ai/news/alibaba-qwen3-8-adds-1m-context-omni-analysis-and-60-language-live-translation)
  (secondary)
- Hu et al. *A frontend-backend architecture for tool calls in full-duplex
  speech models.* [arXiv 2609.19334](https://arxiv.org/abs/2609.19334)
- *Full-Duplex-Bench-v3.* [arXiv 2604.04847](https://arxiv.org/abs/2604.04847)
- Speech LLMs as implicit ASR: [arXiv 2602.17598](https://arxiv.org/abs/2602.17598)
  (carried over, not re-read)
- [Qwen-Live-Harness](https://github.com/QwenLM/Qwen-Live-Harness) (carried over)
- Repo: `app/developer_ws/DESIGN.md`, `app/developer_ws/pipecat_llm.py`,
  `OCI_INFRASTRUCTURE.md`
