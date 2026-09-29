"""HTTP API for the developer agent registry.

Two audiences share one `agents` table (see `agents_registry.py`):

  * **The registration website** uses the CRUD routes under `/api/agents` to let a
    developer add/list/edit/delete their agents (name, description, url, extras).
    Open (no auth) for v1.

  * **Self-registering services** use `/developer/register` + `/developer/unregister`
    per `developer_ws/BUILD_SERVICE_PROMPT.md`. A running relay service POSTs its
    current `public_url` on startup; the orchestrator later dials whatever URL is
    registered for that `service_id`.

Both write to the same store, so an agent added on the website and one that
self-registers are routable the same way through `agent_router`.

Protocol v2 adds `modes` (["bridge"], ["task"] or both) and `max_concurrency`
to both write paths, plus `/api/agents/search` (ranked, intent-aware lookup
backed by the in-memory router) and paging on `/api/agents`.
"""

import logging
import traceback
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

import agents_registry
from agent_router import MODE_BRIDGE, MODE_TASK, get_router

log = logging.getLogger("agents_registry")

router = APIRouter()


# ===== Website CRUD models =====
# Maps onto the `agents` table: name/description/recs -> agent_info JSON, url -> agent_url.
class AgentCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    url: str = Field(..., min_length=1, max_length=2000)
    description: str = ""  # -> agent_info.summary
    # "Other recs" the orchestrator uses to route to this agent:
    keywords: List[str] = []
    capabilities: List[str] = []
    user_intents: List[str] = []
    service_id: Optional[str] = None
    version: str = "1"
    extra: Optional[dict] = None  # any additional free-form agent_info keys
    # Protocol v2: live audio ("bridge"), background tasks ("task"), or both.
    modes: List[str] = [MODE_BRIDGE]
    max_concurrency: Optional[int] = Field(default=None, ge=1, le=1000)

    @field_validator("modes")
    @classmethod
    def _check_modes(cls, v: List[str]) -> List[str]:
        return _validate_modes(v)


def _validate_modes(v: Optional[List[str]]) -> List[str]:
    modes = [str(m).strip().lower() for m in (v or []) if str(m).strip()]
    bad = [m for m in modes if m not in (MODE_BRIDGE, MODE_TASK)]
    if bad:
        raise ValueError(f"unknown mode(s) {bad}; allowed: bridge, task")
    return list(dict.fromkeys(modes)) or [MODE_BRIDGE]


class AgentUpdateRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    keywords: Optional[List[str]] = None
    capabilities: Optional[List[str]] = None
    user_intents: Optional[List[str]] = None
    service_id: Optional[str] = None
    version: Optional[str] = None
    active: Optional[bool] = None
    modes: Optional[List[str]] = None
    max_concurrency: Optional[int] = Field(default=None, ge=1, le=1000)

    @field_validator("modes")
    @classmethod
    def _check_modes(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return None if v is None else _validate_modes(v)


# ===== Self-registration models (BUILD_SERVICE_PROMPT contract) =====
class RegisterRequest(BaseModel):
    service_id: str
    public_url: str
    version: str = "1"
    # Optional niceties so a self-registered service shows up well on the site.
    name: Optional[str] = None
    description: Optional[str] = None
    # Protocol v2 (all optional; omitted fields leave stored values untouched).
    modes: Optional[List[str]] = None
    max_concurrency: Optional[int] = None
    keywords: Optional[List[str]] = None
    capabilities: Optional[List[str]] = None
    user_intents: Optional[List[str]] = None


class UnregisterRequest(BaseModel):
    service_id: str


# ---------------------------------------------------------------------------
# Website CRUD  (/api/agents)
# ---------------------------------------------------------------------------
@router.get("/api/agents")
def api_list_agents(
    active_only: bool = False,
    limit: Optional[int] = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    q: Optional[str] = None,
):
    """List registered agents, sorted by name.

    `?active_only=true` hides inactive ones. `limit`/`offset` page the list and
    `q` filters by name/description/service_id substring. Without `limit` the
    full list is returned (back-compat for the website).
    """
    try:
        agents = agents_registry.list_agents(
            active_only=active_only, limit=limit, offset=offset, q=q,
        )
        body = {"agents": agents}
        if limit is not None:
            body["total"] = agents_registry.count_agents(active_only=active_only) if not q else None
            body["limit"] = limit
            body["offset"] = offset
        return body
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"failed to list agents: {e}")


