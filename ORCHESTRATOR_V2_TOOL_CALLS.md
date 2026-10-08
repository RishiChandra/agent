# Orchestrator v2: routing, dispatch and reliable tool calls

**Status:** all decisions answered 2026-10-06; implemented 2026-10-07 on
branch `orchestrator-v2` (`app/orchestrator/`: see its README for the layout;
`listener/worker.py`), with tests in `test/app/orchestrator/` (every row of the
§1.2 matrix has a test). Kairos takes background tasks since 2026-10-08
(`app/kairos_tasks.py`, §1.7). The wire-level agent protocol is in
[`app/orchestrator/BRIDGE_PROTOCOL.md`](app/orchestrator/BRIDGE_PROTOCOL.md).
The voice-architecture decision and the latency measurements are in
[ORCHESTRATOR_V2_VOICE.md](ORCHESTRATOR_V2_VOICE.md).

- **Part 1** covers what the orchestrator does with a request: routing to an
  agent, live calls versus background tasks, task management, and how agents
  and the user are told what happened.
- **Part 2** covers how we stop wrong tool calls, based on the When2Call
  paper.
- The **appendices** hold the decision log, configuration and sources.

---

## Part 1: Routing and dispatch

### 1.1 What the orchestrator can do

An *agent* is any registered service (Kairos, MyFitnessPal, Tabletop…). The
orchestrator reaches agents in two ways:

- **Protocol 1, live call (bridge):** the user's mic is relayed to the agent
  and the agent talks back.
- **Protocol 2, background task:** the orchestrator sends the agent a job,
  keeps talking to the user, and reports back.

| Mode | Protocol | The user says | What happens |
|---|---|---|---|
| **M1 Direct route** | 1 | "Connect me to Kairos" | Connect to the named agent |
| **M2 Indirect route** | 1 | "How many calories have I had today?" | Pick the agent that owns the request (MyFitnessPal) and connect |
| **M3 Connect or dispatch** | 1 or 2 | "Have Tabletop book dinner" vs "Let me talk to Tabletop" | Decide between a live call and a background task |
| **M4 Dispatch and monitor** | 2 | "Book Nopa for two at seven and let me know" | Send a task, relay the agent's questions, deliver the result |
| **M5 Task management** | 2 | "Is it done?", "Move it to eight", "Cancel that" | Read, update, cancel, complete or delete tasks |

Gemini sees five tools:

| Tool | Covers |
|---|---|
| `route_to_agent(intent, mode_hint, agent?, slots?, notify?, deadline?, drop_at_deadline?)` | M1–M4 |
| `find_agents(query)` | "Is there an agent for…?" |
| `manage_task(action, task_ref?, changes?, answer?, notify?)` | M5, plus confirming or declining a read-back |
| `end_conversation(reason)` | Unchanged |
| `google_search` | Unchanged (server-side) |

**Why one routing tool:** choosing between two similar tools is exactly
where models err (Part 2). Gemini only reports what the user's words imply;
code makes the decision.

### 1.2 How a request is handled: the decision matrix

Every tool call passes the **validation gate** (§2.4) before anything is
spoken or executed. The matrix below is the complete list of outcomes. Every
spoken line is fixed text (§1.8), not Gemini's phrasing.

| # | Situation | Orchestrator does | The user hears |
|---|---|---|---|
| 1 | General question, no agent needed | Answers itself | Gemini's answer |
| 2 | Names an agent; one confident match, including STT garbles ("cairo's" → Kairos) | Connects or dispatches (§1.4) | "Connecting you to Kairos now." + ding |
| 3 | Names an agent; two close matches (a shared prefix like "weather", or two agents with the same name) | Asks; never picks | "Did you mean Weather Alerts or Weather Bot?" |
| 4 | Names an agent that doesn't exist | Declines; never falls back to a default agent | "I couldn't find an agent called Zorblax." |
| 5 | Named agent is unreachable | Says so; **never substitutes** another agent | "Kairos isn't answering right now." |
| 6 | Agent doesn't support the needed mode | Offers the other mode | "Ledger doesn't take live calls, but I can send it a task." |
| 7 | No agent named; one agent owns the request (a name Gemini filled in that the user never said counts as unnamed) | Live call: announces with a cancel cue, then listens (below). Background task: the dispatch line names the agent and the task can be cancelled | "Getting MyFitnessPal for that. Say no if that's not right." + ding |
| 8 | No agent named; the owning agent is `on_request` and the question is general | Answers itself | Gemini's answer |
| 9 | No agent named; no confident match | Asks or answers | "Want me to check MyFitnessPal?" or Gemini's answer |
| 10 | A required detail is missing | Asks for it | "For what time?" |
| 11 | Gemini filled in a value the user never said | Asks for it | "For what time?" |
| 12 | The user corrected themselves ("seven, no, eight") | Uses the later value, or asks | — / "Seven or eight?" |
| 13 | The agent has side effects (book, buy, send) | Reads back, waits for yes | "Book Nopa for 2 at 7. Shall I go ahead?" |
| 14 | The user already has 8 active tasks | Refuses the new one | "You already have several tasks running. Want me to cancel one first?" |
| 15 | The agent replies that a detail is missing (`missing_input` nack) | Asks, then re-sends the same task | The agent's question, or "For what time?" |
| 16 | A task result arrives during a live call | Holds it until the call ends (kept for the next session if the call outlasts the session) | (announced after) |

