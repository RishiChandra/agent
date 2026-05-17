"""HTTP routes for the developer_ws subsystem.

Three POST endpoints mounted by `app/main.py` via `app.include_router(router)`:

- `/developer/ping/{user_id}` — external service asks main to initiate the bridge
  back to it. Looks up the live WS session via `registry.get(user_id)` and calls
  `pipeline.on_service_ping(service_id=...)`.
- `/developer/register`        — external service publishes its current dial URL
  (e.g. cloudflared `*.trycloudflare.com`); writes it straight to the `Agents`
  table (matching `agent_id = service_id`). New sessions pick it up on next
  Gemini connect since the session-start DB read is the source of truth.
- `/developer/unregister`      — external service clears its agent_url in the DB
  on shutdown.

See `developer_ws/BUILD_SERVICE_PROMPT_V2.md` for the full contract callers use.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body

from . import registry
from .queries import clear_agent_url, set_agent_url

log = logging.getLogger("developer_ws")

router = APIRouter()


@router.post("/developer/ping/{user_id}")
async def developer_ping(user_id: str, payload: dict | None = Body(default=None)):
    """Service-initiated call hook.

    Called by: any external service that wants main to initiate the bridge back
    to it. The reference caller is `_ping_main` in
    `developer_ws/testing/echo_server.py`, but any HTTP client can hit this
    route. See `developer_ws/BRIDGE_PROTOCOL.md`.

    Flow:
      1. Read `service_id` + `version` from the JSON body (if any).
      2. Look up the live session via `registry.get(user_id)`.
      3. If a session exists, call `pipeline.on_service_ping(service_id=...)`
         which speaks the announcement and dials the bridge.
    """
    body = payload or {}
    service_id = str(body.get("service_id", "unknown"))
    caller_version = str(body.get("version", "?"))
    log.info(
        "ping received user_id=%s service_id=%s caller_version=%s",
        user_id, service_id, caller_version,
    )
    pipeline = registry.get(user_id)
    if pipeline is None:
        log.info(
            "ping rejected: no active session user_id=%s service_id=%s",
            user_id, service_id,
        )
        return {
            "ok": False,
            "reason": "no active session",
            "user_id": user_id,
            "service_id": service_id,
        }
    ok = await pipeline.on_service_ping(service_id=service_id)
    return {"ok": ok, "user_id": user_id, "service_id": service_id}


@router.post("/developer/register")
async def developer_register(payload: dict | None = Body(default=None)):
    """Persist a service's current WebSocket URL into the Agents table.

    Called by: external services after they discover their public tunnel URL
    (e.g. cloudflared `*.trycloudflare.com`). See
    `developer_ws/BUILD_SERVICE_PROMPT_V2.md` for the contract.

    Body: `{"service_id": "...", "public_url": "wss://.../relay", "version": "1"}`
    The incoming `service_id` is treated as the `agent_id`: this updates the
    matching row in Agents. Returns `ok: false` with `reason: "unknown service_id"`
    when no row matches — registration must precede session start, so a no-op
    UPDATE is a configuration error.
    """
    body = payload or {}
    service_id = str(body.get("service_id", "")).strip()
    public_url = str(body.get("public_url", "")).strip()
    caller_version = str(body.get("version", "?"))
    log.info(
        "register received service_id=%s public_url=%s caller_version=%s",
        service_id or "(missing)", public_url or "(missing)", caller_version,
    )
    if not service_id:
        return {"ok": False, "reason": "missing service_id"}
    if not public_url:
        return {"ok": False, "reason": "missing public_url"}
    try:
        rows = await asyncio.to_thread(set_agent_url, service_id, public_url)
    except Exception as e:
        log.warning("register DB update failed service_id=%s: %s", service_id, e)
        return {"ok": False, "reason": "database error"}
    if rows == 0:
        log.warning("register: no Agents row matches service_id=%s", service_id)
        return {"ok": False, "reason": "unknown service_id"}
    log.info("register applied service_id=%s rows=%d", service_id, rows)
    return {"ok": True, "service_id": service_id}


@router.post("/developer/unregister")
async def developer_unregister(payload: dict | None = Body(default=None)):
    """Clear a service's agent_url in the Agents table on shutdown.

    Called by: external services on graceful shutdown (SIGTERM / SIGINT /
    atexit). Body: `{"service_id": "..."}`. Best-effort: returns `ok: true`
    even when no row matches, so callers don't need to track state to call
    this safely.
    """
    body = payload or {}
    service_id = str(body.get("service_id", "")).strip()
    log.info(
        "unregister received service_id=%s",
        service_id or "(missing)",
    )
    if not service_id:
        return {"ok": False, "reason": "missing service_id"}
    try:
        rows = await asyncio.to_thread(clear_agent_url, service_id)
    except Exception as e:
        log.warning("unregister DB update failed service_id=%s: %s", service_id, e)
        return {"ok": False, "reason": "database error"}
    log.info("unregister applied service_id=%s rows=%d", service_id, rows)
    return {"ok": True, "service_id": service_id, "existed": rows > 0}
