"""TaskService: background tasks end to end (ORCHESTRATOR_V2_TOOL_CALLS.md §1.5–1.7).

* **Dispatch.** A task row is created in `tasks` (`kind = 'agent_task'`,
  status `pending`), and a `task.dispatch` goes through the outbox. The agent's
  ack moves it to `running` and schedules the optional deadline job. A nack
  fails over (unnamed requests only), asks the user for a missing detail, or
  fails the task.
* **Events.** Agents push `task.event` (HTTP callback with a per-task token,
  or an open socket). Events are deduplicated by `seq`; terminal events cancel
  the deadline job. A result that arrives after a deadline still wins.
* **Delivery.** Each announcement goes to a live session if there is one
  (the pipeline holds it during a live call). Otherwise `notify` decides:
  `device` wakes the pin within the wake limits, `next_session` waits for the
  next session, `silent` records only. The owning agent hears `task.delivered`.
* **Task management.** status, update, cancel, complete, delete, answer.
* **Deadlines.** Scheduled as a `jobs` row once the agent acks; the worker
  calls `on_deadline` at that time with the task's context.
* **Liveness.** Heartbeat `open_task_ids` reconciliation marks tasks an agent
  lost as failed; heartbeat silence flags tasks as stalled. No polling.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from orchestrator.routing import policy
from orchestrator import speech
from orchestrator.tasks import protocol as proto
from orchestrator.tasks.link import AgentLink
from orchestrator.routing.router import AgentRecord, AgentRouter, get_router
from orchestrator.tasks.store import TaskStore, hash_token, parse_ts, utcnow

log = logging.getLogger("task_service")


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_ACTIVE_PER_USER = int(_env_f("DISPATCH_MAX_ACTIVE_PER_USER", 8))
STALL_AFTER_S = _env_f("DISPATCH_STALL_AFTER_S", 900)
STALL_CHECK_S = _env_f("DISPATCH_STALL_CHECK_S", 60)
RECONCILE_GRACE_S = _env_f("DISPATCH_RECONCILE_GRACE_S", 120)
SESSION_START_MAX = 3

# Reasons returned by dispatch().
OK = "ok"
USER_LIMIT = "user_limit"
BUSY = "busy"
BAD_REQUEST = "bad_request"

# Announcement kinds.
KIND_RESULT = "result"
KIND_FAILURE = "failure"
KIND_QUESTION = "question"
KIND_INFO = "info"


class TaskError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass
class Announcement:
    task_id: str
    user_id: str
    agent_name: str
    line: str
    kind: str
    terminal: bool
    # For an escalation offer, the agent to connect to on "yes".
    escalate_agent_id: str = ""
    # A yes/no offer attached to the line: "cancel" (overdue, too late to
    # change) or "retry_without_deadline" (agent can't meet the deadline).
    offer: str = ""


Listener = Callable[[Announcement], Awaitable[bool]]


@dataclass
class DispatchOutcome:
    ok: bool
    reason: str = OK
    task: Optional[dict] = None
    agent: Optional[AgentRecord] = None


def iso_utc(value: Any) -> Optional[str]:
    """ISO 8601 UTC with a Z suffix, as the wire spec requires."""
    dt = parse_ts(value)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def what(task: dict) -> str:
    """A spoken reference to a task ("your request to book Nopa for 2")."""
    intent = str((task.get("task_info") or {}).get("intent") or "").strip().rstrip(".")
    if not intent:
        return "that task"
    return "your request to " + intent[0].lower() + intent[1:]


def spoken_time(dt: Optional[datetime], tz_name: Optional[str]) -> str:
    if dt is None:
        return "the deadline"
    try:
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo(os.environ.get("ORCHESTRATOR_DEFAULT_TZ", "America/Los_Angeles"))
    except Exception:
        tz = timezone.utc
    local = dt.astimezone(tz)
    h = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{h}:{local.minute:02d} {suffix}" if local.minute else f"{h} {suffix}"


def to_public(task: dict) -> dict:
    info = task.get("task_info") or {}
    out = {
        "task_id": task["task_id"], "user_id": task["user_id"], "agent_id": task.get("agent_id"),
        "agent_name": info.get("agent_name"), "intent": info.get("intent"), "slots": info.get("slots") or {},
        "status": task["status"], "notify": task.get("notify"), "question": task.get("question"),
        "result": task.get("result"), "contextId": info.get("contextId"),
        "deadline_at": task.get("deadline_at"), "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"), "finished_at": task.get("finished_at"),
        "delivered_at": task.get("delivered_at"), "stalled": bool(info.get("stalled_since")),
        "overdue": bool(info.get("overdue_since")),
    }
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in out.items()}


class TaskService:
    def __init__(
        self,
        store: Optional[TaskStore] = None,
        router: Optional[AgentRouter] = None,
        link: Optional[AgentLink] = None,
        *,
        public_base: Optional[str] = None,
        max_active_per_user: int = MAX_ACTIVE_PER_USER,
    ) -> None:
        self.store = store or TaskStore()
        self._router = router
        self.link = link or AgentLink(self.store, self.router)
        self.link.on_reply = self.on_reply
        self.link.on_event = self._on_ws_event
        self.link.on_expired = self.on_dispatch_expired
        self._public_base = (public_base or os.environ.get("ORCHESTRATOR_PUBLIC_BASE_URL", "")).rstrip("/")
        self._max_active = max_active_per_user
        self._listeners: dict[str, list[Listener]] = {}
        self._bg: list[asyncio.Task] = []

    @property
    def router(self) -> AgentRouter:
        return self._router or get_router()

    # ----- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self.link.start()
        if not self._bg:
            self._bg.append(asyncio.get_running_loop().create_task(self._stall_forever(), name="task-stall-check"))

    async def stop(self) -> None:
        for t in self._bg:
            t.cancel()
        for t in self._bg:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._bg.clear()
        await self.link.stop()

    # ----- public base URL (for agent callbacks) ----------------------------

    def note_public_host(self, scheme: str, host: str) -> None:
        """First public host seen on /ws/developer becomes the callback base if unset."""
        if self._public_base or not host or host.startswith(("127.", "localhost", "0.0.0.0")):
            return
        self._public_base = f"{'https' if scheme in ('https', 'wss') else 'http'}://{host}".rstrip("/")
        log.info("callback base URL set from first public request: %s", self._public_base)

    def callback_url(self, task_id: str) -> str:
        base = self._public_base or "http://127.0.0.1:8000"
        return f"{base}/developer/tasks/{task_id}/events"

    # ----- listeners (live sessions) ----------------------------------------

    def subscribe(self, user_id: str, listener: Listener) -> Callable[[], None]:
        self._listeners.setdefault(user_id, []).append(listener)

        def _unsub() -> None:
            ls = self._listeners.get(user_id, [])
            if listener in ls:
                ls.remove(listener)
            if not ls:
                self._listeners.pop(user_id, None)

        return _unsub

    def has_session(self, user_id: str) -> bool:
        return bool(self._listeners.get(user_id))

    # ----- helpers ----------------------------------------------------------

    async def _task(self, task_id: str) -> Optional[dict]:
        return await asyncio.to_thread(self.store.get_task, task_id)

    async def _update(self, task_id: str, *, info: Optional[dict] = None, **fields: Any) -> Optional[dict]:
        return await asyncio.to_thread(lambda: self.store.update_task(task_id, info=info, **fields))

    def _agent_of(self, task: dict) -> Optional[AgentRecord]:
        return self.router.get(task.get("agent_id") or "")

    def _name_of(self, task: dict) -> str:
        info = task.get("task_info") or {}
        a = self._agent_of(task)
        return (a.name if a else "") or info.get("agent_name") or "The agent"

    async def _send(self, task: dict, mtype: str, body: dict) -> None:
        agent = self._agent_of(task)
        if agent is None:
            await asyncio.to_thread(self.router.load_now)
            agent = self._agent_of(task)
        if agent is None:
            log.warning("no agent for task %s; dropping %s", task["task_id"], mtype)
            return
        info = task.get("task_info") or {}
        if mtype not in (proto.TASK_DISPATCH, proto.TASK_CANCEL) and not agent.supports_op(proto.OP_OF[mtype]):
            return  # optional op the agent didn't advertise (fallbacks are the caller's job)
        msg = proto.envelope(
            mtype, task_id=task["task_id"], context_id=info.get("contextId"),
            agent_task_ref=info.get("agent_task_ref"), body=body,
        )
        await self.link.send(agent, task["task_id"], msg)

    # ----- dispatch ---------------------------------------------------------

    async def dispatch(
        self,
        *,
        user_id: str,
        agent: AgentRecord,
        intent: str,
        slots: Optional[dict] = None,
        notify: str = "next_session",
        deadline_at: Optional[datetime] = None,
        drop_at_deadline: bool = False,
        context_id: Optional[str] = None,
        chain: Sequence[AgentRecord] = (),
        named: bool = True,
        created_by: str = "orchestrator",
    ) -> DispatchOutcome:
        if not user_id or not (intent or "").strip():
            return DispatchOutcome(False, BAD_REQUEST)
        if await asyncio.to_thread(self.store.count_active, user_id) >= self._max_active:
            return DispatchOutcome(False, USER_LIMIT)
        candidates = [agent] + [a for a in chain if a.id != agent.id]
        chosen: Optional[AgentRecord] = None
        for cand in candidates:
            busy = len(await asyncio.to_thread(self.store.open_tasks_for_agent, cand.id)) >= cand.max_concurrency
            if not busy:
                chosen = cand
                break
            if named:
                break
        if chosen is None:
            return DispatchOutcome(False, BUSY, agent=agent)
        if deadline_at is None and chosen.default_deadline_s:
            deadline_at = utcnow() + timedelta(seconds=float(chosen.default_deadline_s))
        task_id = proto.new_task_id()
        token = proto.new_callback_token()
        info = {
            "intent": intent.strip(), "slots": dict(slots or {}), "contextId": context_id or proto.new_context_id(),
            "agent_name": chosen.name, "on_deadline": "cancel" if drop_at_deadline else "notify",
            "callback_token_sha256": hash_token(token), "named": bool(named),
            "chain": [a.id for a in candidates if a.id != chosen.id][:2] if not named else [],
            "question_count": 0, "last_seq": 0,
        }
        task = await asyncio.to_thread(
            lambda: self.store.create_agent_task(
                task_id=task_id, user_id=user_id, agent_id=chosen.id, task_info=info,
                notify=notify, deadline_at=deadline_at, created_by=created_by,
            )
        )
        await self._send_dispatch(task, chosen, token)
        log.info("dispatch: task_id=%s user_id=%s agent=%s intent=%r", task_id, user_id, chosen.name, intent)
        return DispatchOutcome(True, OK, task=task, agent=chosen)

    async def _send_dispatch(self, task: dict, agent: AgentRecord, token: str) -> None:
        info = task.get("task_info") or {}
        deadline = task.get("deadline_at")
        body = {
            "user_id": task["user_id"], "intent": info.get("intent"), "slots": info.get("slots") or {},
            "input": {}, "deadline_at": iso_utc(deadline),
            "notify_hint_allowed": task.get("notify") != "silent",
            "callback": {"url": self.callback_url(task["task_id"]), "token": token},
        }
        msg = proto.envelope(proto.TASK_DISPATCH, task_id=task["task_id"], context_id=info.get("contextId"), body=body)
        await self.link.send(agent, task["task_id"], msg)

    async def _redispatch(self, task: dict, agent: AgentRecord) -> None:
        """Re-send a task (same task_id) with a fresh callback token, e.g. after
        the user supplied a missing detail or on failover."""
        token = proto.new_callback_token()
        task = await self._update(
            task["task_id"], agent_id=agent.id, status=proto.DB_PENDING,
            info={"callback_token_sha256": hash_token(token), "agent_name": agent.name},
        ) or task
        await self._send_dispatch(task, agent, token)

    # ----- replies to our requests ------------------------------------------

    async def on_reply(self, agent: AgentRecord, row: dict, msg: dict) -> None:
        task = await self._task(row["task_id"])
        if task is None:
            return
        rtype, kind, body = row["type"], msg.get("type"), msg.get("body") or {}
        if rtype == proto.TASK_DISPATCH:
            await self._on_dispatch_reply(agent, task, kind, body)
        elif rtype == proto.TASK_UPDATE and kind == proto.TASK_NACK:
            code = str(body.get("code") or "")
            if code == proto.NACK_UNSUPPORTED:
                # The agent can't update in place: cancel and re-create.
                changes = (row.get("envelope") or {}).get("body", {}).get("changes") or {}
                line = await self._recreate(task, changes)
                await self.announce(task, line, KIND_INFO)
            elif code == proto.NACK_TOO_LATE:
                await self.announce(task, speech.update_too_late(self._name_of(task)), KIND_INFO, offer="cancel")
            else:
                await self.announce(task, speech.update_failed(self._name_of(task), proto.clip(body.get("message") or "", 200)), KIND_INFO)
        elif rtype == proto.TASK_CANCEL and kind == proto.TASK_ACK:
            if body.get("status") == "already_finished" and body.get("result"):
                await self._record_result(task, proto.COMPLETED, body.get("result") or {}, late=True)
        elif rtype == proto.TASK_CLOSE:
            await self._update(task["task_id"], agent_informed_at=utcnow())
            if (task.get("task_info") or {}).get("deleted"):
                await asyncio.to_thread(self.store.delete_task, task["task_id"])
        elif rtype == proto.TASK_STATUS and kind == proto.TASK_ACK:
            await self._apply_state(agent, task, body, seq=None)
        elif rtype == proto.TASK_STATUS and kind == proto.TASK_NACK and body.get("code") == proto.NACK_UNKNOWN_TASK:
            await self._mark_lost(task)
        task = await self._task(row["task_id"])
        if task is not None and (task.get("task_info") or {}).get("deleted"):
            if await asyncio.to_thread(self.store.outbox_open_count, task["task_id"]) == 0:
                await asyncio.to_thread(self.store.delete_task, task["task_id"])

    async def _on_dispatch_reply(self, agent: AgentRecord, task: dict, kind: str, body: dict) -> None:
        if task["status"] in proto.DB_TERMINAL:
            return
        if kind == proto.TASK_ACK:
            self.router.report_success(agent.id)
            status = proto.WIRE_TO_DB.get(str(body.get("status") or proto.SUBMITTED), proto.DB_RUNNING)
            info: dict[str, Any] = {}
            if body.get("agent_task_ref"):
                info["agent_task_ref"] = str(body["agent_task_ref"])
            task = await self._update(task["task_id"], status=status, info=info) or task
            await self._schedule_deadline(task)
            return
        code = str(body.get("code") or "")
        message = proto.clip(body.get("message") or "", 300)
        info = task.get("task_info") or {}
        if code == proto.NACK_BUSY and not info.get("named") and info.get("chain"):
            nxt_id, rest = info["chain"][0], info["chain"][1:]
            nxt = self.router.get(nxt_id)
            if nxt is not None:
                await self._update(task["task_id"], info={"chain": rest})
                await self._redispatch(task, nxt)
                return
        if code in (proto.NACK_MISSING_INPUT, proto.NACK_INVALID_INPUT):
            fields = body.get("fields") or ([body["field"]] if body.get("field") else [])
            await self._update(task["task_id"], info={"pending_input": {"fields": fields, "message": message}})
            field_name = str(fields[0]) if fields else "that"
            await self.announce(task, message or speech.ask_for_slot(field_name), KIND_QUESTION)
            return
        if code == proto.NACK_CANNOT_MEET_DEADLINE:
            # Offer a retry without the deadline (task stays pending until answered).
            await self._update(task["task_id"], info={"deadline_offer": True})
            await self.announce(task, speech.cannot_meet_deadline(self._name_of(task), message), KIND_QUESTION,
                                offer="retry_without_deadline")
            return
        if code == proto.NACK_BUSY:
            line = speech.BUSY if not info.get("named") else speech.busy_named(self._name_of(task))
        elif code == proto.NACK_UNSUPPORTED_INTENT:
            line = speech.cant_do(self._name_of(task))
        else:
            line = speech.failure(self._name_of(task), message)
        task = await self._update(
            task["task_id"], status=proto.DB_FAILED, finished_at=utcnow(),
            result={"error": message or code},
        ) or task
        await self.announce(task, line, KIND_FAILURE, terminal=True)

    async def on_dispatch_expired(self, agent: AgentRecord, row: dict) -> None:
        """No ack after the maximum sends: fail over (unnamed) or report."""
        task = await self._task(row["task_id"])
        if task is None or task["status"] != proto.DB_PENDING:
            return
        self.router.report_failure(agent.id, "dispatch never acked")
        # The agent may have received it and lost the ack: tell it to drop it.
        await self._send(task, proto.TASK_CANCEL, {"reason": proto.CANCEL_ACCEPT_TIMEOUT})
        info = task.get("task_info") or {}
        if not info.get("named") and info.get("chain"):
            nxt = self.router.get(info["chain"][0])
            if nxt is not None:
                await self._update(task["task_id"], info={"chain": info["chain"][1:]})
                await self._redispatch(task, nxt)
                return
        task = await self._update(
            task["task_id"], status=proto.DB_FAILED, finished_at=utcnow(),
            result={"error": "agent unreachable"},
        ) or task
        await self.announce(task, speech.unreachable(agent.name), KIND_FAILURE, terminal=True)

    # ----- events from agents -----------------------------------------------

    async def _on_ws_event(self, agent: AgentRecord, msg: dict) -> Optional[int]:
        return await self.handle_event(msg, agent=agent)

    async def handle_event(self, msg: dict, *, agent: Optional[AgentRecord] = None,
                           token: Optional[str] = None, task_id: Optional[str] = None) -> int:
        """Ingest one task.event. Returns the seq to ack. Raises TaskError."""
        task_id = str(task_id or msg.get("task_id") or "")
        task = await self._task(task_id) if task_id else None
        if task is None:
            raise TaskError(proto.NACK_UNKNOWN_TASK)
        info = task.get("task_info") or {}
        if agent is not None and agent.id != task.get("agent_id"):
            raise TaskError(proto.NACK_UNAUTHORIZED, "event from a different agent")
        if token is not None and hash_token(token) != info.get("callback_token_sha256"):
            raise TaskError(proto.NACK_UNAUTHORIZED, "bad callback token")
        try:
            seq = int(msg.get("seq"))
        except (TypeError, ValueError):
            raise TaskError(proto.NACK_INVALID_INPUT, "seq must be a positive integer")
        if seq < 1:
            raise TaskError(proto.NACK_INVALID_INPUT, "seq must be a positive integer")
        last = int(info.get("last_seq") or 0)
        if seq <= last:
            return last  # duplicate: already recorded
        await self._update(task_id, info={"last_seq": seq})
        agent = agent or self._agent_of(task)
        await self._apply_state(agent, task, msg.get("body") or {}, seq=seq)
        return seq

    async def _apply_state(self, agent: Optional[AgentRecord], task: dict, body: dict, *, seq: Optional[int]) -> None:
        state = str(body.get("status") or "")
        if state not in proto.WIRE_STATES:
            return
        name = self._name_of(task)
        if task["status"] in proto.DB_TERMINAL:
            # A result after we gave up (deadline) or after a cancel: it still wins.
            if state == proto.COMPLETED and task["status"] in (proto.DB_TIMED_OUT, proto.DB_CANCELLED):
                await self._record_result(task, state, body.get("result") or {}, late=True)
            return
        if state in (proto.SUBMITTED, proto.WORKING):
            info: dict[str, Any] = {}
            progress = body.get("progress") or {}
            if isinstance(progress, dict) and progress.get("message"):
                info["progress"] = proto.clip(progress["message"], 300)  # recorded, not spoken (B4)
            await self._update(task["task_id"], status=proto.DB_RUNNING, question=None, info=info)
            return
        if state == proto.INPUT_REQUIRED:
            question = proto.clip(body.get("question") or "", proto.MAX_QUESTION_CHARS)
            count = int((task.get("task_info") or {}).get("question_count") or 0) + 1
            task = await self._update(
                task["task_id"], status=proto.DB_INPUT_REQUIRED, question=question,
                info={"question_seq": seq, "question_count": count},
            ) or task
            if count >= 2 and agent is not None and agent.supports(proto.MODE_BRIDGE):
                await self.announce(task, speech.escalation_offer(name), KIND_QUESTION,
                                    escalate_agent_id=agent.id)
            else:
                await self.announce(task, speech.agent_question(name, question), KIND_QUESTION)
            return
        await self._record_result(task, state, body.get("result") or {}, error=body.get("error"))

    async def _record_result(self, task: dict, state: str, result: dict, *, late: bool = False,
                             error: Any = None) -> None:
        name = self._name_of(task)
        say = proto.clip(result.get("say") if isinstance(result, dict) else "", proto.MAX_SAY_CHARS)
        output = result.get("output") if isinstance(result, dict) else None
        err = proto.clip(error or (result.get("error") if isinstance(result, dict) else "") or "", 300)
        db_state = proto.WIRE_TO_DB.get(state, proto.DB_FAILED)
        user_cancelled = task["status"] == proto.DB_CANCELLED and not late
        task = await self._update(
            task["task_id"], status=db_state, question=None, finished_at=utcnow(),
            delivered_at=None, result={"say": say, "output": output, "error": err},
        ) or task
        await self._cancel_deadline(task)
        if db_state == proto.DB_COMPLETED:
            line = speech.late_result(name, say) if late else speech.result(name, say)
            await self.announce(task, line, KIND_RESULT, terminal=True)
        elif db_state == proto.DB_FAILED:
            await self.announce(task, speech.failure(name, err), KIND_FAILURE, terminal=True)
        elif db_state == proto.DB_CANCELLED and not user_cancelled:
            await self.announce(task, speech.agent_cancelled(name, what(task)), KIND_INFO, terminal=True)

    # ----- delivery ---------------------------------------------------------

    async def announce(self, task: dict, line: str, kind: str, *, terminal: bool = False,
                       escalate_agent_id: str = "", offer: str = "") -> None:
        """Tell the user `line` about `task` now if possible, else per `notify`.

        `silent` tasks are recorded only: their results are never announced
        (the user hears them by asking). Questions an agent asks are still
        relayed, since the agent is waiting on the user.
        """
        notify = task.get("notify") or "next_session"
        if notify == "silent" and kind != KIND_QUESTION:
            return
        ann = Announcement(
            task_id=task["task_id"], user_id=task["user_id"], agent_name=self._name_of(task),
            line=line, kind=kind, terminal=terminal, escalate_agent_id=escalate_agent_id, offer=offer,
        )
        for listener in list(self._listeners.get(task["user_id"], [])):
            try:
                if await listener(ann):
                    return
            except Exception:
                log.exception("listener failed task_id=%s", task["task_id"])
        if notify == "silent":
            return
        if not terminal:
            await self._update(task["task_id"], info={"pending_announcement": line})
        if notify == "device":
            await self._maybe_wake(task["user_id"], task["task_id"])

    async def _maybe_wake(self, user_id: str, task_id: str) -> None:
        tz = await asyncio.to_thread(self.store.user_timezone, user_id)
        wakes = await asyncio.to_thread(self.store.wakes_last_hour, user_id)
        if not policy.wake_allowed(tz, wakes):
            log.info("wake suppressed user_id=%s (quiet hours or %d wakes this hour)", user_id, wakes)
            return
        if await asyncio.to_thread(self.store.pending_wake_exists, user_id):
            return  # several results finishing together share one wake
        await asyncio.to_thread(self.store.insert_job, "task_result", {"user_id": user_id, "task_id": task_id})
        log.info("wake queued user_id=%s task_id=%s", user_id, task_id)

    async def defer(self, ann: Announcement) -> None:
        """A live session couldn't speak `ann` (it closed during a live call):
        keep non-terminal lines for the next session. Terminal results stay
        undelivered and are announced from the task row anyway."""
        if not ann.terminal:
            await self._update(ann.task_id, info={"pending_announcement": ann.line})

    async def mark_delivered(self, task_id: str, via: str) -> None:
        """Called once the user has heard an announcement about `task_id`."""
        task = await self._task(task_id)
        if task is None:
            return
        await asyncio.to_thread(self.store.drop_info_keys, task_id, "pending_announcement")
        if task["status"] not in proto.DB_TERMINAL or task.get("delivered_at"):
            return
        now = utcnow()
        await self._update(task_id, delivered_at=now, delivered_via=via)
        await self._send(task, proto.TASK_DELIVERED, {"via": via, "at": now.isoformat()})

    def _line_for(self, task: dict) -> str:
        info = task.get("task_info") or {}
        if info.get("pending_announcement"):
            return str(info["pending_announcement"])
        name, res = self._name_of(task), task.get("result") or {}
        if task["status"] == proto.DB_COMPLETED:
            return speech.result(name, res.get("say") or "")
        if task["status"] == proto.DB_FAILED:
            return speech.failure(name, res.get("error") or "")
        if task["status"] == proto.DB_TIMED_OUT:
            return speech.timed_out(name, what(task), "the deadline")
        return speech.status_line(name, what(task), task["status"], task.get("question") or "")

    async def session_start(self, user_id: str) -> list[tuple[str, str]]:
        """(task_id, line) pairs to announce when a session opens, marked delivered."""
        rows = await asyncio.to_thread(self.store.undelivered, user_id)
        woke = await asyncio.to_thread(self.store.recent_wake, user_id)
        out: list[tuple[str, str]] = []
        for task in rows[:SESSION_START_MAX]:
            out.append((task["task_id"], self._line_for(task)))
            via = proto.DELIVERED_DEVICE_WAKE if woke else proto.DELIVERED_NEXT_SESSION
            await self.mark_delivered(task["task_id"], via)
        if len(rows) > SESSION_START_MAX:
            out.append(("", speech.more_updates(len(rows) - SESSION_START_MAX)))
        return out

    # ----- task management --------------------------------------------------

    async def get(self, task_id: str) -> Optional[dict]:
        return await self._task(task_id)

    async def list(self, user_id: str, *, active_only: bool = False, limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(lambda: self.store.list_tasks(user_id, active_only=active_only, limit=limit))

    def status_line(self, task: dict) -> str:
        info = task.get("task_info") or {}
        name = self._name_of(task)
        if info.get("stalled_since") and task["status"] in proto.DB_ACTIVE:
            return speech.stalled(name, what(task))
        return speech.status_line(name, what(task), task["status"], task.get("question") or "")

    async def update(self, task: dict, changes: dict) -> str:
        name = self._name_of(task)
        if task["status"] in proto.DB_TERMINAL:
            return speech.already_finished(name, what(task))
        info = task.get("task_info") or {}
        slots = {**(info.get("slots") or {}), **(changes or {})}
        agent = self._agent_of(task)
        if agent is not None and agent.supports_op("update"):
            task = await self._update(task["task_id"], info={"slots": slots}) or task
            await self._send(task, proto.TASK_UPDATE, {"changes": changes})
            return speech.updated(name)
        return await self._recreate(task, changes)

    def can_update_in_place(self, task: dict) -> bool:
        agent = self._agent_of(task)
        return agent is not None and agent.supports_op("update")

    async def _recreate(self, task: dict, changes: dict) -> str:
        """Fallback for agents that can't update: cancel, then re-dispatch with the change."""
        name = self._name_of(task)
        info = task.get("task_info") or {}
        slots = {**(info.get("slots") or {}), **(changes or {})}
        agent = self._agent_of(task)
        await self.cancel(task, quiet=True)
        if agent is None:
            return speech.unreachable(name)
        out = await self.dispatch(
            user_id=task["user_id"], agent=agent, intent=info.get("intent") or "", slots=slots,
            notify=task.get("notify") or "next_session", deadline_at=task.get("deadline_at"),
            drop_at_deadline=info.get("on_deadline") == "cancel", context_id=info.get("contextId"),
        )
        return speech.updated(name) if out.ok else speech.BUSY

    async def cancel(self, task: dict, *, reason: str = proto.CANCEL_USER, quiet: bool = False) -> str:
        name = self._name_of(task)
        if task["status"] not in proto.DB_TERMINAL:
            task = await self._update(task["task_id"], status=proto.DB_CANCELLED, finished_at=utcnow(),
                                      delivered_at=utcnow(), delivered_via=proto.DELIVERED_LIVE) or task
            await self._cancel_deadline(task)
            await asyncio.to_thread(self.store.outbox_drop_pending, task["task_id"], (proto.TASK_UPDATE, proto.TASK_INPUT))
            await self._send(task, proto.TASK_CANCEL, {"reason": reason})
        return "" if quiet else speech.cancelled(name, what(task))

    async def complete(self, task: dict) -> str:
        if task["status"] not in proto.DB_TERMINAL:
            task = await self._update(task["task_id"], status=proto.DB_COMPLETED, finished_at=utcnow(),
                                      delivered_at=utcnow(), delivered_via=proto.DELIVERED_LIVE) or task
            await self._cancel_deadline(task)
        await self._send(task, proto.TASK_CLOSE, {"reason": proto.CLOSE_COMPLETED_BY_USER, "by": "user"})
        return speech.completed_by_user(what(task))

    async def delete(self, task: dict) -> str:
        if task["status"] not in proto.DB_TERMINAL:
            await self.cancel(task, quiet=True)
        task = await self._update(task["task_id"], info={"deleted": True}) or task
        await self._send(task, proto.TASK_CLOSE, {"reason": proto.CLOSE_DELETED, "by": "user"})
        if await asyncio.to_thread(self.store.outbox_open_count, task["task_id"]) == 0:
            await asyncio.to_thread(self.store.delete_task, task["task_id"])
        return speech.deleted(what(task))

    async def answer(self, task: dict, answer: str) -> str:
        name = self._name_of(task)
        info = task.get("task_info") or {}
        pending = info.get("pending_input")
        if task["status"] == proto.DB_PENDING and pending:
            fields = pending.get("fields") or []
            slots = dict(info.get("slots") or {})
            if fields:
                slots[str(fields[0])] = answer
            task = await self._update(task["task_id"], info={"slots": slots, "pending_input": None}) or task
            agent = self._agent_of(task)
            if agent is not None:
                await self._redispatch(task, agent)
            return speech.answer_sent(name)
        if task["status"] != proto.DB_INPUT_REQUIRED:
            return speech.not_waiting(name)
        task = await self._update(task["task_id"], status=proto.DB_RUNNING, question=None) or task
        await asyncio.to_thread(self.store.drop_info_keys, task["task_id"], "pending_announcement")
        await self._send(task, proto.TASK_INPUT, {"answer": answer, "question_seq": info.get("question_seq")})
        return speech.answer_sent(name)

    async def retry_without_deadline(self, task: dict) -> str:
        """The user accepted retrying a task the agent couldn't finish by the deadline."""
        agent = self._agent_of(task)
        if agent is None:
            return speech.unreachable(self._name_of(task))
        task = await self._update(task["task_id"], deadline_at=None, info={"deadline_offer": None}) or task
        await self._redispatch(task, agent)
        return speech.dispatched(agent.name, will_tell=task.get("notify") != "silent")

    async def probe(self, task: dict) -> bool:
        """One `task.status` attempt for a task whose agent looks dead (never scheduled)."""
        agent = self._agent_of(task)
        if agent is None or not agent.supports_op("status"):
            return False
        await self._send(task, proto.TASK_STATUS, {})
        return True

    async def _mark_lost(self, task: dict) -> None:
        task = await self._update(task["task_id"], status=proto.DB_FAILED, finished_at=utcnow(),
                                  result={"error": "agent lost track of this task"}) or task
        await self._cancel_deadline(task)
        await self._send(task, proto.TASK_CLOSE, {"reason": proto.CLOSE_AGENT_LOST, "by": "orchestrator"})
        await self.announce(task, speech.lost(self._name_of(task), what(task)), KIND_FAILURE, terminal=True)

    async def escalate(self, task: dict) -> None:
        """The user accepted a live call instead: cancel the task (escalated)."""
        await self.cancel(task, reason=proto.CANCEL_ESCALATED, quiet=True)

    # ----- live → task hand-off ---------------------------------------------

    async def adopt(self, agent: AgentRecord, user_id: str, context_id: Optional[str], body: dict) -> dict:
        """An agent handed off work during a live call (`task.created`)."""
        if not (body.get("intent") or "").strip():
            raise TaskError(proto.NACK_INVALID_INPUT, "intent is required")
        if await asyncio.to_thread(self.store.count_active, user_id) >= self._max_active:
            raise TaskError(proto.NACK_BUSY, "the user already has too many tasks running")
        token = proto.new_callback_token()
        info = {
            "intent": str(body["intent"]).strip(), "slots": body.get("slots") or {},
            "contextId": context_id or proto.new_context_id(), "agent_name": agent.name,
            "agent_task_ref": body.get("agent_task_ref"), "on_deadline": "notify",
            "callback_token_sha256": hash_token(token), "named": True, "chain": [],
            "question_count": 0, "last_seq": 0,
        }
        task = await asyncio.to_thread(
            lambda: self.store.create_agent_task(
                task_id=proto.new_task_id(), user_id=user_id, agent_id=agent.id, task_info=info,
                notify="next_session", created_by="agent", status=proto.DB_RUNNING,
            )
        )
        return {"task_id": task["task_id"], "callback": {"url": self.callback_url(task["task_id"]), "token": token}}

    # ----- deadlines ----------------------------------------------------------

    async def _schedule_deadline(self, task: dict) -> None:
        deadline = parse_ts(task.get("deadline_at"))
        if deadline is None or (task.get("task_info") or {}).get("deadline_job_id"):
            return
        info = task.get("task_info") or {}
        payload = {
            "task_id": task["task_id"], "user_id": task["user_id"], "agent_id": task.get("agent_id"),
            "agent_name": self._name_of(task), "contextId": info.get("contextId"),
            "intent": info.get("intent"), "slots": info.get("slots"), "notify": task.get("notify"),
            "on_deadline": info.get("on_deadline"), "deadline_at": deadline.isoformat(),
        }
        job_id = await asyncio.to_thread(self.store.insert_job, "agent_task_deadline", payload, max(deadline, utcnow()))
        await self._update(task["task_id"], info={"deadline_job_id": job_id})

    async def _cancel_deadline(self, task: dict) -> None:
        job_id = (task.get("task_info") or {}).get("deadline_job_id")
        if job_id:
            await asyncio.to_thread(self.store.cancel_job, job_id)
            await asyncio.to_thread(self.store.drop_info_keys, task["task_id"], "deadline_job_id")

    async def set_deadline(self, task: dict, deadline_at: Optional[datetime], *, drop: Optional[bool] = None) -> None:
        await self._cancel_deadline(task)
        info: dict[str, Any] = {}
        if drop is not None:
            info["on_deadline"] = "cancel" if drop else "notify"
        task = await self._update(task["task_id"], deadline_at=deadline_at, info=info) or task
        if task["status"] in (proto.DB_RUNNING, proto.DB_INPUT_REQUIRED):
            await self._schedule_deadline(task)

    async def on_deadline(self, task_id: str, payload: dict) -> str:
        task = await self._task(task_id)
        if task is None or task["status"] in proto.DB_TERMINAL:
            return "stale"
        deadline = parse_ts(task.get("deadline_at"))
        if deadline is None or (payload.get("deadline_at") and parse_ts(payload["deadline_at"]) != deadline):
            return "stale"  # rescheduled or removed since the job was queued
        await asyncio.to_thread(self.store.drop_info_keys, task_id, "deadline_job_id")
        tz = await asyncio.to_thread(self.store.user_timezone, task["user_id"])
        name, due = self._name_of(task), spoken_time(deadline, tz)
        if (task.get("task_info") or {}).get("on_deadline") == "cancel":
            task = await self._update(task_id, status=proto.DB_TIMED_OUT, finished_at=utcnow(),
                                      result={"error": f"didn't finish by {due}"}) or task
            await self._send(task, proto.TASK_CANCEL, {"reason": proto.CANCEL_DEADLINE})
            await self.announce(task, speech.timed_out(name, what(task), due), KIND_INFO, terminal=True)
            return "timed_out"
        task = await self._update(task_id, info={"overdue_since": utcnow().isoformat()}) or task
        await self.announce(task, speech.overdue(name, what(task), due), KIND_QUESTION, offer="cancel")
        return "overdue"

    # ----- liveness ---------------------------------------------------------

    async def reconcile(self, agent_id: str, open_task_ids: Sequence[str]) -> int:
        """Heartbeat: tasks we think are open but the agent doesn't list were lost."""
        listed = {str(t) for t in open_task_ids}
        lost = 0
        now = utcnow()
        for task in await asyncio.to_thread(self.store.open_tasks_for_agent, agent_id):
            info = task.get("task_info") or {}
            if info.get("stalled_since"):
                await asyncio.to_thread(self.store.drop_info_keys, task["task_id"], "stalled_since")
            if task["task_id"] in listed or task["status"] == proto.DB_PENDING:
                continue
            created = parse_ts(task.get("created_at")) or now
            if (now - created).total_seconds() < RECONCILE_GRACE_S:
                continue
            lost += 1
            await self._mark_lost(task)
        return lost

    async def stall_check(self) -> int:
        flagged = 0
        now = utcnow()
        for row in await asyncio.to_thread(self.store.agents_with_open_tasks):
            last_seen = parse_ts(row.get("last_seen"))
            for task in await asyncio.to_thread(self.store.open_tasks_for_agent, row["agent_id"]):
                info = task.get("task_info") or {}
                ref = last_seen or parse_ts(task.get("created_at")) or now
                if info.get("stalled_since") or (now - ref).total_seconds() < STALL_AFTER_S:
                    continue
                flagged += 1
                task = await self._update(task["task_id"], info={"stalled_since": now.isoformat()}) or task
                await self.announce(task, speech.stalled(self._name_of(task), what(task)), KIND_INFO)
        return flagged

    async def _stall_forever(self) -> None:
        while True:
            await asyncio.sleep(STALL_CHECK_S)
            try:
                await self.stall_check()
            except Exception:
                log.exception("stall check failed")


_service: Optional[TaskService] = None


def get_service() -> TaskService:
    global _service
    if _service is None:
        _service = TaskService()
    return _service


def set_service(service: Optional[TaskService]) -> None:
    """Test hook."""
    global _service
    _service = service
