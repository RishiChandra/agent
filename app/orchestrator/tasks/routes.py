"""HTTP routes for background tasks (ORCHESTRATOR_V2_TOOL_CALLS.md §1.5–1.7).

* `/api/dispatch/*`: the task API. **Always** requires
  `Authorization: Bearer $DISPATCH_API_TOKEN`; with no token configured every
  request is refused (decision E1), because it can make agents act for any user.
* `POST /developer/tasks/{task_id}/events`: agents push `task.event` here,
  authenticated by the per-task callback token from the dispatch.
* `POST /internal/tasks/{task_id}/deadline`: the worker's deadline hook,
  authenticated by `INTERNAL_API_TOKEN` (default: derived from DB_PASSWORD,
  which the app and worker share).
* `GET /api/agents/search`, `GET /api/router/stats`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from orchestrator.tasks import protocol as proto
from orchestrator.routing.router import DECISION_AMBIGUOUS, DECISION_MATCHED, DECISION_NONE, DECISION_WRONG_MODE, get_router
from orchestrator.tasks.service import TaskError, get_service, to_public
from orchestrator.tasks.store import parse_ts

log = logging.getLogger("task_service")

router = APIRouter()


def internal_token() -> str:
    explicit = os.environ.get("INTERNAL_API_TOKEN", "").strip()
    if explicit:
        return explicit
    secret = os.environ.get("DB_PASSWORD", "")
    if not secret:
        return ""
    return hmac.new(secret.encode(), b"aipin-internal-api", hashlib.sha256).hexdigest()


def _bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        return ""
    return authorization.split(" ", 1)[1].strip()


def _require(expected: str, authorization: Optional[str], *, what: str) -> None:
    if not expected:
        raise HTTPException(status_code=503, detail=f"{what} disabled: token not configured")
    if not hmac.compare_digest(_bearer(authorization), expected):
        raise HTTPException(status_code=401, detail="invalid token")


def _dispatch_auth(authorization: Optional[str]) -> None:
    _require(os.environ.get("DISPATCH_API_TOKEN", "").strip(), authorization, what="dispatch API")


# ----- agent event callback --------------------------------------------------


@router.post("/developer/tasks/{task_id}/events")
async def agent_event(task_id: str, request: Request, authorization: Optional[str] = Header(None)):
    try:
        msg = await request.json()
    except Exception:
        return _json(400, {"ok": False, "reason": "body must be JSON"})
    if not isinstance(msg, dict) or msg.get("type") != proto.TASK_EVENT:
        return _json(400, {"ok": False, "reason": "expected a task.event"})
    token = _bearer(authorization)
    if not token:
        return _json(401, {"ok": False, "reason": "missing callback token"})
    try:
        seq = await get_service().handle_event(msg, token=token, task_id=task_id)
    except TaskError as e:
        if e.code == proto.NACK_UNKNOWN_TASK:
            return _json(404, {"ok": False, "reason": "unknown task"})
        if e.code == proto.NACK_UNAUTHORIZED:
            return _json(401, {"ok": False, "reason": str(e)})
        return _json(400, {"ok": False, "reason": str(e)})
    return {"ok": True, "ack_seq": seq}


def _json(status: int, body: dict):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status, content=body)


# ----- internal deadline hook ------------------------------------------------


@router.post("/internal/tasks/{task_id}/deadline")
async def internal_deadline(task_id: str, request: Request, authorization: Optional[str] = Header(None)):
    _require(internal_token(), authorization, what="internal API")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    outcome = await get_service().on_deadline(task_id, payload if isinstance(payload, dict) else {})
    return {"ok": True, "outcome": outcome}


# ----- dispatch API ----------------------------------------------------------


class DispatchRequest(BaseModel):
    user_id: str
    intent: str
    agent: Optional[str] = None
    slots: dict = {}
    notify: str = "next_session"
    deadline_at: Optional[str] = None
    drop_at_deadline: bool = False


class UpdateRequest(BaseModel):
    changes: dict = {}
    deadline_at: Optional[str] = None
    drop_at_deadline: Optional[bool] = None


class InputRequest(BaseModel):
    answer: str


@router.post("/api/dispatch", status_code=202)
async def api_dispatch(req: DispatchRequest, authorization: Optional[str] = Header(None)):
    _dispatch_auth(authorization)
    r = get_router()
    await r.ensure_fresh_async()
    named = bool((req.agent or "").strip())
    route = r.resolve(req.agent or "", req.intent, mode=proto.MODE_TASK, k=5)
    if route.decision == DECISION_WRONG_MODE:
        raise HTTPException(409, detail={"reason": "wrong_mode", "agent": route.wrong_mode.name if route.wrong_mode else ""})
    if route.decision == DECISION_NONE or route.best is None:
        raise HTTPException(404, detail={"reason": "no_agent"})
    if route.decision == DECISION_AMBIGUOUS and named:
        raise HTTPException(409, detail={"reason": "ambiguous", "candidates": route.names(5)})
    notify = req.notify if req.notify in ("device", "next_session", "silent") else "next_session"
    out = await get_service().dispatch(
        user_id=req.user_id, agent=route.best, intent=req.intent, slots=req.slots, notify=notify,
        deadline_at=parse_ts(req.deadline_at), drop_at_deadline=req.drop_at_deadline,
        chain=[c.agent for c in route.candidates[1:3]] if not named else (), named=named,
    )
    if not out.ok:
        code = {"user_limit": 429, "busy": 503, "bad_request": 400}.get(out.reason, 400)
        raise HTTPException(code, detail={"reason": out.reason})
    return {"ok": True, "task": to_public(out.task)}


async def _task_or_404(task_id: str) -> dict:
    task = await get_service().get(task_id)
    if task is None or (task.get("task_info") or {}).get("deleted"):
        raise HTTPException(404, detail="task not found")
    return task


@router.get("/api/dispatch/user/{user_id}")
async def api_list(user_id: str, active_only: bool = False, authorization: Optional[str] = Header(None)):
    _dispatch_auth(authorization)
    return {"tasks": [to_public(t) for t in await get_service().list(user_id, active_only=active_only)]}


@router.get("/api/dispatch/{task_id}")
async def api_get(task_id: str, authorization: Optional[str] = Header(None)):
    _dispatch_auth(authorization)
    return to_public(await _task_or_404(task_id))


@router.patch("/api/dispatch/{task_id}")
async def api_update(task_id: str, req: UpdateRequest, authorization: Optional[str] = Header(None)):
    _dispatch_auth(authorization)
    svc = get_service()
    task = await _task_or_404(task_id)
    line = ""
    if req.changes:
        line = await svc.update(task, req.changes)
    if req.deadline_at is not None or req.drop_at_deadline is not None:
        await svc.set_deadline(task, parse_ts(req.deadline_at) if req.deadline_at else None, drop=req.drop_at_deadline)
    return {"ok": True, "message": line, "task": to_public(await _task_or_404(task_id))}


@router.post("/api/dispatch/{task_id}/cancel")
async def api_cancel(task_id: str, authorization: Optional[str] = Header(None)):
    _dispatch_auth(authorization)
    line = await get_service().cancel(await _task_or_404(task_id))
    return {"ok": True, "message": line}


@router.post("/api/dispatch/{task_id}/complete")
async def api_complete(task_id: str, authorization: Optional[str] = Header(None)):
    _dispatch_auth(authorization)
    line = await get_service().complete(await _task_or_404(task_id))
    return {"ok": True, "message": line}


@router.post("/api/dispatch/{task_id}/input")
async def api_input(task_id: str, req: InputRequest, authorization: Optional[str] = Header(None)):
    _dispatch_auth(authorization)
    line = await get_service().answer(await _task_or_404(task_id), req.answer)
    return {"ok": True, "message": line}


@router.delete("/api/dispatch/{task_id}")
async def api_delete(task_id: str, authorization: Optional[str] = Header(None)):
    _dispatch_auth(authorization)
    line = await get_service().delete(await _task_or_404(task_id))
    return {"ok": True, "message": line}


# ----- discovery and stats ---------------------------------------------------


@router.get("/api/agents/search")
async def api_search(q: str, k: int = 5) -> dict[str, Any]:
    from orchestrator.routing import embeddings

    r = get_router()
    await r.ensure_fresh_async()
    emb = await embeddings.embed_query(q)
    return {"query": q, "results": [c.to_public() for c in r.search(q, k=max(1, min(k, 20)), query_embedding=emb)]}


@router.get("/api/router/stats")
async def api_stats(authorization: Optional[str] = Header(None)):
    _dispatch_auth(authorization)
    svc = get_service()
    return {"router": get_router().stats(), "connections": svc.link.connection_count()}


__all__ = ["router", "internal_token", "DECISION_MATCHED"]