**The row 7 cancel window** (decision M-b) is judged by what the user says,
not by sound alone. It opens once the cue has played and the self-echo guard
has released the mic, and lasts 1.5 s. If the user speaks, the orchestrator
waits for the transcript:
- "no" (or "wait", "not that"…) cancels: "Okay. Who should I ask instead?";
- "yes" connects;
- anything else cancels the call and is handled as a normal turn;
- speech with no words (noise) connects.

**Every yes/no question is tracked.** The orchestrator remembers the question
it asked, and Gemini's `manage_task(confirm|decline)` answers it. This
applies to read-backs, "Want me to check X?", other-mode offers, escalation
offers, "Want me to cancel one first?", "Want me to cancel it?" (overdue, too
late to change), "Want me to try without a deadline?", and "Want to hear
them?". Questions expire after 2 minutes.

### 1.3 Routing: resolving what the user said to an agent

The router (`app/orchestrator/routing/router.py`) keeps an in-memory snapshot of the
active agents. It refreshes every 30 s and immediately after any registry
write, and never touches the database while a voice turn waits.

**Signals, strongest first.** Each signal raises a candidate's score to at
least that value; they don't add up.

| Signal | Score | Example |
|---|---|---|
| Exact name or `service_id` | 1.00 | "kairos" |
| Name starts with / contains what was said | 0.90 / 0.85 / 0.70 | "weather" → "Weather Bot" |
| Phonetic match (consonant skeleton) | 0.75 | "cairo's", "kai ross" → Kairos |
| Shares a name word, or a fuzzy name match | 0.50–0.95 | |
| `intent_aliases` hit (curated trigger phrases) | 0.88 | "calories" → MyFitnessPal |
| Lexical intent: words shared with description, keywords, `user_intents`, `domains` | 0.35–0.85 | "book a table" |
| **Embedding similarity** (unnamed requests only) | blended into intent | "I'm starving, how much have I eaten?" → MyFitnessPal |

**Decisions, not guesses.** `resolve()` returns `matched`, `ambiguous` (top two
within 0.12), `none` (best below 0.55) or `wrong_mode`. A score of 0.92 or more
with a clear lead is always `matched`. Thresholds are env-tunable and will be
tuned on logs.

**Embeddings** (decision A1). Each agent's routing text (name, description,
`domains`, `intent_aliases`, `user_intents`, `keywords`, `capabilities`) is embedded once, at registration
or backfill, and cached with the agent. Only unnamed requests are embedded at
route time, using Gemini's embedding API (`gemini-embedding-001`, 768
dimensions) with a 300 ms budget; measured from the VM at 185–240 ms. If the
call fails or is slow, the router uses lexical matching alone, so routing never
blocks on it. Calibration on real queries (2026-10-07): the right agent scored
cosine 0.58–0.70, unrelated agents 0.48–0.57, so similarity is mapped onto the
intent score from 0.55 (nothing) to 0.75 (maximum).

**Registry fields that drive indirect routing** (decision M-c):

| Field | Meaning | Kairos (backfill) | MyFitnessPal (backfill, applied when it registers) |
|---|---|---|---|
| `domains` | What the agent is the authority for | tasks, reminders, schedule | nutrition, food log, calories |
| `intent_aliases` | Trigger phrases | "my list", "remind me", "my tasks" | "calories", "macros", "what did I eat", "log my lunch" |
| `routing_policy` | `owns_domain`: route every in-domain request. `on_request`: route only if user data or an action is needed | `owns_domain` | `on_request` |
| `user_data` | Holds the user's personal data: "my…" questions must route, never be answered by Gemini | true | true |
| `modes` and task fields (§1.7) | Live calls, background tasks, or both | `bridge` + `task`; `task_ops` dispatch, status, cancel, close, delivered; `events` callback; `max_concurrency` 4; `side_effects` true | set by its own registration |

**Bounded prompt.** Up to 30 agents are listed in the system prompt; beyond
that the prompt only gives the count and tells Gemini to use `find_agents`.
Prompt size no longer grows with the registry.

**Health.** After 3 consecutive connection failures an agent's circuit opens
for 60 s. It is ranked lower and tried after healthy agents, but a user who
names it still gets it (row 5).

### 1.4 Connect or dispatch (mode policy)

Gemini passes `mode_hint` (`connect`, `dispatch` or `auto`) taken from the
user's wording. The code in `app/orchestrator/routing/policy.py` decides, in this order:

| # | Rule | Result |
|---|---|---|
| 1 | The agent supports only one mode | That mode (row 6 of §1.2 if the hint asks for the other) |
| 2 | The user's wording was explicit ("talk to", "put me through" / "have X do", "let me know when") | What they said |
| 3 | Open-ended or conversational request, or more than one required detail missing | **Connect**: faster for the agent to ask live |
| 4 | Complete one-shot request, or anything future-dated or long-running | **Dispatch** |
| 5 | Still unclear | Ask: "Should I connect you, or have it done and let you know?" |

