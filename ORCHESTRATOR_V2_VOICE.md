# Orchestrator v2, decision 1: voice architecture

**Status:** proposal for review. This is one of two design docs for the
orchestrator v2. The other is
[ORCHESTRATOR_V2_TOOL_CALLS.md](ORCHESTRATOR_V2_TOOL_CALLS.md) (decision 2:
reliable tool calls, orchestrator modes and the agent protocol).

**The decision:** should the orchestrator's voice loop stay a text LLM between
STT and TTS, or move to a multimodal speech LLM (MMLLM)? If an MMLLM, which
shape: hosted realtime, frontend/backend split, or a self-hosted Qwen
Thinker–Talker?

The two decisions are coupled. The orchestrator's job is tool decisions, and
the voice architecture decides where that decision is made and whether it can
be intercepted before anything is spoken or executed (§14).

**Section numbers are shared** with the companion doc so cross-references stay
stable: §0–7 and §14 are in [ORCHESTRATOR_V2_VOICE.md](ORCHESTRATOR_V2_VOICE.md);
§8–13 and the appendices (§15–16) are in
[ORCHESTRATOR_V2_TOOL_CALLS.md](ORCHESTRATOR_V2_TOOL_CALLS.md). A § number not in
this file is in the other one.

**Sourcing.** Qwen3-Omni architecture facts come from its technical report.
Hosted-API facts come from vendor docs and secondary write-ups, marked
*(verify)* where only a secondary source was available. Latency figures in
§1.1–1.2 were measured on production on 2026-10-06.

---

## Contents

0. Summary of recommendations
1. Context: the orchestrator today (1.1 measured latency, 1.2 STT stall root cause, 1.3 re-measured after the fix)
2. What the orchestrator needs from its voice loop
3. The option space
4. MMLLM options in detail
5. Deep dive: Qwen Omni Thinker–Talker
6. Scoring the options
7. Recommendation and decisions
14. How the two decisions interact
16. Decision checklist (voice)
17. Sources

---

## 0. Summary of recommendations

1. **Measured (§1.1).** End of speech to first TTS audio takes **4.2–5.8 s** on
   the server. The breakdown: **STT 1.35–2.8 s** (Vosk decodes slower than
   real time on the 2-core VM, so audio backs up), LLM 1.4–2.3 s, endpointing
   0.8–1.0 s, TTS first chunk 0.4–0.6 s. The STT stall came from creating a
   fresh Vosk recognizer per utterance. **Fixed and deployed** with a pool
   of warm recognizers (§1.2). Re-measured (§1.3): STT is now 0.23–0.51 s,
   and the total is **2.9–4.5 s** (median 3.0 s, down from 5.65 s). **The
   LLM is now the largest hop**, so stream it next.
2. **Decision 1:** fix the STT backlog and stream the current cascade now
   (cheap, keeps exact speech; target ~2.5–3 s, against 4.2–5.8 s today).
   Then prototype a **frontend/backend split** in which a speech model holds the
   conversation and a text LLM backend owns every tool decision. Keep
   **self-hosted Qwen3-Omni** as the option that uniquely lets us edit the
   Thinker's text before the Talker speaks it, if exact in-voice acks or data
   residency become requirements.
3. **Gate any switch on the tool-call eval** (§12, in the tool-calls doc). Latency
   gains don't count if tool-decision accuracy drops.

---

## 1. Context: the orchestrator today

The orchestrator is the `/ws/developer/{user_id}` voice session in
`app/developer_ws/`. It is not Kairos (`/ws/{user_id}`), which already runs on
a native-audio Gemini model and is just another agent behind the router.

```
mic ─► Opus decode ─► Silero VAD (~0.8 s silence; 1.0 s timer fallback) ─► Vosk STT (streamed, then finalize)
    ─► Gemini text (gemini-3-flash-preview, one non-streamed generateContent call, with tools)
    ─► whole reply text ─► Piper TTS ─► Opus encode ─► downlink
```

Facts from the code that matter for both decisions:

