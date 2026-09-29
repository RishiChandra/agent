"""Task dispatcher: the orchestrator hands a unit of work to an agent and tracks it.

Before this, the orchestrator could only *route* — pick an agent and hand the
user's live microphone over to it (bridge mode). The dispatcher adds the other
half: the orchestrator sends an agent a structured task ("book a table for two
at 7"), keeps talking to the user while the agent works, and speaks the result
when it lands. Protocol: `agent_protocol.py` / `developer_ws/BRIDGE_PROTOCOL.md`
(task mode).

Design for 1000+ registered agents
----------------------------------
* **Routing** goes through `agent_router` (in-memory index, mode-aware, health
  aware). Only agents that declared `modes: ["task"]` are candidates.
* **Connection pool.** One multiplexed task-mode WebSocket per *active* agent,
  shared by every user's tasks (frames carry `task_id`). Connections are opened
  lazily, closed after `DISPATCH_IDLE_CLOSE_S` of idleness, and capped at
  `DISPATCH_MAX_CONNECTIONS` (idle LRU eviction). Registering 1000 agents costs
  nothing until they're used.
* **Backpressure, not errors.** A global in-flight semaphore, a per-agent
  semaphore sized from the agent's declared `max_concurrency`, a per-user cap
  on active tasks and a bounded submit queue. When a limit is hit, `submit`
  returns a clear reason (`busy`, `user_limit`) the voice layer can say out
  loud instead of raising.
* **Timeouts everywhere.** Connect, handshake, accept, and an overall task
  deadline (extended while the agent waits for the user's answer).
* **Failover.** If the user asked for an *outcome* (intent routing), a failed
  connect / retryable rejection falls through to the next-best capable agent.
  If the user *named* an agent, we don't silently substitute another one.
  Nothing is retried once an agent has accepted the task (no duplicate side
  effects); agents also receive an `idempotency_key` to dedupe on their side.
* **Circuit breaker.** Dial failures and protocol errors are reported to the
  router, which parks a failing agent for a cool-down.
* **Bounded memory.** Task records live in an LRU capped at
  `DISPATCH_MAX_RECORDS`; results for users with no live session are kept
  (bounded) and announced when they reconnect.

The module is FastAPI/Pipecat-free so it can be unit-tested against a local
WebSocket server (see test/app/developer/test_task_dispatcher.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import websockets

import agent_protocol as proto
from agent_router import (
    DECISION_AMBIGUOUS,
    DECISION_MATCHED,
    DECISION_NONE,
    DECISION_WRONG_MODE,
    MODE_TASK,
    AgentRecord,
    AgentRouter,
    RouteResult,
    get_router,
)

log = logging.getLogger("task_dispatcher")


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


CONNECT_TIMEOUT_S = _env_f("DISPATCH_CONNECT_TIMEOUT_S", 5.0)
HANDSHAKE_TIMEOUT_S = _env_f("DISPATCH_HANDSHAKE_TIMEOUT_S", 5.0)
ACCEPT_TIMEOUT_S = _env_f("DISPATCH_ACCEPT_TIMEOUT_S", 5.0)
DEFAULT_DEADLINE_S = _env_f("DISPATCH_DEFAULT_DEADLINE_S", 120.0)
MAX_DEADLINE_S = _env_f("DISPATCH_MAX_DEADLINE_S", 3600.0)
INPUT_WAIT_S = _env_f("DISPATCH_INPUT_WAIT_S", 120.0)
IDLE_CLOSE_S = _env_f("DISPATCH_IDLE_CLOSE_S", 60.0)
MAX_CONNECTIONS = int(_env_f("DISPATCH_MAX_CONNECTIONS", 200))
MAX_INFLIGHT = int(_env_f("DISPATCH_MAX_INFLIGHT", 256))
MAX_QUEUED = int(_env_f("DISPATCH_MAX_QUEUED", 1024))
MAX_ACTIVE_PER_USER = int(_env_f("DISPATCH_MAX_ACTIVE_PER_USER", 8))
MAX_ATTEMPTS = int(_env_f("DISPATCH_MAX_ATTEMPTS", 3))
MAX_RECORDS = int(_env_f("DISPATCH_MAX_RECORDS", 5000))
MAX_UNDELIVERED_PER_USER = int(_env_f("DISPATCH_MAX_UNDELIVERED_PER_USER", 20))


# ---------------------------------------------------------------------------
# Task state
# ---------------------------------------------------------------------------

QUEUED = "queued"
DISPATCHING = "dispatching"
ACCEPTED = "accepted"
RUNNING = "running"
INPUT_REQUIRED = "input_required"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TIMED_OUT = "timed_out"

TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED, TIMED_OUT})

# submit() refusal reasons
REASON_AMBIGUOUS = "ambiguous"
REASON_NO_AGENT = "no_agent"
REASON_WRONG_MODE = "wrong_mode"
REASON_USER_LIMIT = "user_limit"
REASON_BUSY = "busy"
REASON_BAD_REQUEST = "bad_request"


@dataclass
class TaskRecord:
    task_id: str
    user_id: str
    intent: str
    input: dict = field(default_factory=dict)
    requested_agent: str = ""
    agent_id: str = ""
    agent_name: str = ""
    status: str = QUEUED
    attempts: int = 0
    tried_agents: list[str] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    question: str = ""
    say: str = ""
    output: Any = None
    error: str = ""
    deadline_s: float = DEFAULT_DEADLINE_S
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    source: str = "voice"

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def touch(self, status: Optional[str] = None) -> None:
        if status is not None:
            self.status = status
        self.updated_at = time.time()

    def spoken_summary(self) -> str:
        """One sentence the voice layer can say about this task's current state."""
        who = self.agent_name or "The agent"
        if self.status == SUCCEEDED:
            return self.say or f"{who} finished: {self.intent}."
        if self.status == FAILED:
            why = f" {self.error}" if self.error else ""
            return f"{who} couldn't complete that.{why}".strip()
        if self.status == TIMED_OUT:
            return f"{who} didn't finish in time, so I stopped waiting."
        if self.status == CANCELLED:
            return f"Cancelled the task with {who}."
        if self.status == INPUT_REQUIRED:
            return f"{who} needs something from you: {self.question}"
        if self.progress:
            return f"{who} is working on it: {self.progress[-1]}"
        return f"{who} is working on it."

    def to_public(self) -> dict:
        return {
            "task_id": self.task_id,
            "user_id": self.user_id,
            "intent": self.intent,
            "input": self.input,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "requested_agent": self.requested_agent,
            "status": self.status,
            "attempts": self.attempts,
            "progress": self.progress[-5:],
            "question": self.question,
            "say": self.say,
            "output": self.output,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "summary": self.spoken_summary(),
        }