**Switching mid-flight.**
- **Task → live:** after an agent's second question on the same task, the
  orchestrator offers "Tabletop has a few questions. Want me to put you
  through?" A yes cancels the task (reason `escalated_to_bridge`) and opens a
  live call carrying that `task_id`.
- **Live → task:** during a live call an agent may send `task.created` to hand
  off ongoing work ("I'll confirm and let you know"). The orchestrator assigns
  a `task_id` and tracks it like any dispatched task.

### 1.5 Tasks

**Storage.** Background tasks are rows in the shared `tasks` table with
`kind = 'agent_task'`, `created_by = 'orchestrator'` (or `'agent'` for hand-offs)
and `agent_id` set. Kairos reminders live in the same table and are
unaffected. `task_info` holds `intent`, `slots`, `contextId`,
`agent_task_ref`, `last_seq`, `on_deadline`, `deadline_job_id`,
`callback_token_sha256`, `stalled_since` and `overdue_since`.

| Database `status` | Agent wire state (A2A names) | Meaning |
|---|---|---|
| `pending` | — | Created, not yet acked (also while waiting for a detail the agent asked for) |
| `running` | `submitted`, `working` | The agent accepted it |
| `input_required` | `input-required` | The agent asked the user something (`question`) |
| `completed` | `completed` | Done; `result.say` is spoken |
| `failed` | `failed` | `result.error` is spoken |
| `cancelled` | `canceled` | Cancelled by the user or the orchestrator |
| `timed_out` | — | Deadline passed with `drop_at_deadline` |

The schema also allows `dispatching`; it is treated as active but never set.

**Operations** (voice through `manage_task`, or HTTP):

| Action | Voice | HTTP | Message to the agent |
|---|---|---|---|
| Create | `route_to_agent` (dispatch) | `POST /api/dispatch` | `task.dispatch` |
| Status | `manage_task(status)` | `GET /api/dispatch/{id}`, `GET /api/dispatch/user/{user_id}` | none; read from the row |
| Update | `manage_task(update, changes)` | `PATCH /api/dispatch/{id}` | `task.update`. If the agent doesn't list `update`, or nacks it as `unsupported`, the task is cancelled and re-sent with the change (read back first for agents with side effects). `too_late` → "too late to change, want me to cancel it instead?" |
| Cancel | `manage_task(cancel)` | `POST /api/dispatch/{id}/cancel` | `task.cancel` |
| Complete | `manage_task(complete)` | `POST /api/dispatch/{id}/complete` | `task.close {completed_by_user}` |
| Delete | `manage_task(delete)` | `DELETE /api/dispatch/{id}` | `task.cancel` if active, then `task.close {deleted}`; the row is deleted once the agent has been told |
| Answer | `manage_task(answer)` | `POST /api/dispatch/{id}/input` | `task.input` |

The HTTP API **always requires** `DISPATCH_API_TOKEN` and refuses everything
if it isn't configured (decision E1), because it can make agents act for any
user.

**Which task does "that" mean?** `task_ref` is resolved like an agent name:
the task mentioned last, then a match on agent name or intent words. Two
candidates → "The dinner booking or the taxi?"; none → "I don't see a task
like that." The orchestrator never acts on a guessed task, and spoken status
always comes from the database row, never from Gemini's memory.

**Limits.** At most 8 active tasks per user (C2). A task the user named an
agent for is never sent to a different agent (C3). Once an agent has acked a
task, it is never re-sent to a different agent (C4), because the work may
already have happened.

### 1.6 Telling the user, wake-ups and deadlines

**`notify`** decides how a result reaches the user:

| Value | When the result arrives |
|---|---|
| `device` | Session open → spoken. Otherwise a `task_result` job wakes the pin, and the result is announced when it calls in |
| `next_session` (**default**, M-e) | Session open → spoken. Otherwise announced at the next session |
| `silent` | Results are recorded only, heard if the user asks. An agent's questions are still relayed, since it is waiting on the user |

- `notify` is a slot like any other: "let me know when it's done" → `device`,
  "don't tell me" → `silent`, nothing said → the default. The orchestrator
  always sets it explicitly, because the database column defaults to `device`.
- **Agents can't raise it.** An agent's `notify_hint: urgent` is honoured only
  up to what the user set.