- **The Gemini call isn't streamed.** `CustomGeminiLLMService._call_gemini`
  waits for the full response, so Piper can't start until Gemini finishes. The
  "thinking cue" (`thinking_cue.py`, shipped in #76) masks this gap with a
  soft pulse but doesn't shorten it.
- **Vosk decodes slower than real time on this VM** (§1.1), so STT, not the
  LLM, is the largest hop.
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

### 1.1 Measured latency (2026-10-06)

**Method.** A live conversation through the test website (user
`2ba330c0…`, 07:40–07:41 UTC) against production on the OCI VM. Hop times
come from the app's existing log lines:
- `audio batch … has_speech`, `vad STOPPED`, `timer FIRING`;
- `transcript=… finalize_ms`, `llm gemini_ms`;
- `tts … ttfc_ms total_ms`, logged when TTS finishes, so TTS start = log time − `total_ms`.

A follow-up benchmark ran Piper-synthesised phrases through the production
Vosk model in 256 ms chunks, inside the app container while it was idle. The
sample is small (3 orchestrator turns), so treat these as a first measurement,
not a distribution.

**Per turn: from the last uplink batch containing speech to the first TTS audio
chunk ready on the server**

| Hop | T1 "hey how's it going" → reply | T2 "can i speak to cairo's" → bridge tool | T3 "okay bye" → `end_conversation` |
|---|---|---|---|
| Endpointing (last speech → stop signal) | 0.79 s (Silero) | 1.00 s (silence timer, VAD 28 ms later) | 1.00 s (timer; Silero never fired on the short utterance) |
| **STT** (stop signal → transcript) | **1.35 s** (finalize 0.15) | **1.81 s** (finalize 0.18) | **2.82 s** (finalize 0.29) |
| Pipeline (transcript → Gemini request) | 0.02 s | 0.02 s | 0.02 s |
| **LLM** (`gemini_ms`, non-streamed) | **1.41 s** | **2.25 s** | **1.38 s** |
| LLM → TTS start | 0.04 s | 0.06 s (bridge ack is a handler string) | 0.00 s |
| TTS first chunk (Piper `ttfc`) | 0.60 s | 0.64 s | 0.43 s |
| **Total, server side** | **4.20 s** | **5.81 s** | **5.65 s** |

Not measured: browser capture and uplink batching (uplink batches arrive every
~256 ms, adding up to 0.26 s), network both ways, and browser playback
buffering. Real time to first audio for the user is therefore somewhat
**above** these totals.

**Where the time goes** (median turn, about 5.6 s):

| Hop | Share | Finding |
|---|---|---|
| **STT** | **~35–50%** | **The largest hop, and not the one we assumed.** `finalize_ms` (0.15–0.29 s) looks cheap, but the stop frame waits behind a backlog. Audio is fed to Vosk chunk by chunk in the pipeline's single frame loop, and the benchmark shows the production model (`vosk-model-en-us-0.22-lgraph`) decoding **slower than real time** on this 2-core ARM VM: real-time factor **1.2–2.6**, mean 288 ms per 256 ms chunk, with a **1.5–3.3 s stall on the first decoded chunk of each utterance**. **Root cause found** (§1.2): the stall is per recognizer, and the code creates a new one per utterance |
| **LLM** | ~25–40% | 1.4–2.3 s for one non-streamed `generateContent`. The tool-call turn was the slowest |
| Endpointing | ~18–20% | Silero's 0.8 s stop, or the 1.0 s silence timer when Silero misses a short utterance |
| TTS | ~8–12% | **Piper is fast** (real-time factor ~0.2), but the first chunk waits for the whole first sentence (0.4–0.6 s) |
| Glue | <1% | Pipeline hand-offs are 20–60 ms |

**Other observations from the same session**

- **Bridge connect** (tool call → remote `ack`): **0.92 s**. That splits into
  0.69 s to send the `hello` (TLS through Caddy to Kairos in the same app
  container, which opens two DB connections on accept) and 0.24 s to the
  `ack`. The spoken "Connecting you…" ack was ready 0.64 s after the tool call,
  so the user hears it while the dial is in flight.
- **Connect greeting:** first audio 0.35 s after the WebSocket opened.
- **Vosk accuracy, from the benchmark:** "kairos" → "cairo's", "nopa" →
  "notebook", "tonight" → "to night". Name garbles are routine, which supports
  the router's phonetic matching and argument grounding (§11.3).

### 1.2 Root cause of the STT stall (2026-10-06)

A benchmark in the production container, using Piper-synthesised phrases fed
to `vosk-model-en-us-0.22-lgraph` in 256 ms chunks, isolated the cause.

| Setup | First decoded chunk | Real-time factor (lower is better; >1 means it can't keep up) | Finalize |
|---|---|---|---|
| **A. New recognizer per utterance (today's code)** | **1.5–3.3 s** | **1.3–3.4** | 0.16–1.06 s |
| E. Same phrase on two fresh recognizers in a row | 2.1 s **both times** | 1.75 | 1.06 s |
| B. One recognizer reused, first pass over 4 phrases | 2.1 s for the first utterance only, then falling | 0.46–1.74 | 0.16–1.06 s |
| **B. One recognizer reused, once warm** | **0.11–0.14 s** | **0.15–0.19** | **0.09–0.13 s** |
| C. Fresh recognizer warmed with 1 s of silence | 0.37–0.75 s | 0.7–1.0 | 0.3–1.0 s |
| D. Fresh recognizer warmed with one speech clip (≈6 s of CPU) | 0.2–0.37 s | 0.37–0.41 | 0.16–0.23 s |

- **The cost is per recognizer, not per model or per process** (row E). A fresh
  `KaldiRecognizer` pays it every time. Constructing one takes only ~0.1 s,
  so the time goes into its first decoding. The likely mechanism: the `lgraph`
  model composes its decoding graph on the fly, and each recognizer builds its
  own composition cache from scratch. Mechanism inferred, effect measured.
- **A warm recognizer runs about 10× faster than real time** (row B, warm),
  so the backlog disappears entirely, and finalize drops to ~0.1 s.
- **`FinalResult()` resets the recognizer for the next utterance but keeps
  the warm cache.** Transcripts were identical with and without reuse.
- **Memory:** about 60–65 MB per warmed recognizer, plateauing after the first
  utterances (763 → 838 MB RSS with one, 886 MB with two). The app container
  uses ~745 MiB of its 3 GiB limit.

**Fix (implemented 2026-10-06, image `step10`): a process-wide pool of warm
recognizers** (`RecognizerPool` in `app/developer_ws/stt.py`).
- `StreamingTranscriber` borrows a recognizer from the pool at
  `UserStartedSpeakingFrame` and returns it after `finalize()`.
- At startup, the pool is warmed in the background with one Piper-synthesised
  clip containing common phrases and agent names (≈6 s of CPU per recognizer).
- Pool size is set by an env var (default 2: two concurrent talkers). When
  the pool is empty, a cold recognizer is created and kept, up to a cap.
- Each recognizer is recycled after N utterances to bound cache growth.

**Deployed** as `codex-app-backend:step10-bce43ee2dd3d` (revision
`80aa930-worktree`) on 2026-10-06. A smoke test of the image against the real
models found:
- every utterance got a warm recognizer;
- the largest chunk took 141–360 ms (the cold stall was 1.5–3.3 s);
- decoding ran at 0.21–0.41× real time (cold: 1.3–3.4×);
- finalize took 163–191 ms.

In production, startup logs `vosk recognizer pool warmed: 2 in 22.4s`.

- **The pool resets on every deploy or restart.** It lives in process
  memory, so each new container re-warms it in the background, which took
  about 22 s here. A turn that starts during that window gets a cold
  recognizer (`warm=False` in the `transcript=` log line) and pays the old
  stall once. Persisting the cache across restarts isn't possible: it is
  internal decoder state.
- **The app container is capped at 1 CPU** (`APP_CPUS`, default `1.0`, in
  `docker-compose.oci.yml`) on a 2-core VM. All in-container numbers above
  were measured under that cap. Vosk, Piper and Silero share the one CPU, so
  raising the cap is a cheap follow-up lever. The worker uses 0.25.

**Expected effect:** the STT hop goes from **1.35–2.82 s to ~0.1–0.3 s**,
bringing server-side end-of-speech → first audio from **4.2–5.8 s to roughly
2.7–3.5 s** before any LLM streaming. This is the single largest latency win
available, and it needs no new service or vendor.

### 1.3 After the fix: re-measured (2026-10-06, image `step10`)

The same three-turn script was repeated through the test website at
08:08–08:09 UTC, measured the same way as §1.1. All three utterances got a
warm recognizer (`warm=True`).

| Hop | T1 "hey how's it going" | T2 "can you connect me to cairo's" → bridge | T3 "okay bye" → `end_conversation` |
|---|---|---|---|
| Endpointing | 0.77 s (Silero) | 1.00 s (timer) | 0.76 s (Silero) |
| **STT** (stop → transcript) | **0.23 s** (was 1.35) | **0.23 s** (was 1.81) | **0.51 s** (was 2.82) |
| LLM (`gemini_ms`) | 1.40 s | 2.44 s | 1.47 s |
| LLM → TTS start | 0.03 s | 0.07 s | 0.00 s |
| TTS first chunk | 0.58 s | 0.75 s | 0.15 s |
| **Total, server side** | **3.02 s** (was 4.20) | **4.48 s** (was 5.81) | **2.89 s** (was 5.65) |

- **STT dropped from 1.35–2.82 s to 0.23–0.51 s**, as predicted by §1.2. The
  median turn went from **5.65 s to 3.02 s (−2.6 s, −46%)**.
- **The LLM is now the largest hop**: 1.4–2.4 s, about half of every turn.
  The next levers, in order, are streaming Gemini into Piper (§7 step 2),
  tightening endpointing (0.76–1.0 s, the second-largest hop), and raising the
  container's 1-CPU cap.
- The bridge connect took 0.69 s (was 0.92). The "Connecting you…" first
  chunk took 0.75 s, probably slower because it was synthesised while the dial
  was running on the same CPU.
- **Still a small sample** (3 turns per run). The fixed STT backlog is clear
  well beyond the noise, but the other hops' variation is within the noise.

The foundation built on branch `claude/orchestrator-upgrade-routing-o7cnph`
(router, protocol v2, task dispatcher; Appendix A) is assumed by both parts. It
turns "every agent is a tool" into a bounded tool surface: `find_agents`,
`dispatch_task`, `start_remote_audio_bridge`, plus task management.

---

## 2. What the orchestrator needs from its voice loop

An orchestrator has requirements a single conversational agent doesn't. These
are the criteria in §6.

| # | Requirement | Why it is orchestrator-specific |
|---|---|---|
| R1 | **Tool-decision accuracy on spoken input** | A wrong call means a real task dispatched to a real agent, possibly with side effects (tool-call doc) |
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

The point for us: D keeps **the tool decision in text**, where the tool-call doc's
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
   the Thinker as text, so the router, dispatcher and tool-call validation gate
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
| R7 Latency | ✘ today: 4.2–5.8 s measured (§1.1). ~ once STT is fixed and the LLM streams | ✔ removes the STT hop, the largest one | ✔ | ✔ | ~ depends on GPU and concurrency |
| R8 Turn-taking | ~ our VAD heuristics | ~ | ✔ native | ✔ native | ~ we build it |
| R9 One voice | ✔ Piper only | ✔ | ✘ provider plus Piper | ✘ unless relayed text is routed through the model | ✔ |
| R10 Cost, privacy, infra | ✔ free, local | ~ GPU or hosted | ~ metered, audio leaves the VM | ~ two models | ✘ GPU servers |

The pattern: **A and D dominate.** A is the cheapest and keeps every
guarantee. D gives native turn-taking and latency while keeping tool
decisions in text. E is the only option scoring well on R2, R3 and R9 at once,
but it costs infrastructure and its open weights lag on tool use.

## 7. Recommendation and decisions

1. **Measured** (§1.1): 4.2–5.8 s server-side, and **STT is the largest
   hop**, not the LLM. That changes what "option A first" has to include.
2. **Ship option A, with STT fixed first.** In order of expected gain:

   | Step | Expected saving (median turn) | Notes |
   |---|---|---|
   | **Make STT keep up with real time**: **done** (§1.2–1.3). Measured saving 1.1–2.3 s per turn | **~1.2–2.5 s.** The backlog goes, leaving only finalize (~0.2–0.3 s) | **(a) Done as analysis, ready to build:** reuse a pool of warm recognizers (§1.2). Measured warm real-time factor is 0.15–0.19, which removes the backlog. If (a) is enough, the alternatives become optional: **(b)** Switch to a smaller Vosk model (e.g. `vosk-model-small-en-us`); faster, but less accurate and worse on names, so measure it. **(c)** Use a hosted streaming STT with phrase hints for agent names; audio leaves the VM (R10). **(d)** Option B: let Gemini hear the audio directly, which removes the hop entirely (and with it the STT garbles) but makes the LLM call slower by an unmeasured amount |
   | **Stream the LLM** into Piper sentence by sentence | ~0.5–1 s *(estimate: `gemini_ms` minus time to first sentence, not measured)* | Smaller than assumed, because replies are short. Tool-call turns gain nothing: the handler speaks when the call completes |
   | **Tighten endpointing** | ~0.2–0.4 s | Silero stop 0.8 → 0.5–0.6 s, and a shorter fallback timer, so short utterances like "okay bye" don't wait the full 1.0 s. Risk: cutting users off mid-pause. Tune against recordings |
   | **TTS first chunk** | ~0.2–0.3 s | Synthesise the first clause rather than the whole first sentence |

   Rough target with all four: **~2.5–3 s** server-side, against 4.2–5.8 s
   today. Hosted speech-to-speech is reported at 0.3–0.6 s *(verify)*, so the
   cascade stays several times slower even when tuned. That is the case for
   step 3.
   - **Instrument permanently:** log one structured line per turn with the
     timestamps used in §1.1 (speech end, stop signal, STT start and end, LLM
     start and end, TTS first chunk, first downlink byte), so the eval (§12)
     reports latency per option.
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

- **1a.** Option A first, **starting with the STT backlog** (proposed, §1.1
  and §7 step 2)? Or go straight to an MMLLM prototype? Option B also removes
  the STT hop.
- **1f.** STT fix: (a) the warm-recognizer pool (§1.2; root cause measured,
  expected to save 1.2–2.5 s per turn) first (proposed). Then (b) a smaller
  model, (c) hosted streaming STT, or (d) Gemini audio input only if accuracy
  on names, not latency, becomes the problem.
- **1b.** If MMLLM: frontend/backend split D (proposed), full native C, or
  self-hosted Thinker–Talker E?
- **1c.** Prototype frontend: Gemini Live (proposed), OpenAI Realtime, or Qwen
  Omni Realtime? Qwen's region latency counts against it.
- **1d.** Are two voices acceptable (provider voice plus Piper for relayed
  `say`)? If not, the choice narrows to A, B or E.
- **1e.** Is a GPU spend for option E on the table at all? If not, E drops out
  and Qwen is only a hosted candidate for C or D.

---

## 14. How the two decisions interact

| | A Streamed cascade | B Half-cascade | C Native S2S | D Frontend/backend | E Thinker–Talker (self-hosted) |
|---|---|---|---|---|---|
| Where the call is decided | Text LLM | Audio LLM, text out | Provider model | Text backend | Thinker (text) |
| L2 gate before execution | ✔ | ✔ | ✔ | ✔ | ✔ |
| L2 gate before the model **speaks** | ✔ nothing is spoken until we choose | ✔ | ✘ the model may say "Done!" before or instead of calling | ~ the frontend may pre-announce | ✔ edit the Thinker's text |
| Deterministic Ask or Decline lines | ✔ TTS | ✔ TTS | ~ inject and hope it's verbatim | ~ | ✔ |
| L5 training possible | Only on a self-hosted text LLM | If open weights | ✘ | On the backend | ✔ |

The consequence: **the further we move toward native speech-to-speech, the
less of the tool-call doc's defence we can apply to what the user hears.** Options D and
E are the two MMLLM shapes that keep those defences fully in force. That is the main
reason this doc prefers them over C.

## 16. Decision checklist (voice)

| # | Decision | Proposed |
|---|---|---|
| **1a** | Streamed cascade (A) first, STT backlog fixed first (measured: STT is the largest hop) | Yes |
| **1f** | First STT fix | Warm-recognizer pool (§1.2). Others only if name accuracy becomes the problem |
| **1b** | MMLLM shape if we move | Frontend/backend split (D) |
| **1c** | Prototype frontend | Gemini Live |
| **1d** | Two voices acceptable | Open |
| **1e** | GPU budget for self-hosted Qwen3-Omni (E) | Open. One-day hook spike first |

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
