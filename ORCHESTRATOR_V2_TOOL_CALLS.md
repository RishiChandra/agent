# Orchestrator v2, decision 2: reliable tool calls, modes and protocol

**Status:** proposal for review. This is one of two design docs for the
orchestrator v2. The other is
[ORCHESTRATOR_V2_VOICE.md](ORCHESTRATOR_V2_VOICE.md) (decision 1: voice
architecture, including measured latency).

**The decision:** how do we make sure the orchestrator calls a tool only when
it should, with the right agent and real arguments, and otherwise asks,
declines or answers? This doc also defines the orchestrator modes (routing,
connect vs dispatch, task CRUD) and the Protocol 2 changes that support them.
The wire-level draft lives in
[`app/developer_ws/BRIDGE_PROTOCOL.md`](app/developer_ws/BRIDGE_PROTOCOL.md).

**Context** (detail in the voice doc, §1): the orchestrator is the
`/ws/developer/{user_id}` voice session. Today it runs Silero VAD → Vosk STT →
Gemini (text, with tools) → Piper TTS. Tool handlers speak fixed sentences.
Tool results don't reach Gemini. Vosk garbles agent names. The router,
protocol v2 and dispatcher foundation is summarised in Appendix A (§15).

**Section numbers are shared** with the companion doc so cross-references stay
stable: §0–7 and §14 are in [ORCHESTRATOR_V2_VOICE.md](ORCHESTRATOR_V2_VOICE.md);
§8–13 and the appendices (§15–16) are in
[ORCHESTRATOR_V2_TOOL_CALLS.md](ORCHESTRATOR_V2_TOOL_CALLS.md). A § number not in
this file is in the other one.

**Sourcing.** The When2Call claims come from the paper itself (arXiv HTML,
read October 2026). Hosted-agent facts (§10.10) come from launch-week
coverage and are marked *(verify)*.

---

## Contents

0. Summary of recommendations
8. What When2Call found
9. What it means for the orchestrator
10. Orchestrator modes and the tool calls behind them (10.8 protocol changes, 10.10 hosted agents)
11. The optimisation space: a layered defence
12. Evaluation: a When2Call-style voice eval set
13. Recommendation and decisions
15. Appendix A: the foundation (router, protocol v2, dispatcher, registry)
16. Decision checklist (tool calls, modes, protocol)
17. Sources

How the voice architecture limits these defences is covered in §14, in the
voice doc.

---

## 0. Summary of recommendations

1. **Build the tool-call eval first (§12).** It is the yardstick for both
   decisions. When2Call shows that benchmark scores on "correct calls" don't
   predict "should I call at all", and Full-Duplex-Bench-v3 shows speech models
   lose accuracy on real spoken input. We need our own numbers.
2. **Decision 2:** don't rely on the model. Layer the defences: a smaller tool
   surface, an explicit four-way prompt, a **deterministic validation gate**
   between the model's tool call and its execution (router decisions, schema
   checks, argument grounding, read-back for side effects), then model
   selection by eval. Use preference training (RPO/DPO, not plain SFT) only if
   we ever self-host the decision model.
3. **Orchestrator modes (§10):** direct and indirect routing over Protocol 1,
   a code-side policy for connecting versus dispatching, and Protocol 2 tasks
   with full CRUD. Tasks live in the single `tasks` table (`agent_tasks` dropped; scheduling optional via `is_scheduled`), carry a `notify` flag
   (`device` wakes the ESP32, `next_session`, `silent`), and the owning agent
   is always told how its task ended.
4. **Future goal (§10.10):** dispatch to hosted personal agents such as
   Muse and Dots. They have no public server and no inbound API, so they
   need an agent-initiated "pull" binding. Not in v2 scope, but v2 keeps
   the envelope transport-neutral so it stays cheap to add.

---

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
  optional `task.created` message from the agent (§10.8), carrying a task_id the
  orchestrator then tracks as if it had dispatched it). That's how "I'll text
  you when the booking confirms" becomes a tracked task instead of a promise.

### 10.4 M4: task dispatch and monitor

This is the dispatcher as built (Appendix A): resolve with `mode=task`, never
substitute a named agent, fail over only on intent routes and only before
acceptance, no re-dispatch to another agent after `task.ack`, relay `input_required` to the
user, deliver results. Two additions follow from M5:

- every task carries a **`notify`** setting (§10.6);
- tasks are **persisted** (§10.5), because a result that has to wake the
  ESP32 must survive a restart and outlive the session that created it;
- a task **may** carry a deadline (optional). If it does, the deadline is
  scheduled as a job once the agent acks, so the orchestrator is pinged on
  time with the task's context (§10.8.10).

### 10.5 M5: task CRUD

**Two kinds of task existed, and they are now one table.** Before this change:

| | User tasks (`tasks` table) | Agent tasks (dispatcher) |
|---|---|---|
| Created by | Kairos voice tools, `POST /tasks` | `dispatch_task`, `POST /api/dispatch` |
| Stored | Postgres. `time_to_execute` → `jobs` row → MQTT wake (SCHEDULER.md) | **In memory**. An `agent_tasks` table existed in `ai_pin_db` with 0 rows and no code using it |
| Status | `pending` / `completed` | queued … succeeded / failed / cancelled |
| User notified | ESP32 wake at `time_to_execute` | Only if a session is open, or at the next session |

**Decision (done): `tasks` is the master table** for Kairos reminders and
orchestrator agent tasks, and `agent_tasks` is dropped. This also settles C1:
Protocol 2 tasks are persisted, in `tasks`. Applied to `ai_pin_db` on
2026-10-06. The full schema is in `DATABASE.md`.

