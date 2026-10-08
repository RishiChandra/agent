"""Reference Protocol 2 (task mode) agent, for tests and local development.

Implements the agent side of orchestrator/BRIDGE_PROTOCOL.md "Task mode":

* Handshake: answers `hello {mode: "task"}` with an ack advertising
  `modes`, `task_ops`, `events: ["callback", "ws"]`, `max_concurrency`.
  Rejects unsupported modes with close code 4405.
* Requests: every `task.*` request gets exactly one `task.ack` / `task.nack`
  with `reply_to`. A repeated dispatch for a known `task_id` returns the same
  ack and does not start the work again.
* Events: pushed to the HTTP callback with an increasing `seq`, resent until
  acked (`ack_seq`), backing off from 1 s to 5 min for at least 24 h.
* Heartbeat: `/developer/register` with `open_task_ids` (required in task
  mode; the standalone runner sends it every 5 min).

Behaviour is driven by the intent text so tests can exercise every path:

  "slow"             finish after `slow_s` instead of `work_s`
  "confirm"          ask "Which restaurant?" first (input-required)
  "twice"            ask two questions (triggers the escalation offer)
  "table" without a `party_size` slot  -> nack missing_input [party_size]
  "fail"             finish with status failed
  "never"            never finish (for deadline / stall tests)
  "busy"             nack busy (retryable)
  "tight"            nack cannot_meet_deadline when the dispatch has a deadline
  "frozen"           nack task.update with `unsupported`

Run standalone:  python orchestrator/testing/task_agent.py --port 8011 --register
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from orchestrator.tasks import protocol as proto  # noqa: E402

log = logging.getLogger("task_agent")


@dataclass
class AgentTask:
    task_id: str
    intent: str
    slots: dict
    callback: dict
    context_id: Optional[str] = None
    seq: int = 0
    acked_seq: int = 0
    status: str = proto.SUBMITTED
    questions_left: int = 0
    answers: list[str] = field(default_factory=list)
    runner: Optional[asyncio.Task] = None
    cancelled: bool = False
    closed_reason: str = ""
    delivered_via: str = ""
    updates: list[dict] = field(default_factory=list)


class TaskAgent:
    def __init__(
        self,
        *,
        service_id: Optional[str] = None,
        task_ops: tuple[str, ...] = ("dispatch", "status", "update", "cancel", "input", "close", "delivered"),
        work_s: float = 0.2,
        slow_s: float = 3.0,
        max_concurrency: int = 8,
        drop_acks: int = 0,
        resend_first_s: float = 1.0,
        resend_max_s: float = 300.0,
        resend_for_s: float = 24 * 3600,
    ) -> None:
        self.resend_first_s = resend_first_s
        self.resend_max_s = resend_max_s
        self.resend_for_s = resend_for_s
        self.service_id = service_id or f"task-agent-{uuid.uuid4().hex[:6]}"
        self.task_ops = task_ops
        self.work_s = work_s
        self.slow_s = slow_s
        self.max_concurrency = max_concurrency
        self.tasks: dict[str, AgentTask] = {}
        self.dispatch_count: dict[str, int] = {}
        self.received: list[dict] = []
        self._drop_acks = drop_acks  # swallow the first N dispatch acks (lost-ack tests)
        self._server = None
        self.port = 0
        self._http = httpx.AsyncClient(timeout=5.0)

    # ----- server -------------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        self._server = await websockets.serve(self._handle, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return f"ws://{host}:{self.port}/relay"

    async def stop(self) -> None:
        for t in self.tasks.values():
            if t.runner and not t.runner.done():
                t.runner.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        await self._http.aclose()

    async def _handle(self, ws) -> None:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
        except Exception:
            return
        hello = proto.parse(raw) or {}
        if hello.get("type") != proto.HELLO:
            await ws.close(code=1002)
            return
        mode = hello.get("mode") or proto.MODE_BRIDGE
        if mode != proto.MODE_TASK:
            await ws.send(json.dumps({"type": proto.ACK, "accept": False, "reason": "task mode only"}))
            await ws.close(code=proto.CLOSE_MODE_NOT_SUPPORTED)
            return
        await ws.send(json.dumps({
            "type": proto.ACK, "accept": True, "service_id": self.service_id, "version": "2",
            "modes": ["task"], "max_concurrency": self.max_concurrency,
            "task_ops": list(self.task_ops), "events": ["callback", "ws"],
        }))
        try:
            async for raw in ws:
                msg = proto.parse(raw)
                if not msg:
                    continue
                self.received.append(msg)
                mtype = msg.get("type")
                if mtype == proto.BYE:
                    break
                if mtype == proto.TASK_EVENT_ACK:
                    continue
                reply = await self._request(msg)
                if reply is not None:
                    await ws.send(proto.dumps(reply))
        except websockets.exceptions.ConnectionClosed:
            pass

    # ----- requests -----------------------------------------------------------

    async def _request(self, msg: dict) -> Optional[dict]:
        mtype = msg.get("type")
        task_id = str(msg.get("task_id") or "")
        body = msg.get("body") or {}
        if mtype not in proto.REQUEST_TYPES:
            return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_UNSUPPORTED})
        if proto.OP_OF[mtype] not in self.task_ops:
            return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_UNSUPPORTED})
        if mtype == proto.TASK_DISPATCH:
            return self._dispatch(msg, task_id, body)
        task = self.tasks.get(task_id)
        if task is None:
            return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_UNKNOWN_TASK})
        if mtype == proto.TASK_STATUS:
            return proto.reply(proto.TASK_ACK, msg, {"status": task.status, "last_seq": task.seq})
        if mtype == proto.TASK_UPDATE:
            if task.status in proto.TERMINAL_WIRE_STATES:
                return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_TOO_LATE})
            if "frozen" in task.intent:
                return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_UNSUPPORTED})
            changes = body.get("changes") or {}
            task.slots.update(changes)
            task.updates.append(changes)
            return proto.reply(proto.TASK_ACK, msg, {"status": task.status, "applied": changes})
        if mtype == proto.TASK_CANCEL:
            if task.status in proto.TERMINAL_WIRE_STATES:
                return proto.reply(proto.TASK_ACK, msg, {"status": "already_finished",
                                                          "result": {"say": f"Done: {task.intent}"}})
            task.cancelled = True
            if task.runner and not task.runner.done():
                task.runner.cancel()
            task.status = proto.CANCELED
            return proto.reply(proto.TASK_ACK, msg, {"status": "canceled"})
        if mtype == proto.TASK_INPUT:
            if task.status != proto.INPUT_REQUIRED:
                return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_NO_QUESTION})
            task.answers.append(str(body.get("answer") or ""))
            task.status = proto.WORKING
            task.runner = asyncio.create_task(self._work(task))
            return proto.reply(proto.TASK_ACK, msg, {"status": proto.WORKING})
        if mtype == proto.TASK_CLOSE:
            task.closed_reason = str(body.get("reason") or "")
            if task.runner and not task.runner.done():
                task.runner.cancel()
            return proto.reply(proto.TASK_ACK, msg, {})
        if mtype == proto.TASK_DELIVERED:
            task.delivered_via = str(body.get("via") or "")
            return proto.reply(proto.TASK_ACK, msg, {})
        return None

    def _dispatch(self, msg: dict, task_id: str, body: dict) -> Optional[dict]:
        self.dispatch_count[task_id] = self.dispatch_count.get(task_id, 0) + 1
        intent = str(body.get("intent") or "").lower()
        slots = dict(body.get("slots") or {})
        if task_id in self.tasks:  # idempotent: same ack, no new work
            if body.get("callback"):
                self.tasks[task_id].callback = body["callback"]  # always use the latest token
            return proto.reply(proto.TASK_ACK, msg, {"status": self.tasks[task_id].status})
        if "tight" in intent and body.get("deadline_at"):
            return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_CANNOT_MEET_DEADLINE,
                                                       "message": "It takes at least a day."})
        if "busy" in intent:
            return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_BUSY, "retryable": True})
        if "table" in intent and "party_size" not in slots:
            return proto.reply(proto.TASK_NACK, msg, {
                "code": proto.NACK_MISSING_INPUT, "fields": ["party_size"], "message": "How many people?",
            })
        task = AgentTask(task_id=task_id, intent=intent, slots=slots, callback=body.get("callback") or {},
                         context_id=msg.get("contextId"))
        task.questions_left = 2 if "twice" in intent else (1 if "confirm" in intent else 0)
        self.tasks[task_id] = task
        if self._drop_acks > 0:
            self._drop_acks -= 1
            task.runner = asyncio.create_task(self._work(task))
            return None  # simulate a lost ack: the work starts, the reply never arrives
        task.runner = asyncio.create_task(self._work(task))
        return proto.reply(proto.TASK_ACK, msg, {"status": proto.SUBMITTED, "agent_task_ref": f"ref-{task_id[:6]}"})

    # ----- work + events ------------------------------------------------------

    async def _work(self, task: AgentTask) -> None:
        try:
            if "never" in task.intent:
                await self.emit(task, {"status": proto.WORKING, "progress": {"message": "waiting"}})
                await asyncio.sleep(3600)
            await asyncio.sleep(self.slow_s if "slow" in task.intent else self.work_s)
            if task.questions_left > 0:
                task.questions_left -= 1
                task.status = proto.INPUT_REQUIRED
                await self.emit(task, {"status": proto.INPUT_REQUIRED, "question": "Which restaurant?"})
                return
            if "fail" in task.intent:
                task.status = proto.FAILED
                await self.emit(task, {"status": proto.FAILED, "error": "No tables left."})
                return
            task.status = proto.COMPLETED
            say = f"Done: {task.intent}" + (f" ({', '.join(task.answers)})" if task.answers else "")
            await self.emit(task, {"status": proto.COMPLETED, "result": {"say": say, "output": {"ok": True}}})
        except asyncio.CancelledError:
            pass

    async def emit(self, task: AgentTask, body: dict) -> None:
        task.seq += 1
        msg = proto.envelope(proto.TASK_EVENT, task_id=task.task_id, seq=task.seq, body=body)
        await self._post_until_acked(task, msg)

    async def _post_until_acked(self, task: AgentTask, msg: dict) -> None:
        loop = asyncio.get_running_loop()
        give_up = loop.time() + self.resend_for_s
        delay = self.resend_first_s
        while loop.time() < give_up:
            url, token = task.callback.get("url"), task.callback.get("token")
            try:
                r = await self._http.post(url, json=msg, headers={"Authorization": f"Bearer {token}"})
                if r.status_code == 200 and int(r.json().get("ack_seq", 0)) >= msg["seq"]:
                    task.acked_seq = msg["seq"]
                    return
                if r.status_code in (400, 401, 404):
                    log.warning("event rejected %s: %s", r.status_code, r.text)
                    return
            except Exception as e:
                log.info("event post failed (%s); retrying", e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.resend_max_s)

    # ----- registration ---------------------------------------------------------

    async def register(self, main_base: str, public_url: str, **extra: Any) -> dict:
        body = {
            "service_id": self.service_id, "public_url": public_url, "version": "2",
            "name": extra.pop("name", self.service_id), "modes": ["task"],
            "task_ops": list(self.task_ops), "events": ["callback", "ws"],
            "max_concurrency": self.max_concurrency,
            "open_task_ids": [t.task_id for t in self.tasks.values()
                              if t.status not in proto.TERMINAL_WIRE_STATES and not t.cancelled],
            **extra,
        }
        r = await self._http.post(f"{main_base.rstrip('/')}/developer/register", json=body)
        return r.json()


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("TASK_AGENT_PORT", "8011")))
    ap.add_argument("--register", action="store_true", help="register with main and heartbeat every 5 min")
    ap.add_argument("--main", default=os.environ.get("MAIN_HTTP_BASE", "http://localhost:8000"))
    ap.add_argument("--name", default="Task Echo")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    agent = TaskAgent()
    url = await agent.start("0.0.0.0", args.port)
    url = url.replace("0.0.0.0", "localhost")
    log.info("task agent listening on %s", url)
    while True:
        if args.register:
            try:
                log.info("register: %s", await agent.register(args.main, url, name=args.name, side_effects=False))
            except Exception as e:
                log.warning("register failed: %s", e)
        await asyncio.sleep(300)


if __name__ == "__main__":
    asyncio.run(_main())
