"""Kairos task mode (Protocol 2): background tasks dispatched by the orchestrator.

Kairos's live calls run on Gemini Live (`websocket_handler.py`). This module
lets the orchestrator hand Kairos a request without a call ("set a reminder to
go to the gym at ten tomorrow"): the orchestrator dials Kairos's registered URL
with `{user_id}` filled in as `orchestrator`, sends `hello {mode: "task"}`, then
`task.dispatch` messages for any user. Wire spec:
app/orchestrator/BRIDGE_PROTOCOL.md ("Task mode").

Each dispatch runs Kairos's text agent (`GeneralThinkingAgent.think`, the same
tool loop the live call's `think` function uses) for the dispatch's user, and
the reply becomes the task result. Events go to the task's callback with its
bearer token and are resent until acknowledged.

Limits, by design for now:
* Tasks live in memory. After a restart Kairos answers `task.status` with
  `unknown_task`, and the orchestrator marks the task lost.
* `task.cancel` cannot stop a think() already running in its thread; the task
  is reported cancelled and its result is dropped, but a reminder it already
  created stays.
* `update` and `input` are not supported (Kairos never asks questions in task
  mode); the orchestrator handles `update` by cancelling and re-dispatching.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from fastapi import WebSocket, WebSocketDisconnect

from orchestrator.tasks import protocol as proto

log = logging.getLogger("kairos.tasks")

SERVICE_ID = "kairos"
TASK_OPS = ("dispatch", "status", "cancel", "close", "delivered")
MAX_CONCURRENCY = int(os.environ.get("KAIROS_TASK_CONCURRENCY", "4"))
# Events are always posted to this app (Kairos runs inside it), never to a host
# named in the dispatch, so a dispatch cannot make Kairos call arbitrary URLs.
_CALLBACK_PATH = re.compile(r"^/developer/tasks/[A-Za-z0-9_-]+/events$")
RESEND_FIRST_S = 2.0
RESEND_MAX_S = 300.0
RESEND_FOR_S = 24 * 3600.0
# Finished tasks are forgotten this long after they end, even without close/delivered.
KEEP_FINISHED_S = 24 * 3600.0


def _callback_base() -> str:
    return os.environ.get("KAIROS_CALLBACK_BASE") or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"


def _callback_target(url: str) -> Optional[str]:
    path = urllib.parse.urlsplit(url or "").path
    if not _CALLBACK_PATH.match(path):
        return None
    return _callback_base().rstrip("/") + path


@dataclass
class KairosTask:
    task_id: str
    user_id: str
    request: str
    callback: dict
    context_id: Optional[str] = None
    status: str = proto.SUBMITTED
    seq: int = 0
    say: str = ""
    error: str = ""
    cancelled: bool = False
    finished_at: float = 0.0
    runner: Optional[asyncio.Task] = None
    sender: Optional[asyncio.Task] = None
    pending: list = field(default_factory=list)


class KairosTaskAgent:
    def __init__(self) -> None:
        self.tasks: dict[str, KairosTask] = {}
        self._slots = asyncio.Semaphore(MAX_CONCURRENCY)
        self._http: Optional[httpx.AsyncClient] = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    # ----- connection -----------------------------------------------------------

    async def serve(self, websocket: WebSocket) -> None:
        """Run one task-mode connection (already accepted)."""
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        except Exception:
            await websocket.close(code=1002)
            return
        hello = proto.parse(raw) or {}
        if hello.get("type") != proto.HELLO or (hello.get("mode") or proto.MODE_BRIDGE) != proto.MODE_TASK:
            await websocket.send_text(json.dumps({"type": proto.ACK, "accept": False,
                                                  "reason": "this URL takes task mode only"}))
            await websocket.close(code=proto.CLOSE_MODE_NOT_SUPPORTED)
            return
        await websocket.send_text(json.dumps({
            "type": proto.ACK, "accept": True, "service_id": SERVICE_ID, "version": proto.PROTOCOL_VERSION,
            "modes": [proto.MODE_BRIDGE, proto.MODE_TASK], "max_concurrency": MAX_CONCURRENCY,
            "task_ops": list(TASK_OPS), "events": ["callback"],
        }))
        log.info("kairos task connection open")
        try:
            while True:
                msg = proto.parse(await websocket.receive_text())
                if not msg:
                    continue
                mtype = msg.get("type")
                if mtype == proto.BYE:
                    break
                if mtype == proto.PING:
                    await websocket.send_text(json.dumps({"type": proto.PONG, "ts": msg.get("ts")}))
                    continue
                if mtype in (proto.PONG, proto.TASK_EVENT_ACK):
                    continue
                reply = self.handle(msg)
                if reply is not None:
                    await websocket.send_text(proto.dumps(reply))
        except WebSocketDisconnect:
            pass
        finally:
            log.info("kairos task connection closed")

    # ----- requests ---------------------------------------------------------------

    def handle(self, msg: dict) -> Optional[dict]:
        self._forget_old()
        mtype = msg.get("type")
        task_id = str(msg.get("task_id") or "")
        body = msg.get("body") or {}
        if mtype not in proto.REQUEST_TYPES or proto.OP_OF[mtype] not in TASK_OPS:
            return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_UNSUPPORTED})
        if mtype == proto.TASK_DISPATCH:
            return self._dispatch(msg, task_id, body)
        task = self.tasks.get(task_id)
        if task is None:
            return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_UNKNOWN_TASK})
        if mtype == proto.TASK_STATUS:
            return proto.reply(proto.TASK_ACK, msg, {"status": task.status, "last_seq": task.seq})
        if mtype == proto.TASK_CANCEL:
            if task.status in proto.TERMINAL_WIRE_STATES:
                out: dict[str, Any] = {"status": "already_finished"}
                if task.say:
                    out["result"] = {"say": task.say}
                return proto.reply(proto.TASK_ACK, msg, out)
            task.cancelled = True
            self._finish(task, proto.CANCELED)
            return proto.reply(proto.TASK_ACK, msg, {"status": proto.CANCELED})
        # close / delivered: the orchestrator is done with this task.
        if task.status not in proto.TERMINAL_WIRE_STATES:
            task.cancelled = True
        self._drop(task)
        return proto.reply(proto.TASK_ACK, msg, {})

    def _dispatch(self, msg: dict, task_id: str, body: dict) -> dict:
        existing = self.tasks.get(task_id)
        if existing is not None:  # resend of the same dispatch: same answer, latest token
            if body.get("callback"):
                existing.callback = body["callback"]
            return proto.reply(proto.TASK_ACK, msg, {"status": existing.status})
        user_id = str(body.get("user_id") or "").strip()
        request = _request_text(body.get("intent"), body.get("slots"))
        callback = body.get("callback") or {}
        if not task_id or not user_id or not request:
            return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_INVALID_INPUT,
                                                       "message": "task_id, user_id and intent are required"})
        if _callback_target(callback.get("url") or "") is None or not callback.get("token"):
            return proto.reply(proto.TASK_NACK, msg, {"code": proto.NACK_INVALID_INPUT,
                                                       "message": "callback must be this app's task events URL"})
        task = KairosTask(task_id=task_id, user_id=user_id, request=request, callback=callback,
                          context_id=msg.get("contextId"))
        self.tasks[task_id] = task
        task.runner = asyncio.create_task(self._work(task), name=f"kairos-task-{task_id[:8]}")
        log.info("dispatch task_id=%s user_id=%s request=%r", task_id, user_id, request)
        return proto.reply(proto.TASK_ACK, msg, {"status": proto.SUBMITTED, "agent_task_ref": task_id})

    # ----- work -------------------------------------------------------------------

    async def _work(self, task: KairosTask) -> None:
        async with self._slots:
            if task.cancelled:
                return
            task.status = proto.WORKING
            self._emit(task, {"status": proto.WORKING})
            started = time.monotonic()
            try:
                say = await asyncio.to_thread(_think, task.user_id, task.request)
            except Exception as e:
                log.exception("think failed task_id=%s", task.task_id)
                if not task.cancelled:
                    task.error = "Kairos couldn't do that right now."
                    self._finish(task, proto.FAILED, {"status": proto.FAILED, "error": task.error,
                                                      "detail": proto.clip(str(e), 300)})
                return
            log.info("think done task_id=%s in %.1fs cancelled=%s", task.task_id,
                     time.monotonic() - started, task.cancelled)
            if task.cancelled:
                return
            task.say = proto.clip(say, proto.MAX_SAY_CHARS) or "Done."
            self._finish(task, proto.COMPLETED, {"status": proto.COMPLETED,
                                                  "result": {"say": task.say, "output": {"reply": say}}})

    def _finish(self, task: KairosTask, status: str, event: Optional[dict] = None) -> None:
        task.status = status
        task.finished_at = time.monotonic()
        if event is not None:
            self._emit(task, event)

    # ----- events -----------------------------------------------------------------

    def _emit(self, task: KairosTask, body: dict) -> None:
        task.seq += 1
        task.pending.append(proto.envelope(proto.TASK_EVENT, task_id=task.task_id, seq=task.seq, body=body))
        if task.sender is None or task.sender.done():
            task.sender = asyncio.create_task(self._send_pending(task), name=f"kairos-events-{task.task_id[:8]}")

    async def _send_pending(self, task: KairosTask) -> None:
        """Post events in order; each is resent until the orchestrator acks its seq."""
        while task.pending:
            msg = task.pending[0]
            if not await self._post_until_acked(task, msg):
                task.pending.clear()
                return
            task.pending.pop(0)

    async def _post_until_acked(self, task: KairosTask, msg: dict) -> bool:
        give_up = time.monotonic() + RESEND_FOR_S
        delay = RESEND_FIRST_S
        while time.monotonic() < give_up:
            target = _callback_target(task.callback.get("url") or "")
            try:
                r = await self._client().post(target, json=msg,
                                              headers={"Authorization": f"Bearer {task.callback.get('token')}"})
                if r.status_code == 200 and int(r.json().get("ack_seq", 0)) >= msg["seq"]:
                    return True
                if r.status_code in (400, 401, 404, 410):
                    log.warning("event rejected task_id=%s seq=%s: %s %s", task.task_id, msg["seq"],
                                r.status_code, r.text[:200])
                    return False
            except Exception as e:
                log.info("event post failed task_id=%s (%s); retrying in %.0fs", task.task_id, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RESEND_MAX_S)
        return False

    # ----- bookkeeping ------------------------------------------------------------

    def _drop(self, task: KairosTask) -> None:
        self.tasks.pop(task.task_id, None)
        if task.sender and not task.sender.done() and task.status in proto.TERMINAL_WIRE_STATES:
            return  # let a terminal event finish delivering
        if task.sender and not task.sender.done():
            task.sender.cancel()

    def _forget_old(self) -> None:
        now = time.monotonic()
        for task in [t for t in self.tasks.values()
                     if t.finished_at and now - t.finished_at > KEEP_FINISHED_S]:
            self.tasks.pop(task.task_id, None)

    def open_task_ids(self) -> list[str]:
        return [t.task_id for t in self.tasks.values() if t.status not in proto.TERMINAL_WIRE_STATES]


def _request_text(intent: Any, slots: Any) -> str:
    text = str(intent or "").strip()
    if isinstance(slots, dict) and slots:
        details = ", ".join(f"{k}: {v}" for k, v in slots.items() if str(v).strip())
        if details:
            text = f"{text} ({details})" if text else details
    return text


def _think(user_id: str, request: str) -> str:
    """Run Kairos's text agent once for `user_id` (blocking; called in a thread)."""
    from user_session_manager import UserSessionManager
    from websocket_handler import generalThinkingAgent

    user_config = UserSessionManager.config_only(user_id)
    out = generalThinkingAgent.think(request, [], user_config=user_config)
    if isinstance(out, dict):
        out = out.get("result")
    return str(out or "").strip()


_agent: Optional[KairosTaskAgent] = None


def get_agent() -> KairosTaskAgent:
    global _agent
    if _agent is None:
        _agent = KairosTaskAgent()
    return _agent


async def task_endpoint(websocket: WebSocket) -> None:
    await get_agent().serve(websocket)
