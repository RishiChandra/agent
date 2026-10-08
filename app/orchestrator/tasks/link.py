"""Connections to agents and outbox delivery (ORCHESTRATOR_V2_TOOL_CALLS.md §1.7).

* **Outbox first.** `send()` writes the request to `agent_outbox`, then
  delivers it. Rows are resent with the same `msg_id` until the agent replies
  (`task.ack` / `task.nack`). Per task, a row is only sent once the previous
  row has a reply, so an update can't overtake its dispatch.
* **Connections on demand** (decision B2). At most one shared WebSocket per
  agent, opened when something needs sending, reused for any user, closed
  after `DISPATCH_IDLE_CLOSE_S` (60 s) idle. Results don't need it: agents push
  events to the HTTP callback.
* **Retries.** First resend after the agent's `max_reply_latency_s`, then
  exponential backoff up to 5 min. A dispatch nobody acks after 3 sends is
  handed back to the service (fail over or tell the user). Other requests stay
  queued for `DISPATCH_OUTBOX_TTL_H` (24 h).
* A sweep every `DISPATCH_OUTBOX_SWEEP_S` (30 s) retries due rows. It only
  resends the orchestrator's own messages; it never asks agents for state.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict, defaultdict
from typing import Any, Awaitable, Callable, Optional

import websockets

from orchestrator.tasks import protocol as proto
from orchestrator.routing.router import AgentRecord, AgentRouter

log = logging.getLogger("agent_link")


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


CONNECT_TIMEOUT_S = _env_f("DISPATCH_CONNECT_TIMEOUT_S", 5.0)
HANDSHAKE_TIMEOUT_S = _env_f("DISPATCH_HANDSHAKE_TIMEOUT_S", 5.0)
IDLE_CLOSE_S = _env_f("DISPATCH_IDLE_CLOSE_S", 60.0)
MAX_CONNECTIONS = int(_env_f("DISPATCH_MAX_CONNECTIONS", 200))
SWEEP_S = _env_f("DISPATCH_OUTBOX_SWEEP_S", 30.0)
TTL_H = _env_f("DISPATCH_OUTBOX_TTL_H", 24.0)
DISPATCH_MAX_SENDS = int(_env_f("DISPATCH_MAX_SENDS", 3))
RETRY_BASE_S = _env_f("DISPATCH_RETRY_BASE_S", 5.0)
MAX_BACKOFF_S = 300.0

OnReply = Callable[[AgentRecord, dict, dict], Awaitable[None]]       # (agent, outbox row, reply)
OnEvent = Callable[[AgentRecord, dict], Awaitable[Optional[int]]]     # -> ack_seq
OnCreated = Callable[[AgentRecord, dict], Awaitable[dict]]            # -> reply message
OnExpired = Callable[[AgentRecord, dict], Awaitable[None]]            # (agent, outbox row)


class LinkError(Exception):
    pass


def backoff_s(attempts: int, first: float) -> float:
    """Wait before resend number `attempts`: the agent's reply budget, then 5 s doubling to 5 min."""
    if attempts <= 1:
        return first
    return min(MAX_BACKOFF_S, max(first, RETRY_BASE_S) * (2 ** (attempts - 2)))


