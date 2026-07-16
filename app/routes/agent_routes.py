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
self-registers are routable the same way by `agents_registry.resolve_bridge_url`.
"""

import logging
import traceback
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import agents_registry

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


# ===== Self-registration models (BUILD_SERVICE_PROMPT contract) =====
class RegisterRequest(BaseModel):
    service_id: str
    public_url: str
    version: str = "1"
    # Optional niceties so a self-registered service shows up well on the site.
    name: Optional[str] = None
    description: Optional[str] = None


class UnregisterRequest(BaseModel):
    service_id: str


# ---------------------------------------------------------------------------
# Website CRUD  (/api/agents)
# ---------------------------------------------------------------------------
@router.get("/api/agents")
def api_list_agents(active_only: bool = False):
    """List registered agents (newest first). `?active_only=true` hides inactive ones."""
    try:
        return {"agents": agents_registry.list_agents(active_only=active_only)}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"failed to list agents: {e}")


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
    log.info(
        "register service_id=%s public_url=%s version=%s",
        service_id, public_url, req.version,
    )
    try:
        agents_registry.upsert_registration(
            service_id=service_id,
            public_url=public_url,
            version=(req.version or "1"),
            name=(req.name or None),
            description=(req.description or ""),
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