@dataclass
class SubmitResult:
    ok: bool
    task: Optional[TaskRecord] = None
    reason: str = ""
    route: Optional[RouteResult] = None

    def to_public(self) -> dict:
        d: dict[str, Any] = {"ok": self.ok, "reason": self.reason}
        if self.task is not None:
            d["task"] = self.task.to_public()
        if self.route is not None:
            d["decision"] = self.route.decision
            d["candidates"] = [c.to_public() for c in self.route.candidates[:5]]
            if self.route.wrong_mode is not None:
                d["wrong_mode_agent"] = self.route.wrong_mode.to_public()
        return d


TaskListener = Callable[[TaskRecord], Awaitable[None]]


class _AttemptError(Exception):
    """One dispatch attempt failed. `retryable` → try the next candidate."""

    def __init__(self, reason: str, *, retryable: bool, health: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        # Whether this failure should count against the agent's circuit breaker.
        self.health = health


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

_DISCONNECTED = {"type": "_disconnected"}


class AgentConnection:
    """One task-mode WebSocket to one agent, multiplexed by `task_id`."""

    def __init__(self, agent: AgentRecord) -> None:
        self.agent = agent
        self.url = agent.url
        self._ws: Any = None
        self._recv_task: Optional[asyncio.Task] = None
        self._queues: dict[str, asyncio.Queue] = {}
        self._send_lock = asyncio.Lock()
        self.last_used = time.monotonic()
        self.closed = False
        self.service_id = ""
        self.max_concurrency = agent.max_concurrency

    @property
    def inflight(self) -> int:
        return len(self._queues)

    async def open(self) -> None:
        try:
            self._ws = await websockets.connect(
                self.url, max_size=proto.MAX_OUTPUT_BYTES * 4, open_timeout=CONNECT_TIMEOUT_S,
            )
        except Exception as e:
            self.closed = True
            raise _AttemptError(f"connect failed: {e}", retryable=True) from e
        try:
            await self._ws.send(proto.dumps(proto.hello("orchestrator", mode=proto.MODE_TASK)))
            raw = await asyncio.wait_for(self._ws.recv(), timeout=HANDSHAKE_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            await self._abort()
            raise _AttemptError("agent did not answer the handshake", retryable=True) from e
        except Exception as e:
            await self._abort()
            raise _AttemptError(f"handshake failed: {e}", retryable=True) from e
        ack = proto.parse(raw)
        if not ack or ack.get("type") != proto.ACK:
            await self._abort()
            raise _AttemptError("agent sent a malformed handshake", retryable=True)
        if not ack.get("accept", False):
            await self._abort()
            raise _AttemptError(
                f"agent declined: {ack.get('reason') or 'no reason'}", retryable=True, health=False,
            )
        if proto.MODE_TASK not in proto.ack_modes(ack):
            await self._abort()
            raise _AttemptError("agent does not support task mode", retryable=True)
        self.service_id = str(ack.get("service_id") or "")
        self.max_concurrency = min(self.agent.max_concurrency, proto.ack_max_concurrency(ack, self.agent.max_concurrency))
        self._recv_task = asyncio.create_task(self._recv_loop(), name=f"agent-recv-{self.agent.id}")
        log.info(
            "dispatch: connected agent=%s url=%s service_id=%s max_concurrency=%d",
            self.agent.name, self.url, self.service_id, self.max_concurrency,
        )

    def register(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._queues[task_id] = q
        self.last_used = time.monotonic()
        return q

    def release(self, task_id: str) -> None:
        self._queues.pop(task_id, None)
        self.last_used = time.monotonic()

    async def send(self, frame: dict) -> None:
        if self.closed or self._ws is None:
            raise _AttemptError("connection closed", retryable=True)
        try:
            async with self._send_lock:
                await self._ws.send(proto.dumps(frame))
        except Exception as e:
            await self._abort()
            raise _AttemptError(f"send failed: {e}", retryable=True) from e
        self.last_used = time.monotonic()

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = proto.parse(raw)
                if not msg:
                    continue
                mtype = msg.get("type")
                if mtype == proto.PING:
                    try:
                        await self.send({"type": proto.PONG, "ts": msg.get("ts")})
                    except _AttemptError:
                        break
                    continue
                if mtype == proto.BYE:
                    log.info("dispatch: agent=%s said bye reason=%s", self.agent.name, msg.get("reason"))
                    break
                if mtype not in proto.AGENT_TO_ORCH_TASK_FRAMES:
                    continue
                q = self._queues.get(str(msg.get("task_id") or ""))
                if q is None:
                    log.debug("dispatch: frame for unknown task agent=%s %r", self.agent.name, msg)
                    continue
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    # A chatty agent flooding progress frames: drop progress,
                    # never terminal frames.
                    if mtype != proto.TASK_PROGRESS:
                        await q.put(msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            log.exception("dispatch: recv loop error agent=%s", self.agent.name)
        finally:
            self.closed = True
            for q in list(self._queues.values()):
                try:
                    q.put_nowait(_DISCONNECTED)
                except asyncio.QueueFull:
                    pass

    async def _abort(self) -> None:
        self.closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def close(self, reason: str = "idle") -> None:
        if self.closed and self._ws is None:
            return
        self.closed = True
        if self._ws is not None:
            try:
                await asyncio.wait_for(
                    self._ws.send(proto.dumps({"type": proto.BYE, "reason": reason})), timeout=0.5,
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._ws.close(), timeout=1.0)
            except Exception:
                pass
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        self._ws = None


class ConnectionPool:
    """Lazily-opened, idle-reaped, capped set of `AgentConnection`s (one per agent)."""

    def __init__(self, *, max_connections: int = MAX_CONNECTIONS, idle_close_s: float = IDLE_CLOSE_S) -> None:
        self._conns: "OrderedDict[str, AgentConnection]" = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._max = max(1, max_connections)
        self._idle_close_s = idle_close_s
        self._reaper: Optional[asyncio.Task] = None

    def __len__(self) -> int:
        return len(self._conns)

    async def get(self, agent: AgentRecord) -> AgentConnection:
        self._ensure_reaper()
        async with self._locks[agent.id]:
            conn = self._conns.get(agent.id)
            if conn is not None and (conn.closed or conn.url != agent.url):
                # Dead, or the agent re-registered at a new URL (tunnel restart).
                self._conns.pop(agent.id, None)
                if conn.inflight == 0:
                    await conn.close("replaced")
                conn = None
            if conn is None:
                await self._make_room()
                conn = AgentConnection(agent)
                await conn.open()
                self._conns[agent.id] = conn
            self._conns.move_to_end(agent.id)
            return conn

    async def _make_room(self) -> None:
        while len(self._conns) >= self._max:
            victim_id = next((aid for aid, c in self._conns.items() if c.inflight == 0), None)
            if victim_id is None:
                # Every pooled connection is busy: allow a soft overflow rather
                # than failing the user's request.
                log.warning("dispatch: connection pool over cap (%d busy)", len(self._conns))
                return
            victim = self._conns.pop(victim_id)
            await victim.close("evicted")

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            try:
                self._reaper = asyncio.get_running_loop().create_task(self._reap_forever())
            except RuntimeError:
                self._reaper = None

    async def _reap_forever(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(1.0, self._idle_close_s / 2))
                await self.reap_idle()
        except asyncio.CancelledError:
            pass

    async def reap_idle(self) -> int:
        now = time.monotonic()
        stale = [
            aid for aid, c in self._conns.items()
            if c.closed or (c.inflight == 0 and now - c.last_used > self._idle_close_s)
        ]
        for aid in stale:
            conn = self._conns.pop(aid, None)
            if conn is not None:
                await conn.close("idle")
        return len(stale)

    async def close_all(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        conns, self._conns = list(self._conns.values()), OrderedDict()
        for c in conns:
            await c.close("shutdown")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TaskDispatcher:
    def __init__(
        self,
        router: Optional[AgentRouter] = None,
        *,
        pool: Optional[ConnectionPool] = None,
        max_inflight: int = MAX_INFLIGHT,
        max_queued: int = MAX_QUEUED,
        max_active_per_user: int = MAX_ACTIVE_PER_USER,
        max_attempts: int = MAX_ATTEMPTS,
        max_records: int = MAX_RECORDS,
        accept_timeout_s: float = ACCEPT_TIMEOUT_S,
        input_wait_s: float = INPUT_WAIT_S,
    ) -> None:
        self._router = router
        self._pool = pool if pool is not None else ConnectionPool()
        self._inflight = asyncio.Semaphore(max(1, max_inflight))
        self._max_queued = max_queued
        self._max_active_per_user = max_active_per_user
        self._max_attempts = max(1, max_attempts)
        self._max_records = max_records
        self._accept_timeout_s = accept_timeout_s
        self._input_wait_s = input_wait_s
        self._agent_sems: dict[str, tuple[int, asyncio.Semaphore]] = {}
        self._records: "OrderedDict[str, TaskRecord]" = OrderedDict()
        self._runners: dict[str, asyncio.Task] = {}
        self._conn_of: dict[str, AgentConnection] = {}
        self._listeners: dict[str, list[TaskListener]] = defaultdict(list)
        self._undelivered: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_UNDELIVERED_PER_USER))

    @property
    def router(self) -> AgentRouter:
        return self._router or get_router()

    # ----- listeners ----------------------------------------------------------

    def subscribe(self, user_id: str, listener: TaskListener) -> Callable[[], None]:
        """Receive every state change for `user_id`'s tasks. Returns an unsubscribe fn."""
        self._listeners[user_id].append(listener)

        def _unsub() -> None:
            try:
                self._listeners[user_id].remove(listener)
            except ValueError:
                pass
            if not self._listeners.get(user_id):
                self._listeners.pop(user_id, None)

        return _unsub

    def drain_undelivered(self, user_id: str) -> list[TaskRecord]:
        """Finished tasks whose result no live session heard (for a reconnect announcement)."""
        q = self._undelivered.pop(user_id, None)
        return list(q) if q else []

    def requeue_undelivered(self, rec: TaskRecord) -> None:
        """A listener couldn't deliver `rec` (session closing) — keep it for reconnect."""
        if rec.terminal and rec.status != CANCELLED:
            self._undelivered[rec.user_id].append(rec)

    async def _notify(self, rec: TaskRecord) -> None:
        listeners = list(self._listeners.get(rec.user_id, ()))
        if not listeners:
            if rec.terminal and rec.status != CANCELLED:
                self._undelivered[rec.user_id].append(rec)
            return
        for cb in listeners:
            try:
                await cb(rec)
            except Exception:
                log.exception("dispatch: listener failed task_id=%s", rec.task_id)

    # ----- queries --------------------------------------------------------------

    def get(self, task_id: str) -> Optional[TaskRecord]:
        return self._records.get(task_id)

    def list_for_user(self, user_id: str, *, active_only: bool = False, limit: int = 20) -> list[TaskRecord]:
        out = [
            r for r in reversed(self._records.values())
            if r.user_id == user_id and (not active_only or not r.terminal)
        ]
        return out[:limit]

    def latest_for_user(self, user_id: str, *, status: Optional[str] = None) -> Optional[TaskRecord]:
        for r in reversed(self._records.values()):
            if r.user_id != user_id:
                continue
            if status is None and not r.terminal:
                return r
            if status is not None and r.status == status:
                return r
        return None

    def stats(self) -> dict:
        by_status: dict[str, int] = defaultdict(int)
        for r in self._records.values():
            by_status[r.status] += 1
        return {
            "records": len(self._records),
            "active": len(self._runners),
            "connections": len(self._pool),
            "by_status": dict(by_status),
        }

    # ----- submit ----------------------------------------------------------------

    async def submit(
        self,
        *,
        user_id: str,
        intent: str,
        agent: str = "",
        input: Optional[dict] = None,
        deadline_s: Optional[float] = None,
        source: str = "voice",
    ) -> SubmitResult:
        """Route + enqueue a task. Never raises for routing/limit problems —
        returns `ok=False` with a machine-readable `reason` instead."""
        intent = (intent or "").strip()
        agent = (agent or "").strip()
        if not user_id or not intent:
            return SubmitResult(ok=False, reason=REASON_BAD_REQUEST)

        active = sum(1 for r in self._records.values() if r.user_id == user_id and not r.terminal)
        if active >= self._max_active_per_user:
            return SubmitResult(ok=False, reason=REASON_USER_LIMIT)
        if len(self._runners) >= self._max_queued:
            return SubmitResult(ok=False, reason=REASON_BUSY)

        router = self.router
        await router.ensure_fresh_async()
        route = router.resolve(agent, intent, mode=MODE_TASK, k=max(self._max_attempts, 5))

        if route.decision == DECISION_WRONG_MODE:
            return SubmitResult(ok=False, reason=REASON_WRONG_MODE, route=route)
        if route.decision == DECISION_NONE:
            return SubmitResult(ok=False, reason=REASON_NO_AGENT, route=route)
        if route.decision == DECISION_AMBIGUOUS and agent:
            # The user named an agent and we can't tell which one — ask, don't guess.
            return SubmitResult(ok=False, reason=REASON_AMBIGUOUS, route=route)

        if agent and route.decision == DECISION_MATCHED:
            # Named agent: use exactly that one — no silent substitution.
            chain = [route.candidates[0].agent]
        else:
            # Intent routed: best first, failover through the other capable ones,
            # healthy agents before parked ones.
            chain = [c.agent for c in route.candidates]
            chain.sort(key=lambda a: 0 if router.is_healthy(a.id) else 1)
            chain = chain[: self._max_attempts]

        deadline = DEFAULT_DEADLINE_S if deadline_s is None else float(deadline_s)
        deadline = max(1.0, min(MAX_DEADLINE_S, deadline))
        rec = TaskRecord(
            task_id=proto.new_id("task"),
            user_id=user_id,
            intent=intent,
            input=dict(input or {}),
            requested_agent=agent,
            agent_id=chain[0].id,
            agent_name=chain[0].name,
            deadline_s=deadline,
            source=source,
        )
        self._store(rec)
        self._runners[rec.task_id] = asyncio.create_task(
            self._run(rec, chain), name=f"task-{rec.task_id}"
        )
        log.info(
            "dispatch: submitted task_id=%s user_id=%s agent=%s chain=%s intent=%r",
            rec.task_id, user_id, rec.agent_name, [a.name for a in chain], intent,
        )
        return SubmitResult(ok=True, task=rec, route=route)

    def _store(self, rec: TaskRecord) -> None:
        self._records[rec.task_id] = rec
        while len(self._records) > self._max_records:
            # Evict the oldest *finished* record; never drop a live one.
            victim = next((tid for tid, r in self._records.items() if r.terminal), None)
            if victim is None:
                break
            self._records.pop(victim, None)

    def _agent_sem(self, agent: AgentRecord, limit: int) -> asyncio.Semaphore:
        cur = self._agent_sems.get(agent.id)
        if cur is None or cur[0] != limit:
            cur = (limit, asyncio.Semaphore(limit))
            self._agent_sems[agent.id] = cur
        return cur[1]

    # ----- execution -------------------------------------------------------------

    async def _run(self, rec: TaskRecord, chain: list[AgentRecord]) -> None:
        router = self.router
        loop = asyncio.get_running_loop()
        deadline_at = loop.time() + rec.deadline_s
        try:
            async with self._inflight:
                last_error = "no agent available"
                for agent in chain:
                    if rec.terminal:
                        return
                    rec.attempts += 1
                    rec.agent_id, rec.agent_name = agent.id, agent.name
                    rec.tried_agents.append(agent.name)
                    rec.touch(DISPATCHING)
                    try:
                        await self._attempt(rec, agent, deadline_at)
                        router.report_success(agent.id)
                        return
                    except _AttemptError as e:
                        last_error = e.reason
                        if e.health:
                            router.report_failure(agent.id, e.reason)
                        log.info(
                            "dispatch: attempt failed task_id=%s agent=%s retryable=%s reason=%s",
                            rec.task_id, agent.name, e.retryable, e.reason,
                        )
                        if not e.retryable or loop.time() >= deadline_at:
                            break
                if not rec.terminal:
                    if loop.time() >= deadline_at:
                        rec.touch(TIMED_OUT)
                    else:
                        rec.error = last_error
                        rec.touch(FAILED)
                    await self._notify(rec)
        except asyncio.CancelledError:
            if not rec.terminal:
                rec.touch(CANCELLED)
                await self._notify(rec)
            raise
        except Exception as e:  # defensive: a bug here must not leak a live task
            log.exception("dispatch: runner crashed task_id=%s", rec.task_id)
            if not rec.terminal:
                rec.error = f"internal error: {e}"
                rec.touch(FAILED)
                await self._notify(rec)
        finally:
            self._runners.pop(rec.task_id, None)
            self._conn_of.pop(rec.task_id, None)

    async def _attempt(self, rec: TaskRecord, agent: AgentRecord, deadline_at: float) -> None:
        loop = asyncio.get_running_loop()
        conn = await self._pool.get(agent)
        sem = self._agent_sem(agent, conn.max_concurrency)
        # Wait for an agent slot, but never past the task deadline.
        try:
            await asyncio.wait_for(sem.acquire(), timeout=max(0.01, deadline_at - loop.time()))
        except asyncio.TimeoutError as e:
            raise _AttemptError("agent is at capacity", retryable=True, health=False) from e
        q = conn.register(rec.task_id)
        self._conn_of[rec.task_id] = conn
        accepted = False
        try:
            remaining_ms = int(max(0.0, deadline_at - loop.time()) * 1000)
            await conn.send(proto.task_dispatch(
                task_id=rec.task_id, user_id=rec.user_id, intent=rec.intent,
                input=rec.input, deadline_ms=remaining_ms, attempt=rec.attempts,
            ))
            # Absolute, so waking for a frame doesn't restart the accept window.
            accept_by = loop.time() + self._accept_timeout_s
            while True:
                if accepted:
                    timeout = deadline_at - loop.time()
                else:
                    timeout = min(accept_by, deadline_at) - loop.time()
                if timeout <= 0:
                    if accepted:
                        await self._send_cancel(conn, rec.task_id, "deadline")
                        rec.touch(TIMED_OUT)
                        await self._notify(rec)
                        return
                    # The agent has the dispatch; make sure a late accept doesn't
                    # run it alongside the agent we fail over to.
                    await self._send_cancel(conn, rec.task_id, "accept_timeout")
                    raise _AttemptError("agent did not accept the task in time", retryable=True)
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue  # loop re-evaluates which deadline expired

                mtype = msg.get("type")
                if msg is _DISCONNECTED or mtype == "_disconnected":
                    if accepted:
                        # The agent may have done part of the work — don't
                        # replay it elsewhere.
                        raise _AttemptError("agent disconnected mid-task", retryable=False)
                    raise _AttemptError("agent disconnected", retryable=True)
                if mtype == proto.TASK_ACCEPTED:
                    accepted = True
                    rec.touch(ACCEPTED)
                    await self._notify(rec)
                elif mtype == proto.TASK_REJECTED:
                    reason = proto.clip_text(msg.get("reason") or "rejected", 300)
                    raise _AttemptError(
                        f"agent rejected the task: {reason}",
                        retryable=bool(msg.get("retryable", True)), health=False,
                    )
                elif mtype == proto.TASK_PROGRESS:
                    accepted = True
                    text = proto.clip_text(msg.get("message") or "", 300)
                    if text:
                        rec.progress.append(text)
                        del rec.progress[:-20]
                    rec.touch(RUNNING)
                    await self._notify(rec)
                elif mtype == proto.TASK_INPUT_REQUIRED:
                    accepted = True
                    rec.question = proto.clip_text(msg.get("question") or "", 500)
                    rec.touch(INPUT_REQUIRED)
                    # Waiting on a human: give them time to answer.
                    deadline_at = max(deadline_at, loop.time() + self._input_wait_s)
                    await self._notify(rec)
                elif mtype == proto.TASK_RESULT:
                    status = str(msg.get("status") or proto.RESULT_SUCCEEDED)
                    rec.say = proto.clip_text(msg.get("say") or "")
                    rec.output = msg.get("output")
                    if status == proto.RESULT_SUCCEEDED:
                        rec.touch(SUCCEEDED)
                    else:
                        rec.error = proto.clip_text(msg.get("error") or "", 300)
                        rec.touch(FAILED)
                    await self._notify(rec)
                    return
        finally:
            conn.release(rec.task_id)
            sem.release()

    async def _send_cancel(self, conn: Optional[AgentConnection], task_id: str, reason: str) -> None:
        if conn is None or conn.closed:
            return
        try:
            await conn.send(proto.task_cancel(task_id, reason))
        except _AttemptError:
            pass

    # ----- control ---------------------------------------------------------------

    async def cancel(self, task_id: str, reason: str = "user_cancelled") -> Optional[TaskRecord]:
        rec = self._records.get(task_id)
        if rec is None or rec.terminal:
            return rec
        await self._send_cancel(self._conn_of.get(task_id), task_id, reason)
        runner = self._runners.get(task_id)
        rec.touch(CANCELLED)
        if runner is not None and not runner.done():
            runner.cancel()
            try:
                await runner
            except (asyncio.CancelledError, Exception):
                pass
        await self._notify(rec)
        return rec

    async def provide_input(self, task_id: str, answer: str) -> bool:
        rec = self._records.get(task_id)
        conn = self._conn_of.get(task_id)
        if rec is None or rec.status != INPUT_REQUIRED or conn is None:
            return False
        try:
            await conn.send(proto.task_input(task_id, answer))
        except _AttemptError:
            return False
        rec.question = ""
        rec.touch(RUNNING)
        return True

    async def shutdown(self) -> None:
        for tid in list(self._runners):
            await self.cancel(tid, "orchestrator_shutdown")
        await self._pool.close_all()


_dispatcher: Optional[TaskDispatcher] = None


def get_dispatcher() -> TaskDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = TaskDispatcher()
    return _dispatcher


def set_dispatcher(d: Optional[TaskDispatcher]) -> None:
    """Test hook."""
    global _dispatcher
    _dispatcher = d