**Not every task is scheduled.** `time_to_execute` was already nullable. The
schema adds `is_scheduled`, a generated column (`time_to_execute IS NOT
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
                                               (timed_out only for a passed deadline with on_deadline = cancel, §10.8.10)
  time_to_execute    timestamptz               NULL = unscheduled
  enqueue_sequence_id bigint                   → jobs.id of the pending wake (no FK)
  ── added 2026-10-06 ──
  is_scheduled       bool  generated           time_to_execute IS NOT NULL
  kind               text  not null 'reminder' reminder | agent_task
  created_by         text                      kairos | app | orchestrator | agent (NULL for older rows)
  agent_id           uuid  → agents(agent_id)  required when kind = agent_task. ON DELETE SET NULL
  notify             text  not null 'device'   device | next_session | silent (§10.6). The orchestrator sets it explicitly for agent tasks
  question           text                      set while input_required
  result             jsonb                     {say (≤ 2,000 chars, spoken verbatim), output (≤ 256 KB), error}
  finished_at, delivered_at, agent_informed_at   timestamptz
  deadline_at        timestamptz               OPTIONAL. NULL = no deadline. When set, a deadline job is scheduled after the ack (§10.8.10)
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
| **Update** | `manage_task(action="update", task_ref, changes)` | `PATCH /api/dispatch/{id}` | **New `task.update {changes}`** → `task.ack` or `task.nack {code: too_late \| invalid_input \| unsupported}` (§10.8). Agents that don't list `update` in their handshake `task_ops` get cancel-and-recreate, with a read-back, because the first attempt may already have had side effects |
| **Cancel** | `manage_task(action="cancel", task_ref)` | `POST …/{id}/cancel` | `task.cancel {reason: user_cancelled}` |
| **Complete** (user says it's done) | `manage_task(action="complete", task_ref)` | `POST …/{id}/complete` | **New `task.close {reason: completed_by_user}`.** For watch-style tasks ("tell me when it drops below $200") the user ends the task |
| **Delete** | `manage_task(action="delete", task_ref)` | `DELETE /api/dispatch/{id}` | Cancel first if active, mark it `cancelled`, then hard-delete the row once `agent_informed_at` is set (as Kairos deletes do today) |
| **Answer** | `manage_task(action="answer", task_ref?, answer)` | `POST …/{id}/input` | `task.input` |

### 10.6 Telling the agent and telling the user

"Task completed" has two audiences, and each needs its own delivery
guarantee.

**Telling the agent.** The agent that owns a task must always learn how it
ended, whoever ended it. The orchestrator sends `task.close {reason, by}`
when the user completes, cancels or deletes a task, or when the agent has lost it
(heartbeat reconciliation, §10.8.4), and `task.delivered {via, at}` once the user has heard the result.
Both are requests the agent acks, defined in §10.8.

Connections are short-lived or pooled, so the agent may be unreachable when
a request or notice is due. **Every** request to an agent goes through the
durable outbox (§10.8.11), not only these notices. A notice leaves the outbox
only when the agent acks it, and `agent_informed_at` is set from that ack.
Agents must treat repeated notices as idempotent.

**Telling the user: the `notify` flag.**

| `notify` | Meaning | When the result arrives |
|---|---|---|
| `device` | Tell me as soon as it's done, even if I'm not in a call | Session open → speak it (held while bridged). Otherwise → insert a `jobs` row (`kind = 'task_result'`, payload `{task_id}`) → the worker publishes `start_websocket` to the ESP32 → the pin calls in → the orchestrator announces it |
| `next_session` | Tell me next time I talk to you | Session open → speak it. Otherwise → wait. Announce at the next connect |
| `silent` | Don't tell me. I'll ask | Record only. Available through `manage_task(status)`. The agent still gets `task.close` |

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
- **Worker change.** `listener/worker.py` learns two kinds:
  - `task_result` publishes the same `start_websocket` wake, and drops the
    job if `tasks.delivered_at` is already set. That mirrors today's "drop a
    task job whose task is no longer pending" safety net.
  - `agent_task_deadline` doesn't wake the device. It POSTs the task context
    to the orchestrator (§10.8.10).

  Both are handled **before** the generic path, which today wakes the device
  for any job kind other than `text_message`.
- **Failed tasks, and tasks lost by a dead agent,** are announced with the same flag. "Tabletop
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

### 10.8 Protocol changes: Protocol 2 as request and ack messages

The task mode built on the branch (Appendix A) ties a task to an open socket.
The orchestrator holds a pooled WebSocket and waits on it for frames, and a
disconnect after `accepted` fails the task. That doesn't fit M5. The user asks
about a task minutes or hours later, edits it, or cancels it, long after the
socket that dispatched it has been reaped.

So Protocol 2 becomes a **message protocol keyed by IDs, not by
connections**. The orchestrator sends a request (a JSON payload), the agent
**acks or nacks** it, and every later operation refers to the same
`task_id`. The connection is just a transport, and either side may close it
once the ack is in.

#### 10.8.1 Identifiers

| ID | Minted by | Scope | Purpose |
|---|---|---|---|
| `task_id` | **Orchestrator** (UUID) | One task, for its whole life | The handle for status, update, cancel, input and close. Minting it on our side makes dispatch **idempotent**: resending the same `task_id` can never create a second task, so a dispatch whose ack was lost can be retried safely. It also lets us reference the task before any ack arrives |
| `agent_task_ref` | Agent (optional) | Agent-internal | Returned in the ack and echoed back on every later request, for agents with their own job IDs |
| `context_id` | Orchestrator | One user conversation thread with that agent | Groups related tasks ("book dinner", then "make it 8", then "and a taxi there") so the agent can keep conversational memory across follow-ups. The same `context_id` is sent on a later bridge to that agent, so live and background work share context. This matches A2A's `contextId` |
| `msg_id` / `reply_to` | Sender / responder | One request and its response | Correlates an ack or nack with the request that caused it, when several requests about one task are in flight |
| `seq` | Agent | Per task, increasing | Orders agent events. The orchestrator drops duplicates and stale events |

**Where they live, with no new schema.** `task_id` is `tasks.task_id`.
`agent_task_ref`, `context_id`, the last acked `seq` and the callback token's
hash go in `tasks.task_info` (`{"intent", "slots", "agent_task_ref",
"context_id", "last_seq", "callback_token_sha256"}`). Wire statuses map onto
`tasks.status` as in §10.5: `accepted` → `running`, `succeeded` →
`completed`, and the others by name.

#### 10.8.2 Envelope

Every task message, in either direction, over either transport:

```json
{
  "type": "task.dispatch",
  "msg_id": "m-7f3a",
  "task_id": "6a1e…",
  "context_id": "c-2b90…",
  "agent_task_ref": null,
  "sent_at": "2026-10-05T19:02:11Z",
  "body": { }
}
```

Responses carry `"reply_to": "<msg_id>"`. Unknown `type`s are answered with
`task.nack {code: "unsupported"}`. Unknown fields are ignored, as in v1.

#### 10.8.3 Requests (orchestrator → agent) and their replies

Every request gets exactly one reply, `task.ack` or `task.nack`, within the
agent's declared **`max_reply_latency_s`** (default **5 s**, §10.8.5). The
reply goes back over the same **transport** the request arrived on, which
need not be the same connection. An ack means "received, valid and recorded". For `task.dispatch` it
also means "I accept responsibility". **An agent must persist the task before
acking a dispatch**, so its events still get sent, and later requests still find it, after an agent
restart.

| Request | `body` | `task.ack` body | Typical `task.nack` codes |
|---|---|---|---|
| `task.dispatch` | `user_id`, `intent`, `slots{}`, `input{}`, `deadline_at?` (advisory, §10.8.10), `notify_hint_allowed`, `callback{url, token}` | `status` (`accepted` or `running`), `eta_s?`, `agent_task_ref?` | `busy` (retryable), `unsupported_intent`, **`missing_input {fields[]}`**, `invalid_input {field, why}`, `cannot_meet_deadline`, `unauthorized` |
| `task.status` (optional, never scheduled) | none | `status`, `progress{message, pct}?`, `question?`, `result?`, `last_seq` | `unknown_task` |
| `task.update` | `changes{}` | `status`, `applied{}` | `too_late` (already done), `invalid_input`, `unsupported` |
| `task.cancel` | `reason`: `user_cancelled`, `deadline`, `escalated_to_bridge` or `accept_timeout` | `status`: `cancelled`, `cancelling`, or `already_finished` (with `result`) | `unknown_task` |
| `task.input` | `answer`, `question_seq` | `status` | `no_question_pending`, `unknown_task` |
| `task.close` | `reason`: `completed_by_user`, `deleted` or `agent_lost`; `by`: `user` or `orchestrator` | none | `unknown_task` (treated as done) |
| `task.delivered` | `via`: `live`, `device_wake` or `next_session`; `at` | none | none |

Re-sending a `task.dispatch` with a `task_id` the agent already has returns
the **same ack** with the current status. It must not start a second task.
That is the agent's half of idempotency.

**`missing_input` is a second When2Call gate.** The agent knows its own
required fields better than our registry does. A `missing_input {fields:
["party_size"]}` nack turns into an **Ask** ("For how many people?"), and the
orchestrator re-dispatches with the **same** `task_id` once the user answers.
It is never treated as a failure.

#### 10.8.4 Events (agent → orchestrator)

The agent reports state changes as `task.event`, whether or not the
orchestrator asked:

```json
{ "type": "task.event", "task_id": "6a1e…", "seq": 4,
  "body": { "status": "input_required", "question": "Which restaurant?" } }
```

`status` is one of `running`, `input_required`, `succeeded`, `failed` or
`cancelled`. Terminal events carry `result{say, output}` or `error`. An agent
may add `notify_hint: "urgent"`, which the orchestrator clamps to the user's
`notify` setting (§10.6).

**Push only: the agent pings the orchestrator. The orchestrator never
polls.**

1. **An HTTP callback, required for task mode.** The agent posts each event to
   `POST <MAIN_BASE>/developer/tasks/{task_id}/events` with
   `Authorization: Bearer <callback.token>`. Every agent can already make
   outbound HTTP calls to the orchestrator, because it calls
   `/developer/register` and `/developer/ping`. (The earlier draft rejected
   callbacks on the grounds that agents couldn't reach us, which was wrong.)
   The token is random, issued per task and stored hashed, so an agent can only
   post events for its own tasks.
2. **The open WebSocket, as an optimisation.** If the orchestrator happens to
   be connected, the agent may send the event there instead.

The orchestrator acknowledges each event: HTTP `200 {ack_seq}`, or
`task.event_ack {seq}` on the socket. **The agent re-sends un-acked events**
with backoff until they are acked. A result therefore survives a dropped
connection, an orchestrator restart and an agent restart (because the agent
persisted the task before acking it). `seq` makes the re-sends harmless.

**Why polling was there, and what replaces it.** Polling covered three cases.
Push covers all three:

| Case polling covered | Push-only answer |
|---|---|
| Agents that can't call back | Not supported in task mode. Every agent already makes outbound HTTP calls, and adapter agents (e.g. for third-party products) do the pushing themselves |
| A lost event | The agent re-sends until acked, and `seq` dedupes |
| "Is it done?" asked by the user | Answered from the last event. The agent pushes every state change, including `running` progress, so the last event *is* the current state |

**The one case push alone can't detect is an agent that dies and never comes
back.** Its tasks would sit in `running` forever. For tasks without a deadline, that is
detected from the **registration heartbeat** agents already send every 5 min
(`/developer/register`, which updates `last_seen`), not by polling:

- **Stale agent.** If `last_seen` is older than 15 min (three missed
  heartbeats) and the agent has open tasks, the orchestrator sets
  `task_info.stalled_since` on them. The status stays `running`, so no new
  state is needed. The user hears "Tabletop hasn't checked in for a while, so
  your booking may be stuck" when they ask, or proactively per their
  `notify` setting (once per stall). When heartbeats resume, the flag clears
  and the agent's re-sent events catch the task up.
- **Reconciliation (SHOULD).** The heartbeat body may include
  `open_task_ids[]`. A task the orchestrator thinks is open but the agent
  doesn't list has been lost by the agent. It is marked `failed` with error
  "Tabletop lost track of this task", and the user is told. This is the push
  equivalent of a `task.status` returning `unknown_task`.

`task.status` stays in the protocol as an **optional** request. The
orchestrator never schedules it, and uses it only when the user asks about a
task flagged `stalled_since`, as a single attempt to reach the agent.

#### 10.8.5 Registration and handshake additions

**Capabilities are declared at registration.** The registration record is the
authoritative, transport-independent statement of what an agent supports,
because the orchestrator must know it before it can reach the agent, and a
future pull agent (§10.10) never sees a handshake. `POST /developer/register`
(and every heartbeat) accepts:

| Field | v2 values | Default | Notes |
|---|---|---|---|
| `binding` | `ws` only. `pull` and `a2a` are reserved for §10.10 | `ws` | How the orchestrator reaches the agent |
| `public_url` | WebSocket URL | none | **Required only when `binding = ws`** |
| `modes` | `bridge`, `task` | `["bridge"]` | |
| `task_ops` | Subset of `dispatch, status, update, cancel, input, close, delivered` | `["dispatch", "cancel", "input"]` | |
| `events` | `callback` (required for task mode), `ws` | `[]` | |
| `max_concurrency` | 1–1000 | 8 | |
| `max_reply_latency_s` | 1–30 for `ws` (longer values are reserved for pull) | 5 | Replaces the hard-coded 5 s (§10.8.3) |
| `default_deadline_s` | Seconds, or null | null | §10.8.10 |
| `side_effects` | bool | false | Read-back before acting (§11.3) |
| `domains`, `intent_aliases`, `routing_policy`, `user_data` | | | Indirect routing (§10.2) |
| `open_task_ids` | List | | Heartbeat reconciliation (§10.8.4) |

**The handshake ack may only narrow these** for that connection (a lower
`max_concurrency` under load, a smaller `task_ops`). It never adds a
capability that registration didn't declare. `hello {mode: "task", version:
"2"}` is unchanged. A v2 ack looks like this:

```json
{ "type": "ack", "accept": true, "version": "2", "modes": ["bridge", "task"],
  "max_concurrency": 8,
  "task_ops": ["dispatch", "status", "update", "cancel", "input", "close"],
  "events": ["callback", "ws"] }
```

| If the agent lacks | The orchestrator |
|---|---|
| `update` | Cancels and recreates, after a read-back to the user |
| `status` | Nothing changes. "Is it done?" is always answered from the last event |
| `callback` in `events` | Refuses task mode for that agent (it is treated as bridge-only) |
| `close` / `delivered` | Skips those notices. They are informational |

**Bridge mode (Protocol 1) changes too.** The bridge `hello` gains
`context_id`, plus `task_id` when the bridge was escalated from a task (§10.3),
so the agent can pick up where the task left off. During a bridge the agent
may send **`task.created {task_id?, intent, slots, notify_hint?}`** to hand off
ongoing work. The orchestrator replies with a `task.ack` carrying the
orchestrator-minted `task_id`, and from then on tracks it like any dispatched
task. The agent's bridge `say` text is also recorded in the LLM history,
reversing v1, so a follow-up such as "change that booking" can be resolved.

#### 10.8.6 Sequences

**Dispatch, disconnect, result by callback, then status and update later:**

```
Orchestrator                                   Agent (Tabletop)
  │ WS connect + hello(task) ─────────────────────►│
  │◄──────────────────────── ack(task_ops, events) │
  │ task.dispatch {task_id=T, context_id=C,        │
  │   slots{restaurant:"Nopa", time:"19:00",       │
  │   party_size:2}, callback{url, token}} ───────►│  persists T
  │◄──────────────────── task.ack {status:accepted, │
  │                        agent_task_ref:"bk-551"} │
  │ (socket idle → closed by the pool)             │
  │                                                │  …works…
  │◄──── POST /developer/tasks/T/events            │
  │      {seq:1, status:succeeded,                 │
  │       result{say:"Booked Nopa, 7pm, 2 people."}}│
  │ 200 {ack_seq:1} ──────────────────────────────►│
  │ (speak, or wake ESP32 per notify)              │
  │                                                │
  │ … an hour later: "move it to eight" …          │
  │ WS connect + hello ───────────────────────────►│
  │ task.update {task_id=T, changes{time:"20:00"}}►│
  │◄────────── task.ack {applied{time:"20:00"}}     │  or task.nack {too_late}
  │ task.delivered {task_id=T, via:live} ─────────►│  (outbox drained on the same connection)
  │◄──────────────────────────────────── task.ack  │
```

**A lost ack, then a safe retry:**

```
  │ task.dispatch T ──────────────────────────────►│  persists T, ack lost (socket dropped)
  │ (5 s, no ack) reconnect                        │
  │ task.dispatch T (same task_id) ───────────────►│  already has T → same ack, no new work
  │◄──────────────────── task.ack {status:running}  │
```

#### 10.8.7 What this changes in the built dispatcher (Appendix A)

| Built behaviour | New behaviour |
|---|---|
| A task lives on a pooled connection, and frames are routed to a per-task queue on that connection | A task lives in the `tasks` table (§10.5). Frames from any connection or the callback endpoint are routed by `task_id` |
| Disconnect after accept → the task fails, non-retryable | Disconnect after ack → **nothing happens**. Wait for the agent to push the next event |
| Accept timeout → cancel, then fail over | No ack within the agent's `max_reply_latency_s` → **resend the same outbox entry** (same `msg_id` and `task_id`, so idempotent), up to 2 times. Then cancel and fail over (intent routes only), as before |
| No retry after acceptance | Unchanged in spirit: never re-dispatch to **another** agent after an ack. Re-sending to the **same** agent is now safe |
| `task.accepted`, `task.rejected`, `task.progress`, `task.input_required`, `task.result` frames | Become `task.ack`, `task.nack` and `task.event {status}`. v2 hasn't been merged and no third-party agent speaks it, so renaming costs nothing now |
| `idempotency_key` = task_id | Folded into `task_id`. Idempotency is now a protocol rule, not an optional hint |
| Default 120 s deadline (max 1 h), extended while waiting on the user; `TIMED_OUT` state; scheduled `task.status` polling | **Deadline optional, no polling.** A deadline, if any, is a `jobs` row scheduled on ack, which pings the orchestrator with the task context (§10.8.10). The agent pushes every state change. Dead agents are caught by heartbeat staleness and `open_task_ids` reconciliation (§10.8.4) |

#### 10.8.8 Alignment with A2A

These changes move Protocol 2 most of the way to Google's Agent2Agent
protocol, which is a good reason to align names now (decision B3). The
mapping is approximate *(verify against the current A2A spec)*:

| Ours | A2A |
|---|---|
| `task.dispatch` / ack | `message/send` returning a `Task` |
| `task.status` | `tasks/get` |
| `task.cancel` | `tasks/cancel` |
| HTTP callback events | Push notifications (`tasks/pushNotificationConfig/set`) |
| `context_id` | `contextId` |
| `input_required` status | `input-required` task state |

The transport differs. A2A is JSON-RPC over HTTP with SSE, while we use a
WebSocket because agents expose one WS URL behind cloudflared. The envelope in
§10.8.2 is transport-neutral, so an HTTP binding (`POST <agent>/tasks`) could
be added later without changing message shapes.

#### 10.8.9 Spec home

**Drafted** as the "Task mode — DRAFT (v2)" chapter of
`app/developer_ws/BRIDGE_PROTOCOL.md`, plus the v2 hello/ack, bridge hand-off and close-code additions, all marked DRAFT until main implements them, with the minimum-compliance
checklist extended to: persist before ack, idempotent dispatch, reply within
`max_reply_latency_s`, re-send un-acked events. `BUILD_SERVICE_PROMPT.md` and the echo agent
(`testing/echo_server.py`) are **not** updated yet. They change when main implements v2, so generated services don't target an unimplemented spec.

#### 10.8.10 Optional deadlines

**A deadline is optional.** Most tasks don't have one, and they run until the
agent reports a terminal event or the user ends them (§10.8.4). When a task
does have one, the orchestrator must act **at that time, with the task's
context**, even if the agent has gone silent. That is done by **scheduling**
the deadline on the existing `jobs` table and worker. Nobody polls the agent.

**Where a deadline comes from.** It is a slot like any other, so it must be
grounded (§11.3). The model never invents one.

| Source | Example | `deadline_at` |
|---|---|---|
| The user states it | "Book it, but forget it if it's not done by six", "I need this in an hour" | The stated time, normalised to UTC |
| The agent's registration declares a default | A ticket-hold agent registers `default_deadline_s: 900` | Dispatch time + default |
| Neither | Almost everything | `NULL`: no deadline, nothing scheduled |

**What happens when it passes** (`task_info.on_deadline`):

| `on_deadline` | Used when | At the deadline |
|---|---|---|
| `cancel` | "Forget it if it's not done by six": the result is worthless after the time | Send `task.cancel {reason: "deadline"}`, set status `timed_out`, tell the user |
| `notify` (**default**) | "I need this in an hour", and any deadline where the user didn't say to drop the task: the user wants to know it's late, but the work should carry on | Leave the task `running`, set `task_info.overdue_since`, tell the user and offer to cancel |

**Storage, with no new schema.** It uses the existing `tasks.deadline_at`
column and the `timed_out` status, which are now used rather than dropped.
`task_info` gains `on_deadline` and `deadline_job_id`. `enqueue_sequence_id`
is left for device-wake jobs, so the two can't overwrite each other.

**Scheduling: only after the agent acks.**

```
on task.ack for a task.dispatch, where rec.deadline_at is set:      # same transaction as status → running
    job_id = insert_job(
        kind       = "agent_task_deadline",
        deliver_at = max(rec.deadline_at, now()),                    # an ack after the deadline fires at once
        payload    = { task_id, user_id, agent_id, agent_name, context_id,
                       intent, slots, notify, on_deadline, deadline_at })
    rec.task_info.deadline_job_id = job_id
```

- **Why after the ack.** A nacked or never-acked task never started, so
  there is nothing to time out. Scheduling on the ack also means the
  same-`task_id` dispatch retries (§10.8.3) can't create duplicate jobs.
- **The payload carries the dispatched task's context**: who, which agent,
  what was asked and how to tell the user. The handler can then act and
  phrase the announcement without reassembling it. The `tasks` row stays the
  source of truth, and the payload is a snapshot.

**Keeping the job in sync** (reusing `insert_job` / `cancel_job` from
`app/enqueue/task_enqueue.py`, as Kairos edits do):

| Event | Job |
|---|---|
| Terminal event from the agent (`succeeded`, `failed`, `cancelled`) | `cancel_job(deadline_job_id)` |
| User cancels, completes or deletes the task | `cancel_job` |
| `task.update` that changes `deadline_at`, once the agent acks it | `cancel_job`, then `insert_job` at the new time |
| `task.update` that removes the deadline | `cancel_job` |

**Firing: worker → orchestrator, not worker → device.**

```
worker claims a due job with kind = "agent_task_deadline":
    # handled BEFORE the generic path, which today wakes the device for any non-text job
    # never deferred for an active session: the orchestrator decides how to tell the user
    POST http://app:8000/internal/tasks/{task_id}/deadline
         Authorization: Bearer $INTERNAL_API_TOKEN          # app-backend network only, not exposed via Caddy
         body = job.payload
    2xx       → job done
    non-2xx / connection error → retry, as for broker transport errors (no attempt spent)

orchestrator on_deadline(payload):
    rec = load tasks row (payload.task_id)
    if rec is terminal or rec.deadline_at != payload.deadline_at:   # finished, or rescheduled: a stale job
        return 200
    if rec.on_deadline == "cancel":
        send task.cancel {reason: "deadline"}                       # queued in the agent outbox (§10.8.11)
        rec.status = timed_out; rec.result.error = "didn't finish by {time}"
        say = "{agent} didn't finish {intent_summary} by {time}, so I cancelled it."
    else:  # notify
        rec.task_info.overdue_since = now
        say = "{agent} still hasn't finished {intent_summary}. It was due at {time}. Want me to cancel it?"
    deliver say per rec.notify (§10.6): speak if a session is open (held while bridged);
        device → insert a task_result wake job; next_session → wait; silent → record only
```

**Races and edge cases**

- **The agent finishes just after the deadline.** The cancel ack comes back
  `already_finished` with a `result`, or a `succeeded` event arrives after
  `timed_out`. The result is recorded and the status becomes `completed`,
  because a side effect that happened matters more than the timeout. The user
  is told: "Tabletop finished after all: booked Nopa for 7." If the user
  already heard the timeout, this is a second announcement under the same
  `notify` rules.
- **Waiting on the user (`input_required`).** The deadline isn't paused. If the
  user never answers, the deadline handles it. The relayed question mentions
  the due time when one is set ("…it's due by six").
- **A stalled agent** (§10.8.4) doesn't affect the deadline. The job still
  fires, and the cancel waits in the outbox.
- **The orchestrator is down at the deadline.** The worker retries the POST
  until the app is back, so the deadline is handled late but never lost.
- **Agent-side use.** `deadline_at` is also sent in `task.dispatch` as
  **advisory**, so the agent can plan, or nack with `cannot_meet_deadline` if
  it knows it can't make it. Enforcement is always the orchestrator's job.

#### 10.8.11 The agent outbox

Every request the orchestrator sends an agent (`task.dispatch`, `update`,
`cancel`, `input`, `close`, `delivered`, `status`) is **first written to a
durable outbox**, in the same transaction as the `tasks` change that caused
it, and only then sent. That gives one delivery path for every request, so
nothing depends on a connection being open at the moment of the request. The
same table becomes a pull agent's inbox later (§10.10).

```
agent_outbox                                        (new table, needs a migration)
  id               bigserial pk
  agent_id         uuid  → agents(agent_id)   not null
  task_id          uuid  → tasks(task_id)     not null    every request is about one task
  msg_id           text  unique               not null    reused on every resend, so the agent can dedupe
  type             text                       not null    task.dispatch | task.update | …
  envelope         jsonb                      not null    the full §10.8.2 message
  created_at       timestamptz                not null
  next_attempt_at  timestamptz                not null    backoff schedule
  attempts         int                        not null default 0
  sent_at          timestamptz                            last send
  acked_at         timestamptz                            set on task.ack or task.nack (reply stored below)
  reply            jsonb
  expired_at       timestamptz                            gave up (dispatch only, see below)
  index: (agent_id, next_attempt_at) where acked_at is null and expired_at is null
```

**Delivery (`binding = ws`)**

```
on new outbox row:            try to send now (pooled connection, or dial)
on any connection to agent:   drain its unacked rows, oldest first, per task in created order
sweep (every 30 s):           rows due by next_attempt_at → connect, send; backoff 5 s → 5 min
on task.ack / task.nack:      match reply_to = msg_id → acked_at, reply; apply the result to tasks
no reply within the agent's max_reply_latency_s → attempts += 1, resend the same row (same msg_id)
```

- **Order per task.** A task's rows are sent in creation order, and a row isn't
  sent until the previous row for that task is acked. So an `update` can never
  overtake its own `dispatch`. Different tasks don't block each other.
- **Dispatch can expire.** A dispatch nobody acked after 3 attempts is marked
  `expired_at`, and the task fails over (intent routes) or the user hears
  "Tabletop isn't reachable" (named agents). A `task.cancel {reason:
  "accept_timeout"}` is queued behind it in case the dispatch arrived but the
  ack was lost. Before an ack nothing has started, so giving up is safe.
- **Everything else stays queued until acked**, for up to 24 h (`cancel`,
  `close`, `update`, `input`, `delivered`). Those matter after the task has
  started. If an `update` or `input` is still unacked after its first window,
  the user hears "I couldn't reach Tabletop yet. I'll keep trying." After
  24 h the row expires and the task is flagged for the user.
- **This isn't polling.** The sweep retries delivering the orchestrator's *own*
  pending messages. It never asks an agent for state.
- **Pull binding later (§10.10).** The same rows are served by `GET
  /agent-link/v1/inbox` instead of being pushed. Only the delivery loop
  differs.

### 10.9 Decisions for you

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
- **M-g.** Rework Protocol 2 into request/ack messages keyed by
  `task_id` (§10.8), so a task outlives its connection? This renames the
  built frames to `task.ack`, `task.nack` and `task.event`, which is free
  while v2 is unmerged.
- **M-h.** Who mints `task_id`: the orchestrator (proposed, which makes
  dispatch idempotent), or the agent in its ack?
- **M-i.** Agent → orchestrator results: **push only** (decided). The HTTP
  callback with a per-task token is required, the WebSocket is optional, and
  there is no polling. Dead agents are detected from heartbeat staleness and
  `open_task_ids` reconciliation (§10.8.4).
- **M-l.** Deadlines are **optional** (decided). Without one, a task runs
  until the agent reports a terminal event or the user ends it. With one,
  it is scheduled as an `agent_task_deadline` job once the agent acks, and
  the worker pings the orchestrator on time with the task's context
  (§10.8.10). `on_deadline` defaults to `notify`, and is `cancel` only
  when the user says to drop the task. Agents may declare a
  `default_deadline_s` at registration (decided).
- **M-j.** Require agents to persist tasks before acking a dispatch (a MUST in
  the spec), or allow "best effort" agents that lose tasks on restart?
- **M-k.** Align names with A2A now (this replaces B3)?
- **M-m.** Add the `agent_outbox` table (§10.8.11) and the registration
  fields `binding` and `max_reply_latency_s` (§10.8.5) in v2? They are
  needed for reliable delivery anyway, and they make the hosted-agent goal
  (§10.10) cheap. Proposed: yes.

### 10.10 Future goal: hosted personal agents (Muse, Dots and similar)

**Goal.** Let the orchestrator dispatch tasks to the user's own hosted
personal agent (Meta **Muse**, OpenAI **Dots**, and products like them) as if
it were any other registered agent. A user could then say "have my Dot keep an
eye on flight prices" from the pin. This is **not in scope for v2**. It is
recorded here so v2 doesn't close the door on it.

**Why it doesn't fit the protocol today** (as of October 2026, from
launch-week coverage; verify before building):

| Assumption in §10.8 | Muse / Dots reality |
|---|---|
| The agent hosts a WebSocket the orchestrator dials | They run in the vendor's cloud. There is no public server to dial |
| The orchestrator can call the agent | **There is no API that addresses a user's instance.** Muse Spark and Muse Code are separate model and coding-agent APIs. OpenAI's Agents API builds *your own* agents, not the user's Dot. Meta lists a "Muse API" for businesses, but no spec is published |
| The agent registers with `/developer/register` and heartbeats | It can't run our code. It can only use connectors |
| One orchestrator credential (`user_id="orchestrator"`) | Each instance belongs to one user and acts on their account, so per-user linking is needed |
| The agent answers within 5 s | **Muse:** nothing documented about it acting without the user, so it acts when the user next talks to it. **Dots:** always-on and pursue standing goals, so latency is minutes |
| Bridge mode (live audio) | Neither offers realtime voice to third parties. **Task mode only** |

**What they *can* do: call out through connectors.** Muse builds a custom
connector when prompted with an MCP server URL or API docs, and saves it as a
reusable skill (bearer-token auth fits its credential prompt). Dots use
ChatGPT connectors, and custom MCP connectors are available in ChatGPT
Developer Mode (Plus and above). Whether a Dot can use one is unverified. So
the instance must **start every exchange**.

**Planned approach**

1. **A pull binding ("agent-link").** A third binding beside the WebSocket and
   any A2A client. Requests for a pull agent go into a durable per-agent
   **inbox** (the §10.6 outbox, generalised) and are never dialed. The agent
   uses our API to:
   - fetch requests: `GET /agent-link/v1/inbox`;
   - ack or nack them: `POST /agent-link/v1/replies`, with `reply_to`;
   - report events: `POST /agent-link/v1/events`;
   - hand off work: `POST /agent-link/v1/tasks`.

   The envelope, `task_id`, ack/nack and events (§10.8.2–10.8.4) are unchanged,
   so only the transport differs. Every fetch counts as a heartbeat. The
   orchestrator still never polls: the agent fetches from us.
2. **Two faces, one implementation:** REST with an OpenAPI spec (for "read the
   API docs" connectors) and an MCP server (`check_inbox`, `reply`,
   `report_event`, `hand_off_task`).
3. **Per-(user, agent) link tokens**, generated in our app, limited to that
   user's tasks for that agent and revocable. The registry gains
   `binding: "pull"`, `max_reply_latency_s` and `requires_user_link`.
4. **Setup is a prompt, not code.** An `AGENT_LINK_PROMPT.md` beside
   `BUILD_SERVICE_PROMPT.md`. For Muse: "create a custom connector from
   `<MAIN_BASE>/agent-link/v1/openapi.json`, check its inbox whenever we
   talk…". For a Dot: add the connector, plus a standing goal ("check the
   Orchestrator inbox every 10 min").
5. **Triggering the instance** is the real limitation:
   - **Dots:** the standing goal works on its own. For lower latency, add a
     **Slack/Teams doorbell**: a bot the user installs posts "new task
     waiting" in the Dot's channel, and the Dot fetches the task through the
     connector. No task data goes through Slack.
   - **Muse:** no trigger found. The orchestrator says so when dispatching
     ("Queued for Muse. It'll pick it up next time you open Muse"). Deadlines
     defaulting to `notify` (§10.8.10) make that degrade gracefully.
6. **Routing and expectations.** Pull agents rank below agents that respond
   immediately for urgent intents. M3 never picks *connect* for them, and the
   dispatch ack mentions the expected latency.

**Risks**

- **Privacy and consent.** The user's task text goes to Meta or OpenAI.
  Linking requires explicit consent, shown when the user links.
- **Untrusted connectors.** Meta doesn't review custom connectors. Keep link
  tokens narrowly scoped, and treat everything the instance sends as untrusted
  agent output (§11.3), as for any agent.
- **Vendor terms and fragility.** The connector features are weeks old and
  may change. The doorbell acts in the user's workspace and needs the user to
  install it.

**What v2 should do now so this stays cheap later**

All four are now part of the v2 design:

| Item | Where |
|---|---|
| Keep the envelope **transport-neutral**. Capabilities are declared at registration, and replies go over the same transport, not the same connection | §10.8.2, §10.8.3, §10.8.5 |
| A **durable outbox** keyed by `task_id`, for every request. It becomes the pull inbox | §10.8.11 |
| A `binding` field in the registry (`ws` only in v2; `pull` and `a2a` reserved), with `public_url` required only for `ws` | §10.8.5 |
| A per-agent reply timeout, `max_reply_latency_s` (default 5) | §10.8.3, §10.8.5 |

For pull agents, the remaining work is new endpoints and a new delivery loop,
not a protocol or schema redesign.

**Revisit when** Meta publishes the Muse API or Business Agent spec, OpenAI
exposes Dots through the Agents API or another inbound API, either product
adopts A2A, or Muse gains scheduled or background runs. Any of these would
replace the pull binding or the doorbell with a direct call.

## 11. The optimisation space: a layered defence

The paper's lesson is that we can't trust the model's own judgement on
whether to call. So we shrink the problem, steer the model, and then **check
its output deterministically** before acting. The layers are ordered by cost.
L0–L3 apply to every voice architecture option (voice doc) that keeps tool calls in text, which is
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
| **No repeat after accept** | Duplicate side effects | The dispatcher doesn't re-dispatch after `task.ack` and passes `idempotency_key` (Appendix A) |

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

This is the yardstick for both decisions (voice architecture and tool calls). It extends When2Call in three
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
   voice architecture option.
3. Add L3 only where §12 shows residual failures, most likely intent-routed
   dispatch.
4. Treat L5 as conditional on choosing option E.
5. Build the §10 modes in this order: `route_to_agent` with M1/M2/M3 (no
   schema change beyond the registry fields), then agent tasks in `tasks` with
   `notify`, then `task.close`/`task.delivered`, then the ESP32 `task_result`
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
| **Protocol v2** | `app/agent_protocol.py`, `BRIDGE_PROTOCOL.md` | One endpoint, `mode: bridge or task` in the hello. Task frames: `dispatch`, `accepted`, `rejected`, `progress`, `input_required`, `input`, `result`, `cancel`. Backward compatible with v1 agents. New close code 4405. **§10.8 reworks task mode** into request/ack messages keyed by `task_id`, so tasks outlive connections, with update and close requests and agent-pushed events (HTTP callback, no polling) and optional scheduled deadlines |
| **Task dispatcher** | `app/task_dispatcher.py` | State machine (queued → dispatching → accepted → running ⇄ input-required → terminal). §10.8 makes its deadline optional and moves it to a scheduled job (§10.8.10). **A named agent is never substituted**, while intent-routed tasks fail over among at most 3 agents. **No retry after accept**, plus an `idempotency_key`. Pooled WS per agent, bounded limits, results held while bridged and announced at the next session if the user is offline. Tasks are in memory today; §10.5 moves them to the shared `tasks` table and adds `notify` and agent closing notices |
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

## 16. Decision checklist (tool calls, modes, protocol)

| # | Decision | Proposed |
|---|---|---|
| **2a** | Validation gate as primary defence | Yes |
| **2b** | Read-back for side-effecting agents | Yes, per agent `side_effects` flag |
| **2c** | Block ungrounded slots | Yes, lenient thresholds, tuned on logs |
| **2d** | Over-caution ceiling | 5% ask rate on complete requests |
| **2e** | Real-speech eval clips | Open |
| A1–A3 | Lexical and phonetic routing; thresholds 0.55 / 0.12 / 0.92; decline unknown names | Yes |
| B1–B4 | One endpoint with `mode`; `user_id="orchestrator"` in task hellos; A2A-style state names; silent progress | Yes / Yes / See M-k / Yes |
| **M-a** | One `route_to_agent` tool plus a code-side connect/dispatch policy | Yes |
| **M-b** | Indirect routes: announce and connect with a barge-in window | Yes |
| **M-c** | Registry fields `domains`, `intent_aliases`, `routing_policy`, `user_data` | Yes, backfilled for Kairos and MyFitnessPal |
| **M-d** | Durable agent tasks | **Done:** in `tasks`, `agent_tasks` dropped (flips C1) |
| **M-e** | Default `notify` | `next_session` |
| **M-f** | Wake limits | Quiet hours, at most 4 per hour per user |
| **M-g** | Protocol 2 as request/ack keyed by `task_id`; tasks outlive connections | Yes (§10.8) |
| **M-h** | `task_id` minted by the orchestrator | Yes. The agent may return `agent_task_ref` |
| **M-i** | Result delivery | **Decided:** push only, callback required, no polling, heartbeat liveness |
| **M-m** | `agent_outbox` table, plus `binding` and `max_reply_latency_s` at registration | Yes (§10.8.5, §10.8.11) |
| **FG-1** | Hosted personal agents (Muse, Dots) | **Future goal** (§10.10). Pull binding, link tokens, Dots doorbell. Not in v2 |
| **M-l** | Task deadlines | **Decided:** optional. Scheduled via `jobs` after the ack (§10.8.10). Default `on_deadline = notify`. Agents may register `default_deadline_s` |
| **M-j** | Persist before ack | MUST |
| **M-k** | Align names with A2A now | Yes (replaces B3) |
| C1–C4 | ~~In-memory tasks~~ **Postgres `tasks`** (M-d); 8 per user, optional deadline (M-l); never substitute a named agent; no retry after accept | Changed / Yes / Yes / Yes |
| D1–D3 | Deterministic spoken results; hold while bridged; merge tools | Yes / Yes / **Merge into `route_to_agent` and `manage_task`** (changed, §10.7) |
| E1–E2 | Dispatch token required; `/api/agents` sorted by name | **Require it (fail closed)** / Yes |

## 17. Sources

- Ross, Mahabaleshwarkar, Suhara. *When2Call: When (not) to Call Tools.*
  NAACL 2025. [arXiv 2504.18851](https://arxiv.org/abs/2504.18851) ·
  [HTML](https://arxiv.org/html/2504.18851v1) ·
  [code and data](https://github.com/NVIDIA/When2Call)
- *Full-Duplex-Bench-v3.* [arXiv 2604.04847](https://arxiv.org/abs/2604.04847)
- Repo: `app/developer_ws/DESIGN.md`, `app/developer_ws/pipecat_llm.py`,
  `OCI_INFRASTRUCTURE.md`
- Hosted personal agents (§10.10), launch-week coverage, October 2026:
  [TechCrunch: OpenAI Dots](https://techcrunch.com/2026/09/29/openai-launches-dots-its-bubbly-agentic-avatar/) ·
  [9to5Google: Dots](https://9to5google.com/2026/09/29/openai-dots-agent/) ·
  [InfoQ: DevDay 2026](https://www.infoq.com/news/2026/10/openai-devday-2026/) ·
  [TechCrunch: Meta Muse](https://techcrunch.com/2026/09/08/meta-debuts-its-muse-ai-agent-will-consumers-trust-it/) ·
  [TechCrunch: Muse for businesses](https://techcrunch.com/2026/09/29/meta-is-expanding-its-ai-agent-muse-to-small-businesses/) ·
  [Sprites: Muse connectors](https://www.sprites.ai/muse/connectors) ·
  [AI Agents Library: Muse and MCP](https://www.aiagentslibrary.com/blog/meta-muse-mcp/) ·
  [Auth0: MCP servers in ChatGPT](https://auth0.com/blog/add-remote-mcp-server-chatgpt/)