- **Wake limits** (M-f): no task wakes during quiet hours (22:00–07:00 in the
  user's `users.timezone`) and at most 4 per hour per user. Results held back
  are announced at the next session. Several results that finish together are
  announced in one wake.
- **At session start** up to 3 undelivered results are announced, oldest
  first, then "You have N more task updates. Want to hear them?" Each one is
  marked delivered, and the agent is told (`task.delivered`).
- **During a live call** results are held and announced when the call ends.
- **A stalled task** gets one `task.status` attempt when the user asks about it
  (never on a schedule). An `unknown_task` reply marks it lost.
- Failed tasks and tasks an agent lost are announced the same way.

**Deadlines are optional** (M-l). A deadline comes from the user ("forget it
if it's not done by six") or from the agent's registered `default_deadline_s`;
Gemini never invents one.

- **Scheduled only after the agent acks:** an `agent_task_deadline` job is
  inserted at `deadline_at`, carrying the task's context. A terminal event or
  the user ending the task cancels it; changing the deadline reschedules it.
- **At the deadline** the worker POSTs the payload to the orchestrator's
  internal endpoint (not to the device), which then:
  - by default (`notify`) leaves the task running, sets `overdue_since`, and
    says "Tabletop still hasn't finished booking Nopa. It was due at 7. Want me
    to cancel it?";
  - with `drop_at_deadline` ("forget it if…") sends `task.cancel {deadline}`,
    sets `timed_out`, and says "Tabletop didn't finish booking Nopa by 7, so I
    cancelled it."
- **A result that arrives after the deadline still wins**: "Tabletop finished
  after all: …". The deadline isn't paused while the user is being asked
  something.

### 1.7 Talking to agents (Protocol 2)

The full wire spec is in `BRIDGE_PROTOCOL.md`; the design points:

| Topic | Design | Why |
|---|---|---|
| **Tasks outlive connections** (M-g) | Every message carries `task_id`; any connection, or the HTTP callback, can carry any task's messages | So status, update and cancel work hours later |
| **IDs** | The orchestrator creates `task_id` (M-h). `contextId` groups one user's thread with an agent. `msg_id`/`reply_to` match replies; `seq` orders events | A resent dispatch can't create a second task |
| **Request → ack/nack** | Every request gets one `task.ack` or `task.nack` within the agent's `max_reply_latency_s` (default 5 s). Nack codes include `busy`, `missing_input {fields}`, `too_late`, `cannot_meet_deadline` | The orchestrator always knows whether a request landed |
| **Names** (M-k) | A2A vocabulary: `submitted`, `working`, `input-required`, `completed`, `canceled`, `failed`, `contextId` | One-to-one with A2A agents later |
| **Delivery to agents** (M-m) | Every request is written to `agent_outbox` first, then sent; resent with the same `msg_id` until acked; per task in order. A dispatch nobody acks after 3 sends fails over (unnamed requests) or is reported; everything else stays queued up to 24 h | Nothing is lost to a dropped connection |
| **Results from agents** (M-i) | **Push only.** The agent POSTs `task.event` to `/developer/tasks/{task_id}/events` with a per-task token (or sends it on an open connection), and resends until acked | No polling |
| **Connections** (B2) | At most one shared connection per agent, opened on demand, closed after 60 s idle | No 24/7 sockets; results arrive by HTTP anyway |
| **Persistence** (M-j) | Agents *should* save a task before acking (best effort) | Easier agents |
| **Liveness** | Heartbeat (`/developer/register` every 5 min) is required in task mode and must list `open_task_ids`. 15 min of silence flags the agent's tasks as stalled; a task missing from the list is marked failed ("Tabletop lost track of this"). Kairos sends no heartbeat (it runs inside the app): its tasks finish in seconds, a task open 15 min after creation is flagged stalled, and a `task.status` it doesn't recognise marks the task lost | Catches dead agents and lost tasks without polling |
| **Task URL** | A registered URL with `{user_id}` gets the end user's id for a live call and `orchestrator` for the shared task connection (`/ws/{user_id}` → `/ws/orchestrator`) | Per-user URLs like Kairos's can tell a task connection apart before `hello` |
| **Registration fields** | `binding` (`ws`), `modes`, `task_ops`, `events`, `max_concurrency`, `max_reply_latency_s`, `default_deadline_s`, `side_effects` (default **true**), `slots`, plus the routing fields of §1.3 | Capabilities are known before the agent is reached |

**Kairos as a task agent.** `/ws/orchestrator` goes to `app/kairos_tasks.py`
instead of a Gemini Live call. Each `task.dispatch` is acked, then runs
Kairos's text agent (`GeneralThinkingAgent.think`, the same tool loop a live
call's `think` uses) for the dispatch's `user_id`, with the intent and slots as
the request; the reply becomes `result.say`. Events go only to this app's own
`/developer/tasks/{task_id}/events` (never to a host named in the dispatch) and
are resent until acked. No `update` (the orchestrator cancels and re-dispatches)
or `input` (Kairos asks nothing in task mode). A cancel can't stop a run already
in progress: the result is dropped, but a reminder it already made stays.

### 1.8 Spoken lines

All are constants in `app/orchestrator/speech.py`, so they can be reworded in
one place.

| When | Line |
|---|---|
| Direct connect | "Connecting you to {agent} now." |
| Indirect connect | "Getting {agent} for that. Say no if that's not right." |
| Indirect connect cancelled | "Okay. Who should I ask instead?" |
| Ambiguous | "Did you mean {A} or {B}?" / "…{A}, {B} or {C}?" |
| Unknown | "I couldn't find an agent called {name}." |
| No agent for the request | "I don't have an agent that can do that." |
| Unreachable | "{agent} isn't answering right now." |
| Declined call | "{agent} declined the call." (+ the agent's reason) |
| Busy | "{agent} is busy right now. Try again in a moment." / "The agents are busy right now. Try again in a moment." |
| Can't do | "{agent} can't do that." |
| Can't meet the deadline | "{agent} can't finish that by the deadline. {reason} Want me to try without a deadline?" |
| Wrong mode | "{agent} doesn't take live calls, but I can send it a task." / "{agent} only takes live calls. Want me to connect you?" |
| Ask connect or dispatch | "Should I connect you, or have it done and let you know?" |
| Missing / ungrounded detail | "For what {slot}?" (or the slot's own question) |
| Read-back | "{summary}. Shall I go ahead?" |
| Dispatched | "Okay, I've asked {agent} to handle that." (+ "I'll let you know when it's done." unless `silent`) |
| Too many tasks | "You already have several tasks running. Want me to cancel one first?" |
| Agent question | "{agent} needs something from you: {question}" |
| Escalation offer | "{agent} has a few questions. Want me to put you through?" |
| Result | "{agent} finished: {say}" |
| Failure | "{agent} couldn't complete that. {error}" |
| Late result | "{agent} finished after all: {say}" |
| Overdue | "{agent} still hasn't finished {intent}. It was due at {time}. Want me to cancel it?" |
| Timed out | "{agent} didn't finish {intent} by {time}, so I cancelled it." |
| Lost | "{agent} lost track of {intent}." |
| Agent cancelled it | "{agent} cancelled {intent}." |
| Stalled | "{agent} hasn't checked in for a while, so {intent} may be stuck." |
| More updates | "You have {n} more task updates. Want to hear them?" |

Task-management replies (`manage_task`):

| When | Line |
|---|---|
| Status | "{agent} is still working on {intent}." / "…is waiting for you: {question}" / "…finished {intent}." |
| Updated / too late | "Okay, I've told {agent} about the change." / "{agent} says it's too late to change that. Want me to cancel it instead?" / "{agent} couldn't make that change. {reason}" / "{agent} already finished {intent}." / "What should I change?" |
| Cancel / delete (side effects) | "Cancel {intent}? It may already be confirmed." / "Delete {intent}? It may already be confirmed." → "Okay, I've cancelled {intent} with {agent}." |
| Too many tasks, then yes | "{status of up to 3 active tasks} Which one should I cancel?" |
| Complete / delete | "Okay, I've marked {intent} as done." / "Okay, I've removed {intent}." |
| Answer | "Okay, I've passed that to {agent}." / "{agent} isn't waiting for an answer right now." / "What should I tell {agent}?" |
| Which task | "The {A} or {B}?" / "I don't see a task like that." / "You don't have any tasks right now." |
| Yes/no | "Okay, I won't do that." / "There's nothing waiting for a yes right now." |
| `find_agents` | "I found 2 agents: {A}, {what it does}; {B}, …" / "I couldn't find an agent for that." |
| Already on a call | "You're already connected to an agent. Say stop to come back to me first." |
| Errors | "Sorry, something went wrong reaching that agent." / "Sorry, something went wrong with that task." |

`{intent}` is spoken as "your request to …" (e.g. "your request to book Nopa
for 2").

### 1.9 Future

- **FG-1, hosted personal agents (Meta Muse, OpenAI Dots).** They have no
  public server and no API that reaches a user's instance, so they would need
  a *pull* binding: the agent fetches requests from the outbox through our API
  (OpenAPI and MCP), using a per-user link token. Muse acts only when the user
  talks to it; a Dot could check the inbox on a standing goal, nudged by a
  Slack doorbell. v2 keeps this cheap: the envelope is transport-neutral, the
  outbox is the future inbox, and the registry has `binding` and
  `max_reply_latency_s`.
- **FG-2, real recorded speech in the eval** (§2.5).
- **FG-3, persist Kairos's dispatch records.** Kairos keeps its own record of
  each dispatch (status, seq, callback token, unacked events, reply) in memory.
  The orchestrator's task and the reminder are in the DB, but after a restart
  Kairos forgets it was asked: a run in progress is lost, an unacked result
  can't be resent, and a resent dispatch could create a second reminder. Fix:
  a small table keyed by `task_id` (via `deploy/sql/`), checked before running a
  dispatch; on startup resend unacked events and report interrupted runs as
  failed rather than re-running them.

---

## Part 2: Reliable tool calls (When2Call)

### 2.1 What When2Call found

*When2Call: When (not) to Call Tools* (Ross, Mahabaleshwarkar, Suhara; NAACL
2025) tests whether a model picks the right **kind** of response: call a tool,
ask a follow-up, say it can't, or answer directly. Test set: 3,652 items built
from BFCL and APIGen, scored by log-probability or an LLM judge.

| Model | When2Call F1 | Calls a tool when **no tools exist** |
|---|---|---|
| Llama 3.1 8B / 70B | 16.6 / 37.8 | 67% / 57% |
| Qwen 2.5 7B / 72B | 32.0 / 32.8 | 21% / 23% |
| GPT-4o-mini / GPT-4o | 52.9 / 61.3 | 41% / 26% |
| 8B trained with When2Call preference pairs (RPO) | 52.4 | 1.2% |

**Takeaways:**
1. **Over-calling is the default failure**, even for GPT-4o.
2. **Scale doesn't fix it**, and good scores on "irrelevant tool" tests don't
   predict it.
3. **Models invent missing arguments** instead of asking, and some model
   families avoid saying "I can't".
4. **Plain fine-tuning on abstention makes models over-cautious**;
   preference training fixes both.
5. **The paper never tested prompting.** Prompt rules alone are unproven, so
   checks in code carry the weight.

### 2.2 What it means for the orchestrator

Every agent is a tool, and agents act in the world, so a wrong call books the
wrong table. Voice makes it worse: STT garbles values ("nopa" → "notebook"),
people correct themselves mid-sentence, and there's no screen to catch
mistakes.

| Outcome | When | Failure it prevents |
|---|---|---|
| **Answer** | General knowledge suffices (valid for us, unlike When2Call) | Needless dispatch |
| **Ask** | A detail is missing or the name is ambiguous | Invented arguments |
| **Decline** | No agent can do it | Wrong agent, or claiming success |
| **Dispatch** | Do it and report back | |
| **Connect** | Talk to an agent live | |

### 2.3 The defences

| Layer | What | In v2 |
|---|---|---|
| L0 Tool surface | Five tools; required slots from the agent's schema; no default-agent fallback; bounded agent list | Yes |
| L1 Prompt | Explicit rules for the five outcomes (§2.6) | Yes (unproven alone) |
| **L2 Validation gate** | Code checks every call before it runs (§2.4). **The main defence** (2a) | **Yes** |
| L3 Model-assisted checks | Shortlist-then-choose, decision classifier | No (embeddings chosen instead, A1) |
| L4 Model choice by eval | Pick the model on our eval set | When the eval exists |
| L5 Preference training | RPO/DPO, never plain fine-tuning | Only if we self-host a model |

### 2.4 The validation gate

`app/orchestrator/routing/gate.py`. Pure code, no extra LLM call: **under ~20 ms**.
Its only real cost is an extra question when a call fails a check.

| Check | Fails when | Then | Rationale |
|---|---|---|---|
| Agent decision | The router returns `ambiguous`, `none` or `wrong_mode` | Rows 3, 4, 6 of §1.2 | Never guess an agent |
| Required slots | A slot in the agent's `slots` schema is missing | Ask (row 10) | Missing info must be visible, not buried in free text |
| **Grounding** (2c) | A slot value can't be traced to the user's last few turns | Ask (row 11) | The core When2Call failure: invented arguments |
| Self-correction | A value only appears before a correction ("no", "actually", "I mean", "wait", "make that") | Use the later value, or ask | Where every speech model failed in Full-Duplex-Bench-v3 |
| Side effects (2b) | The agent's `side_effects` is true (the default) | Read back; act only on yes | Wrong values cost real money and time |
| Task reference | `task_ref` matches zero or several tasks | Ask or decline | Never act on a guessed task |
| Destructive action | Cancel or delete of an accepted side-effect task | Confirm | "It may already be confirmed." |
| Notify | `device` without the user asking | Use the default | Waking the pin needs consent |
| Agent output | Text from an agent | Quoted as agent output; never triggers a tool without a user turn | Prompt injection |
| Named agent | Gemini filled in `agent` but the user never said that name | Treated as an indirect route (cancel cue, failover allowed) | Seen with the real model (§2.7) |

**Grounding is lenient** (2c): case and punctuation are ignored; numbers match
in words or digits ("seven" = 7, "7pm" = 19:00 = "seven"); names match
phonetically; at least half of a value's content words must appear. It's
tightened using logs. **Over-caution is capped at 5%** (2d): if the eval shows
more than 1 in 20 complete requests getting an unnecessary question, the
checks are loosened.

### 2.5 Evaluation

A When2Call-style set of ~200 items generated from the registry. Each item is
rendered as **TTS audio for now** (2e), with When2Call's own items as a text
control.

| Case | Expected |
|---|---|
| Complete request | Dispatch or connect |
| Missing slot | Ask |
| Unservable | Decline |
| General knowledge | Answer |
| Unknown or ambiguous agent | Decline / ask |
| Garbled name ("kai ross") | Route correctly |
| Late self-correction | Later value |
| Indirect route: personal vs general question | Route / answer |
| Connect vs dispatch wording | Policy outcome |
| Task reference, notify, status | Right task, `device` only when asked, status from the row |

**Metrics:** macro-F1 over the five outcomes, tool-hallucination rate,
argument-hallucination rate, wrong-agent rate, over-ask rate (≤ 5%), and
latency.

**Known blind spot (FG-2):** TTS audio is cleaner than real speech, so the
eval overstates accuracy. Real recordings (~50 scripted clips plus logged
transcripts) are a future step, needed before the thresholds count as tuned.

### 2.6 Prompt rules

Appended to the system prompt whenever agents exist (prod overrides the base
prompt with `DEVELOPER_GEMINI_SYSTEM_INSTRUCTION`, so the rules are appended,
not merged):

```
Routing and tasks. For each turn decide exactly one:
 ANSWER   general knowledge is enough -> reply, no tool. Never answer questions about the
          user's own data ("my ...", "have I ...") yourself: route them.
 ASK      a needed detail is missing or you're unsure what they said -> ONE short question.
          Never fill in a value the user didn't say.
 DECLINE  nothing can do it -> say so. Never claim something was done.
 ROUTE    call route_to_agent: mode_hint "connect" for live talk, "dispatch" for
          get-it-done-and-report, "auto" if unsure. Pass agent names exactly as heard.
 TASKS    use manage_task for status, changes, cancelling, completing, answering an agent's
          question, and for the user's yes/no to the orchestrator's questions ("Shall I go
          ahead?", "Want me to ...?", "Want me to put you through?") -> confirm / decline.
If the user corrects themselves ("no, Wednesday"), use the LAST value.
The orchestrator speaks tool outcomes itself; don't repeat or rephrase them.
Live-call state: "Connecting you to <agent> now." or "Getting <agent> for that..." means a live
call opened; "... disconnected. You're back with me now." means it closed.
Speech-to-text garbles names ("Kairos" may arrive as "cut in", "cairo's", "kai ross"); pass
what you heard, the orchestrator resolves it.
```

### 2.7 Real-model check (2026-10-07)

Nine utterances through the production Gemini model
(`gemini-3-flash-preview`) with the five tools and the §2.6 rules. This is a
smoke test, not the eval:

| User said | Gemini did | Then |
|---|---|---|
| "what's the capital of France" | Answered | ✔ |
| "can i speak to cairo's" | `route_to_agent(agent="cairo's", mode_hint=connect)` | ✔ Router resolves Kairos |
| "have tabletop book nopa for two at seven tonight and let me know" | Dispatch with restaurant, party_size=2, time=19:00, date=today, `notify=device` | ✔ All values grounded; read-back |
| "have tabletop book nopa" | Dispatch with only the restaurant | ✔ Gate asks for the party size |
| "how many calories have I had today" | `route_to_agent(agent="MyFitnessPal")`: **named an agent the user didn't say** | Gate now treats this as an indirect route |
| "how many calories are in a banana" | Answered | ✔ |
| "book nopa for two at seven, no, eight" | Used the later value (08:00) | ✔ Correction already applied |
| "is my table booked yet" | `manage_task(status, task_ref="the table booking")` | ✔ |
| "cancel the dinner booking" | `manage_task(cancel, task_ref="the dinner booking")` | ✔ Confirmed if the agent has side effects |

The schemas (with name/value pairs for `slots`) were accepted, and no tool was
called for general-knowledge questions.

---

## Appendix A: Decision log

| # | Decision | Choice | Rationale |
|---|---|---|---|
| M-a | Routing tool | One `route_to_agent` + code-side mode policy | Models confuse similar tools |
| M-b | Indirect-route confirmation | Announce with "Say no if that's not right", 1.5 s window after the sentence | Fast, and the option is obvious |
| M-c | Routing registry fields | `domains`, `intent_aliases`, `routing_policy`, `user_data`; backfill Kairos, MyFitnessPal | Makes intent routing deterministic |
| M-d | Task storage | `tasks` table | One source of truth, survives restarts |
| M-e | Default notify | `next_session` | No surprise wake-ups |
| M-f | Wake limits | Quiet hours 22:00–07:00, ≤ 4/hour | Waking the pin is the most intrusive action |
| M-g | Tasks outlive connections | Yes | Needed for later status, update, cancel |
| M-h | `task_id` creator | Orchestrator | Safe resends, no duplicates |
| M-i | Results | Push only, HTTP callback required | No polling |
| M-j | Persist before ack | Best effort; heartbeat `open_task_ids` required | Easier agents, lost tasks still caught |
| M-k | Names | A2A states and `contextId` | Interop for free while unmerged |
| M-l | Deadlines | Optional, scheduled after ack, default `notify` | Most tasks need none |
| M-m | Outbox + `binding`/`max_reply_latency_s` | Yes | Reliable delivery; cheap FG-1 |
| A1 | Intent matching | Lexical + aliases + embeddings | Covers oddly phrased requests |
| A2 | Thresholds | 0.55 / 0.12 / 0.92, tuned on logs | Starting point |
| A3 | Unknown agent | Decline | No silent wrong agent |
| B1 | Agent URL | One URL, `mode` in hello | One tunnel per agent |
| B2 | Task connections | One shared per agent, on demand | Few sockets |
| B4 | Progress | Recorded, not spoken | Avoid chatter |
| C2–C4 | Task limits | 8 per user; never substitute a named agent; never re-send after ack | Fairness; no surprises; no double side effects |
| D1–D2 | Spoken results | Fixed sentences; held during live calls | Exact and fast; no talking over agents |
| E1 | Dispatch API | Token always required | It can act for any user |
| E2 | `/api/agents` | Sorted by name | Stable listing |
| 2a | Main defence | Validation gate | Prompts unproven |
| 2b | Read-back | Always for side-effect agents; `side_effects` defaults true | Agents can't skip it by omission |
| 2c | Ungrounded values | Block and ask, lenient matching | Invented values never reach agents |
| 2d | Over-caution | ≤ 5% | Bound the cost of the gate |
| 2e | Eval audio | TTS now, real speech later (FG-2) | Start now, note the blind spot |

## Appendix B: Configuration

| Env var | Default | Purpose |
|---|---|---|
| `AGENT_ROUTER_REFRESH_S` | 30 | Snapshot refresh |
| `AGENT_ROUTER_MATCH_FLOOR` / `_AMBIGUITY_MARGIN` / `_CONFIDENT_SCORE` | 0.55 / 0.12 / 0.92 | Decisions |
| `AGENT_ROUTER_FAILURE_THRESHOLD` / `_COOLDOWN_S` | 3 / 60 | Circuit breaker |
| `AGENT_ROUTER_EMBEDDINGS` | 1 | `0` disables embeddings |
| `GEMINI_EMBEDDING_MODEL` / `AGENT_ROUTER_EMBED_TIMEOUT_S` / `AGENT_ROUTER_EMBED_DIM` | `gemini-embedding-001` / 0.3 / 768 | Embedding model, budget, size |
| `AGENT_ROUTER_EMBED_LOW` / `_HIGH` | 0.55 / 0.75 | Cosine → intent-score mapping |
| `DEVELOPER_PROMPT_AGENT_LIMIT` | 30 | Agents listed in the prompt |
| `ORCHESTRATOR_INDIRECT_CANCEL_WINDOW_S` | 1.5 | M-b window |
| `DISPATCH_API_TOKEN` | unset → HTTP dispatch API refuses all | E1 |
| `INTERNAL_API_TOKEN` | derived from `DB_PASSWORD` | Worker → orchestrator deadline calls |
| `ORCHESTRATOR_PUBLIC_BASE_URL` | first public host seen on `/ws/developer` | Callback URL given to agents |
| `DISPATCH_MAX_ACTIVE_PER_USER` | 8 | C2 |
| `DISPATCH_REPLY_LATENCY_S` | 5 | Default `max_reply_latency_s` |
| `DISPATCH_MAX_SENDS` / `DISPATCH_RETRY_BASE_S` | 3 / 5 | Dispatch sends before failover; resend backoff base |
| `DISPATCH_CONNECT_TIMEOUT_S` / `DISPATCH_HANDSHAKE_TIMEOUT_S` | 5 / 5 | Task connections |
| `DEVELOPER_WS_BRIDGE_CONNECT_TIMEOUT_S` | 5 | Live-call connect timeout |
| `ORCHESTRATOR_INTERNAL_URL` (worker) | `http://app:8000` | Where the worker sends deadline calls |
| `ORCHESTRATOR_DEFAULT_TZ` | `America/Los_Angeles` | Used when a user has no timezone |
| `DISPATCH_IDLE_CLOSE_S` / `DISPATCH_MAX_CONNECTIONS` | 60 / 200 | Connection pool |
| `DISPATCH_OUTBOX_SWEEP_S` / `DISPATCH_OUTBOX_TTL_H` | 30 / 24 | Outbox retries |
| `DISPATCH_STALL_AFTER_S` / `DISPATCH_STALL_CHECK_S` | 900 / 60 | Heartbeat silence before stalled; how often it's checked |
| `DISPATCH_RECONCILE_GRACE_S` | 120 | A just-dispatched task isn't marked lost by a heartbeat that predates it |
| `ORCHESTRATOR_QUIET_HOURS` / `ORCHESTRATOR_MAX_WAKES_PER_HOUR` | `22-7` / 4 | M-f |
| `KAIROS_CALLBACK_BASE` | `http://127.0.0.1:$PORT` | Where Kairos posts task events (always this app) |
| `KAIROS_TASK_CONCURRENCY` | 4 | Kairos task runs at once |

## Appendix C: Sources

- Ross, Mahabaleshwarkar, Suhara. *When2Call: When (not) to Call Tools.* NAACL
  2025. [arXiv 2504.18851](https://arxiv.org/abs/2504.18851) ·
  [code and data](https://github.com/NVIDIA/When2Call)
- *Full-Duplex-Bench-v3.* [arXiv 2604.04847](https://arxiv.org/abs/2604.04847)
- Hosted personal agents (FG-1), launch-week coverage, October 2026:
  [TechCrunch: Dots](https://techcrunch.com/2026/09/29/openai-launches-dots-its-bubbly-agentic-avatar/) ·
  [TechCrunch: Muse](https://techcrunch.com/2026/09/08/meta-debuts-its-muse-ai-agent-will-consumers-trust-it/) ·
  [Sprites: Muse connectors](https://www.sprites.ai/muse/connectors)
- Google Agent2Agent (A2A) protocol: task states and `contextId`
  *(verify against the current spec)*