class AgentConnection:
    """One task-mode WebSocket to one agent; carries any user's messages."""

    def __init__(self, agent: AgentRecord, link: "AgentLink") -> None:
        self.agent = agent
        self.url = proto.task_url(agent.url)
        self._link = link
        self._ws: Any = None
        self._recv_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        self.last_used = time.monotonic()
        self.closed = False
        self.ack: dict = {}

    async def open(self) -> None:
        try:
            self._ws = await websockets.connect(
                self.url, max_size=proto.MAX_OUTPUT_BYTES * 4, open_timeout=CONNECT_TIMEOUT_S,
            )
        except Exception as e:
            self.closed = True
            raise LinkError(f"connect failed: {e}") from e
        try:
            await self._ws.send(proto.dumps(proto.hello(proto.TASK_USER_ID, mode=proto.MODE_TASK)))
            raw = await asyncio.wait_for(self._ws.recv(), timeout=HANDSHAKE_TIMEOUT_S)
        except Exception as e:
            await self._abort()
            raise LinkError(f"handshake failed: {e}") from e
        ack = proto.parse(raw) or {}
        if ack.get("type") != proto.ACK or not ack.get("accept", False):
            await self._abort()
            raise LinkError(f"agent declined: {ack.get('reason') or ack}")
        if proto.MODE_TASK not in proto.ack_modes(ack):
            await self._abort()
            raise LinkError("agent does not support task mode")
        self.ack = ack
        self._recv_task = asyncio.create_task(self._recv_loop(), name=f"agent-recv-{self.agent.id}")
        log.info("link: connected agent=%s url=%s", self.agent.name, self.url)

    async def send(self, msg: dict) -> None:
        if self.closed or self._ws is None:
            raise LinkError("connection closed")
        try:
            async with self._send_lock:
                await self._ws.send(proto.dumps(msg))
        except Exception as e:
            await self._abort()
            raise LinkError(f"send failed: {e}") from e
        self.last_used = time.monotonic()

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                self.last_used = time.monotonic()
                msg = proto.parse(raw)
                if not msg:
                    continue
                mtype = msg.get("type")
                if mtype == proto.PING:
                    await self.send({"type": proto.PONG, "ts": msg.get("ts")})
                elif mtype == proto.BYE:
                    break
                elif mtype in (proto.TASK_ACK, proto.TASK_NACK):
                    await self._link.handle_reply(self.agent, msg)
                elif mtype == proto.TASK_EVENT:
                    seq = await self._link.handle_event(self.agent, msg)
                    if seq is not None:
                        await self.send(proto.envelope(
                            proto.TASK_EVENT_ACK, task_id=msg.get("task_id"),
                            reply_to=msg.get("msg_id"), body={"ack_seq": seq},
                        ))
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            log.exception("link: recv loop error agent=%s", self.agent.name)
        finally:
            self.closed = True

    async def _abort(self) -> None:
        self.closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def close(self, reason: str = "idle") -> None:
        self.closed = True
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.send(proto.dumps({"type": proto.BYE, "reason": reason})), 0.5)
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._ws.close(), 1.0)
            except Exception:
                pass
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        self._ws = None


