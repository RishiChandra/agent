"""HTTP API for task dispatch (protocol v2) and routing diagnostics.

Lets non-voice callers — the scheduler, another agent, a test script — hand a
task to a registered agent and poll for its result, using the same dispatcher
the voice session uses. If the target user has a live voice session, results
are also announced there.

    POST /api/dispatch                     submit a task
    GET  /api/dispatch/{task_id}           task status/result
    GET  /api/dispatch/user/{user_id}      a user's recent tasks
    POST /api/dispatch/{task_id}/cancel    cancel a running task
    POST /api/dispatch/{task_id}/input     answer an agent's question
    GET  /api/router/stats                 router + dispatcher health

Auth: when `DISPATCH_API_TOKEN` is set, every route requires
`Authorization: Bearer <token>`. Unset keeps the v1 "open API" behaviour of
the rest of the service — set it in production, since dispatch triggers
side effects in agents on a user's behalf.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

import task_dispatcher as td
from agent_router import get_router

router = APIRouter()


def _require_token(authorization: Optional[str] = Header(default=None)) -> None:
    token = os.environ.get("DISPATCH_API_TOKEN", "").strip()
    if not token:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="invalid or missing dispatch token")


class DispatchRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=200)
    intent: str = Field(..., min_length=1, max_length=2000)
    agent: Optional[str] = Field(default=None, max_length=200)
    input: Optional[dict[str, Any]] = None
    deadline_s: Optional[float] = Field(default=None, gt=0, le=td.MAX_DEADLINE_S)


class InputRequest(BaseModel):
    answer: str = Field(..., min_length=1, max_length=2000)


_REASON_STATUS = {
    td.REASON_BAD_REQUEST: 400,
    td.REASON_NO_AGENT: 404,
    td.REASON_WRONG_MODE: 409,
    td.REASON_AMBIGUOUS: 409,
    td.REASON_USER_LIMIT: 429,
    td.REASON_BUSY: 503,
}


@router.post("/api/dispatch", status_code=202, dependencies=[Depends(_require_token)])
async def api_dispatch(req: DispatchRequest):
    res = await td.get_dispatcher().submit(
        user_id=req.user_id.strip(),
        intent=req.intent,
        agent=(req.agent or ""),
        input=req.input,
        deadline_s=req.deadline_s,
        source="api",
    )
    if not res.ok:
        raise HTTPException(status_code=_REASON_STATUS.get(res.reason, 400), detail=res.to_public())
    return res.to_public()


@router.get("/api/dispatch/user/{user_id}", dependencies=[Depends(_require_token)])
def api_dispatch_for_user(
    user_id: str,
    active_only: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
):
    recs = td.get_dispatcher().list_for_user(user_id, active_only=active_only, limit=limit)
    return {"tasks": [r.to_public() for r in recs]}


@router.get("/api/dispatch/{task_id}", dependencies=[Depends(_require_token)])
def api_dispatch_get(task_id: str):
    rec = td.get_dispatcher().get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="task not found")
    return rec.to_public()


@router.post("/api/dispatch/{task_id}/cancel", dependencies=[Depends(_require_token)])
async def api_dispatch_cancel(task_id: str):
    rec = await td.get_dispatcher().cancel(task_id, "api_cancel")
    if rec is None:
        raise HTTPException(status_code=404, detail="task not found")
    return rec.to_public()


@router.post("/api/dispatch/{task_id}/input", dependencies=[Depends(_require_token)])
async def api_dispatch_input(task_id: str, req: InputRequest):
    d = td.get_dispatcher()
    if d.get(task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    if not await d.provide_input(task_id, req.answer):
        raise HTTPException(status_code=409, detail="task is not waiting for input")
    return d.get(task_id).to_public()


@router.get("/api/router/stats", dependencies=[Depends(_require_token)])
async def api_router_stats():
    r = get_router()
    await r.ensure_fresh_async()
    return {"router": r.stats(), "dispatcher": td.get_dispatcher().stats()}