@router.get("/api/agents/search")
async def api_search_agents(
    q: str = Query(..., min_length=1, max_length=500),
    mode: Optional[str] = Query(default=None, pattern="^(bridge|task)$"),
    k: int = Query(default=5, ge=1, le=25),
):
    """Ranked agent lookup (name, STT-garbled name, or intent) via the router.

    Declared before `/api/agents/{agent_id}` so "search" isn't taken as an id.
    """
    router_ = get_router()
    await router_.ensure_fresh_async()
    return {"query": q, "results": [c.to_public() for c in router_.search(q, mode=mode, k=k)]}


@router.get("/api/agents/{agent_id}")
def api_get_agent(agent_id: str):
    agent = agents_registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent


@router.post("/api/agents", status_code=201)
def api_create_agent(req: AgentCreateRequest):
    try:
        return agents_registry.create_agent(
            name=req.name.strip(),
            url=req.url.strip(),
            description=(req.description or "").strip(),
            keywords=[k.strip() for k in req.keywords if k.strip()],
            capabilities=[c.strip() for c in req.capabilities if c.strip()],
            user_intents=[u.strip() for u in req.user_intents if u.strip()],
            service_id=(req.service_id.strip() if req.service_id else None),
            version=(req.version or "1").strip(),
            extra=req.extra or {},
            source="web",
            modes=req.modes,
            max_concurrency=req.max_concurrency,
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"failed to create agent: {e}")


@router.put("/api/agents/{agent_id}")
def api_update_agent(agent_id: str, req: AgentUpdateRequest):
    fields = req.model_dump(exclude_unset=True)
    # Trim provided string fields.
    for k in ("name", "url", "description", "version", "service_id"):
        if k in fields and isinstance(fields[k], str):
            fields[k] = fields[k].strip()
    try:
        updated = agents_registry.update_agent(agent_id, fields)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"failed to update agent: {e}")
    if updated is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return updated


@router.delete("/api/agents/{agent_id}")
def api_delete_agent(agent_id: str):
    try:
        deleted = agents_registry.delete_agent(agent_id)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"failed to delete agent: {e}")
    if not deleted:
        raise HTTPException(status_code=404, detail="agent not found")
    return {"ok": True, "id": agent_id}


# ---------------------------------------------------------------------------
# Self-registration  (/developer/register, /developer/unregister)
# ---------------------------------------------------------------------------
@router.post("/developer/register")
def developer_register(req: RegisterRequest):
    """Store/refresh a self-registering service's `service_id -> public_url` mapping.

    Contract per BUILD_SERVICE_PROMPT.md: respond `{"ok": true, "service_id": ...}`
    on success, or `{"ok": false, "reason": ...}` on failure (services exit non-zero
    on a false response, so keep the reason human-readable).
    """
    service_id = (req.service_id or "").strip()
    public_url = (req.public_url or "").strip()
    if not service_id or not public_url:
        return {"ok": False, "reason": "service_id and public_url are required"}
    modes = None
    if req.modes is not None:
        try:
            modes = _validate_modes(req.modes)
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
    log.info(
        "register service_id=%s public_url=%s version=%s modes=%s",
        service_id, public_url, req.version, modes,
    )
    try:
        agents_registry.upsert_registration(
            service_id=service_id,
            public_url=public_url,
            version=(req.version or "1"),
            name=(req.name or None),
            description=(req.description or ""),
            modes=modes,
            max_concurrency=req.max_concurrency,
            keywords=req.keywords,
            capabilities=req.capabilities,
            user_intents=req.user_intents,
        )
        return {"ok": True, "service_id": service_id}
    except Exception as e:
        traceback.print_exc()
        log.warning("register failed service_id=%s err=%s", service_id, e)
        return {"ok": False, "reason": f"registration failed: {e}"}


@router.post("/developer/unregister")
def developer_unregister(req: UnregisterRequest):
    """Mark a self-registered service inactive on graceful shutdown. Best-effort."""
    service_id = (req.service_id or "").strip()
    if not service_id:
        return {"ok": False, "reason": "service_id is required"}
    log.info("unregister service_id=%s", service_id)
    try:
        changed = agents_registry.deactivate_registration(service_id)
        return {"ok": True, "service_id": service_id, "changed": changed}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "reason": f"unregister failed: {e}"}