class AgentLink:
    def __init__(
        self,
        store,
        router: AgentRouter,
        *,
        on_reply: Optional[OnReply] = None,
        on_event: Optional[OnEvent] = None,
        on_expired: Optional[OnExpired] = None,
        max_connections: int = MAX_CONNECTIONS,
        idle_close_s: float = IDLE_CLOSE_S,
        sweep_s: float = SWEEP_S,
    ) -> None:
        self._store = store
        self._router = router
        self.on_reply = on_reply
        self.on_event = on_event
        self.on_expired = on_expired
        self._conns: "OrderedDict[str, AgentConnection]" = OrderedDict()
        self._conn_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._deliver_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._max = max(1, max_connections)
        self._idle_close_s = idle_close_s
        self._sweep_s = sweep_s
        self._bg: list[asyncio.Task] = []

    # ----- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if not self._bg:
            self._bg.append(loop.create_task(self._sweep_forever(), name="outbox-sweep"))
            self._bg.append(loop.create_task(self._reap_forever(), name="link-reaper"))

    async def stop(self) -> None:
        for t in self._bg:
            t.cancel()
        for t in self._bg:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._bg.clear()
        conns, self._conns = list(self._conns.values()), OrderedDict()
        for c in conns:
            await c.close("shutdown")

    def connection_count(self) -> int:
        return len(self._conns)

    # ----- sending --------------------------------------------------------------

    async def send(self, agent: AgentRecord, task_id: str, msg: dict) -> dict:
        """Queue `msg` in the outbox and try to deliver it now."""
        row = await asyncio.to_thread(self._store.outbox_add, agent_id=agent.id, task_id=task_id, envelope=msg)
        self.kick(agent.id)
        return row

    def kick(self, agent_id: str) -> None:
        try:
            asyncio.get_running_loop().create_task(self.deliver(agent_id))
        except RuntimeError:
            pass

    async def deliver(self, agent_id: str) -> int:
        """Send every due head-of-line row for one agent. Returns rows sent."""
        agent = self._router.get(agent_id)
        if agent is None:
            await asyncio.to_thread(self._router.load_now)
            agent = self._router.get(agent_id)
        if agent is None or agent.binding != "ws":
            return 0
        async with self._deliver_locks[agent_id]:
            heads = await asyncio.to_thread(self._store.outbox_heads, agent_id=agent_id, due_only=True)
            if not heads:
                return 0
            sent = 0
            for row in heads:
                if row["type"] == proto.TASK_DISPATCH and row["attempts"] >= DISPATCH_MAX_SENDS:
                    await asyncio.to_thread(self._store.outbox_expire, row["id"])
                    if self.on_expired:
                        await self.on_expired(agent, row)
                    continue
                try:
                    conn = await self._get(agent)
                    await conn.send(row["envelope"])
                except LinkError as e:
                    self._router.report_failure(agent.id, str(e))
                    log.info("link: send to %s failed (%s); retrying later", agent.name, e)
                    for r in heads:
                        if r["type"] == proto.TASK_DISPATCH:
                            # Count failed connects toward the dispatch limit so an
                            # unreachable agent fails over promptly.
                            await asyncio.to_thread(
                                self._store.outbox_mark_sent, r["id"],
                                retry_in_s=backoff_s(r["attempts"] + 1, agent.max_reply_latency_s),
                            )
                        else:
                            await asyncio.to_thread(
                                self._store.outbox_defer, r["id"],
                                retry_in_s=backoff_s(r["attempts"] + 1, agent.max_reply_latency_s),
                            )
                    return sent
                await asyncio.to_thread(
                    self._store.outbox_mark_sent, row["id"],
                    retry_in_s=backoff_s(row["attempts"] + 1, agent.max_reply_latency_s),
                )
                sent += 1
            return sent

    async def _get(self, agent: AgentRecord) -> AgentConnection:
        async with self._conn_locks[agent.id]:
            conn = self._conns.get(agent.id)
            if conn is not None and (conn.closed or conn.url != proto.task_url(agent.url)):
                self._conns.pop(agent.id, None)
                await conn.close("replaced")
                conn = None
            if conn is None:
                while len(self._conns) >= self._max:
                    victim_id, victim = next(iter(self._conns.items()))
                    self._conns.pop(victim_id, None)
                    await victim.close("evicted")
                conn = AgentConnection(agent, self)
                await conn.open()
                self._router.report_success(agent.id)
                self._conns[agent.id] = conn
            self._conns.move_to_end(agent.id)
            return conn

    # ----- incoming -------------------------------------------------------------

    async def handle_reply(self, agent: AgentRecord, msg: dict) -> None:
        reply_to = str(msg.get("reply_to") or "")
        if not reply_to:
            return
        pending = await asyncio.to_thread(self._store.outbox_get, reply_to)
        if not pending or pending.get("agent_id") != agent.id:
            log.info("link: ignoring reply from %s for a message it wasn't sent", agent.name)
            return
        row = await asyncio.to_thread(self._store.outbox_ack, reply_to, msg)
        if not row:
            return  # duplicate reply, or for a message we no longer track
        if self.on_reply:
            try:
                await self.on_reply(agent, row, msg)
            except Exception:
                log.exception("link: reply handler failed msg_id=%s", reply_to)
        self.kick(agent.id)  # the next row for that task is now head-of-line

    async def handle_event(self, agent: AgentRecord, msg: dict) -> Optional[int]:
        if self.on_event is None:
            return None
        try:
            return await self.on_event(agent, msg)
        except Exception:
            log.exception("link: event handler failed task_id=%s", msg.get("task_id"))
            return None

    # ----- background -----------------------------------------------------------

    async def sweep_once(self) -> None:
        expired = await asyncio.to_thread(self._store.outbox_expire_older_than, TTL_H)
        if expired:
            log.info("link: expired %d outbox rows older than %sh", expired, TTL_H)
        heads = await asyncio.to_thread(self._store.outbox_heads, due_only=True, limit=500)
        for agent_id in dict.fromkeys(r["agent_id"] for r in heads):
            try:
                await self.deliver(agent_id)
            except Exception:
                log.exception("link: sweep delivery failed agent_id=%s", agent_id)

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_s)
            try:
                await self.sweep_once()
            except Exception:
                log.exception("link: sweep failed")

    async def reap_idle(self) -> int:
        now = time.monotonic()
        stale = [aid for aid, c in self._conns.items() if c.closed or now - c.last_used > self._idle_close_s]
        for aid in stale:
            conn = self._conns.pop(aid, None)
            if conn is not None:
                await conn.close("idle")
        return len(stale)

    async def _reap_forever(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, self._idle_close_s / 2))
            try:
                await self.reap_idle()
            except Exception:
                log.exception("link: reaper failed")
