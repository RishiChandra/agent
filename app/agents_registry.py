"""Registry of developer-registered agents (the existing `agents` table).

The database already has an `agents` table used as the orchestrator's routing
registry:

    agent_id   uuid   primary key
    agent_info jsonb  { name, summary, keywords[], capabilities[], user_intents[], ... }
    agent_url  text   the WebSocket URL the orchestrator bridges to

This module is the single access layer over that table. It maps the developer-
facing fields the website collects onto the `agent_info` JSON:

    name         -> agent_info.name
    description  -> agent_info.summary
    url          -> agent_url
    "other recs" -> agent_info.keywords / capabilities / user_intents (+ any extras)

Plus a few operational keys we add: `service_id`, `source` ('web' | 'self'),
`active`, `version`. Rows that predate these keys (the original seed agents) are
treated as active web agents.

Two writers share the table:
  * The registration website via the CRUD routes in `routes/agent_routes.py`.
  * Self-registering relay services via `/developer/register` (see
    `orchestrator/BUILD_SERVICE_PROMPT.md`), through `upsert_registration`.

The orchestrator resolves which URL to dial with `resolve_bridge_url`, matching a
spoken agent name (or a service_id) against `agent_info`.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg2.extras import Json

from database import get_db_connection

log = logging.getLogger("agents_registry")

# ---------------------------------------------------------------------------
# Orchestrator v2 fields (ORCHESTRATOR_V2_TOOL_CALLS.md §1.3, §1.7). All live in
# agent_info. Registration (website or /developer/register) may set any of them;
# a heartbeat only overwrites the ones it sends.
# ---------------------------------------------------------------------------
_MODES = ("bridge", "task")
_TASK_OPS = ("dispatch", "status", "update", "cancel", "input", "close", "delivered")
_EVENTS = ("callback", "ws")
_POLICIES = ("owns_domain", "on_request")

# First-party routing data (decision M-c). Applied to an agent with this name
# when it has no routing fields of its own yet, including when it first registers.
FIRST_PARTY_ROUTING: dict[str, dict] = {
    "kairos": {
        "domains": ["tasks", "reminders", "schedule", "calendar", "to-do list"],
        "intent_aliases": ["my list", "my tasks", "my reminders", "remind me", "my schedule",
                           "my to do list", "what's on my list", "set a reminder"],
        "routing_policy": "owns_domain",
        "user_data": True,
        # Live calls on Gemini Live, background tasks via app/kairos_tasks.py.
        "modes": ["bridge", "task"],
        "task_ops": ["dispatch", "status", "cancel", "close", "delivered"],
        "events": ["callback"],
        "max_concurrency": 4,
        "side_effects": True,
    },
    "myfitnesspal": {
        "domains": ["nutrition", "food log", "calories", "diet"],
        "intent_aliases": ["calories", "macros", "what did i eat", "log my lunch", "log my breakfast",
                           "log my dinner", "how much protein", "food diary"],
        "routing_policy": "on_request",
        "user_data": True,
    },
}


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


def _clean_v2_fields(fields: dict) -> dict:
    """Validate and normalize the v2 fields present in `fields` (others untouched)."""
    out: dict[str, Any] = {}
    if "modes" in fields and fields["modes"] is not None:
        modes = [m.lower() for m in _str_list(fields["modes"]) if m.lower() in _MODES]
        out["modes"] = modes or ["bridge"]
    if "task_ops" in fields and fields["task_ops"] is not None:
        out["task_ops"] = [o.lower() for o in _str_list(fields["task_ops"]) if o.lower() in _TASK_OPS]
    if "events" in fields and fields["events"] is not None:
        out["events"] = [e.lower() for e in _str_list(fields["events"]) if e.lower() in _EVENTS]
    if "binding" in fields and fields["binding"]:
        out["binding"] = "ws" if str(fields["binding"]).lower() not in ("pull", "a2a") else str(fields["binding"]).lower()
    for key, lo, hi in (("max_concurrency", 1, 1000), ("max_reply_latency_s", 1, 30)):
        if key in fields and fields[key] is not None:
            try:
                out[key] = max(lo, min(hi, float(fields[key])))
                if key == "max_concurrency":
                    out[key] = int(out[key])
            except (TypeError, ValueError):
                pass
    if "default_deadline_s" in fields:
        try:
            v = fields["default_deadline_s"]
            out["default_deadline_s"] = float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            pass
    for key in ("side_effects", "user_data"):
        if key in fields and fields[key] is not None:
            out[key] = bool(fields[key])
    for key in ("domains", "intent_aliases"):
        if key in fields and fields[key] is not None:
            out[key] = _str_list(fields[key])
    if "routing_policy" in fields and fields["routing_policy"]:
        pol = str(fields["routing_policy"]).lower()
        out["routing_policy"] = pol if pol in _POLICIES else "on_request"
    if "slots" in fields and fields["slots"] is not None:
        slots = []
        for sl in fields["slots"] or []:
            if isinstance(sl, dict) and str(sl.get("name") or "").strip():
                slots.append({
                    "name": str(sl["name"]).strip(), "required": bool(sl.get("required")),
                    "description": str(sl.get("description") or ""), "question": str(sl.get("question") or ""),
                })
        out["slots"] = slots
    return out


V2_FIELDS = (
    "modes", "task_ops", "events", "binding", "max_concurrency", "max_reply_latency_s",
    "default_deadline_s", "side_effects", "user_data", "domains", "intent_aliases",
    "routing_policy", "slots",
)


def _apply_first_party(info: dict) -> None:
    key = "".join(ch for ch in str(info.get("name") or "").lower() if ch.isalnum())
    defaults = FIRST_PARTY_ROUTING.get(key)
    if not defaults:
        return
    for k, v in defaults.items():
        if not info.get(k) and k not in info.get("_owner_set", []):
            info[k] = v


def _routing_text_sha(info: dict) -> str:
    parts = [info.get("name") or "", info.get("summary") or ""]
    for k in ("domains", "intent_aliases", "user_intents", "keywords", "capabilities"):
        parts.append("|".join(str(v) for v in (info.get(k) or [])))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _after_write(agent_id: Optional[str] = None) -> None:
    """Invalidate the router snapshot and refresh the agent's embedding (background)."""
    try:
        from orchestrator.routing import router as agent_router

        agent_router.invalidate()
    except Exception:
        log.exception("router invalidate failed")
    if agent_id:
        threading.Thread(target=refresh_embedding, args=(agent_id,), daemon=True,
                         name=f"embed-{agent_id[:8]}").start()


def refresh_embedding(agent_id: str, *, force: bool = False) -> bool:
    """(Re)embed an agent's routing text if it changed. Safe to call from a thread."""
    try:
        from orchestrator.routing import embeddings
    except Exception:
        return False
    agent = get_agent(agent_id)
    if agent is None:
        return False
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        info = _get_info(cur, agent_id) or {}
        sha = _routing_text_sha(info)
        emb = embeddings.get_embedder()
        if emb is None:
            return False
        if (not force and info.get("routing_embedding") and info.get("routing_embedding_sha") == sha
                and info.get("routing_embedding_model") == emb.model):
            return False
        fields = embeddings.embed_agent(agent)
        if not fields:
            return False
        cur.execute(
            "UPDATE agents SET agent_info = coalesce(agent_info, '{}'::jsonb) || %s WHERE agent_id = %s",
            (Json({**fields, "routing_embedding_sha": sha}), agent_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("refresh_embedding failed agent_id=%s", agent_id)
        return False
    finally:
        conn.close()
    try:
        from orchestrator.routing import router as agent_router

        agent_router.invalidate()
    except Exception:
        pass
    return True


def backfill_routing_and_embeddings() -> int:
    """Apply first-party routing data and embed agents missing embeddings.

    Run once per deploy by deploy/app_backend/backfill_agent_routing.py (not at
    app startup). Safe to re-run: only missing fields and stale embeddings change.
    """
    changed = 0
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT agent_id, agent_info FROM agents")
        for agent_id, info in cur.fetchall():
            info = info or {}
            before = dict(info)
            _apply_first_party(info)
            if info != before:
                cur.execute("UPDATE agents SET agent_info = %s WHERE agent_id = %s", (Json(info), agent_id))
                changed += 1
        conn.commit()
    finally:
        conn.close()
    if changed:
        _after_write()
    embedded = 0
    for a in list_agents():
        if refresh_embedding(a["id"]):
            embedded += 1
    log.info("routing backfill: %d agents updated, %d embedded", changed, embedded)
    return changed + embedded


def _normalize(agent_id: Any, info: Optional[dict], url: Optional[str]) -> dict:
    """Flatten a DB row into the shape the website + system prompt consume."""
    info = info or {}
    active = info.get("active")
    return {
        "id": str(agent_id),
        "name": info.get("name") or "",
        "description": info.get("summary") or "",
        "url": url or "",
        "keywords": info.get("keywords") or [],
        "capabilities": info.get("capabilities") or [],
        "user_intents": info.get("user_intents") or [],
        "service_id": info.get("service_id"),
        "version": info.get("version") or "1",
        "source": info.get("source") or "web",
        # Missing/`true` -> active; only an explicit False deactivates.
        "active": active if isinstance(active, bool) else True,
        # Orchestrator v2 (see V2_FIELDS). Defaults match the protocol spec.
        "modes": info.get("modes") or ["bridge"],
        "task_ops": info.get("task_ops") or ["dispatch", "cancel", "input"],
        "events": info.get("events") or [],
        "binding": info.get("binding") or "ws",
        "max_concurrency": info.get("max_concurrency") or 8,
        "max_reply_latency_s": info.get("max_reply_latency_s"),
        "default_deadline_s": info.get("default_deadline_s"),
        # Unknown agents are treated as acting in the world (decision 2b).
        "side_effects": info.get("side_effects") if isinstance(info.get("side_effects"), bool) else True,
        "user_data": bool(info.get("user_data")),
        "domains": info.get("domains") or [],
        "intent_aliases": info.get("intent_aliases") or [],
        "routing_policy": info.get("routing_policy") or "on_request",
        "slots": info.get("slots") or [],
        "last_seen": info.get("last_seen"),
        "routing_embedding": info.get("routing_embedding") or [],
    }


def public_view(agent: dict) -> dict:
    """An agent row without the (large) embedding vector, for HTTP responses."""
    return {k: v for k, v in agent.items() if k != "routing_embedding"}


def _build_info(
    *,
    name: str,
    description: str = "",
    keywords: Optional[list] = None,
    capabilities: Optional[list] = None,
    user_intents: Optional[list] = None,
    service_id: Optional[str] = None,
    version: str = "1",
    source: str = "web",
    active: bool = True,
    extra: Optional[dict] = None,
) -> dict:
    """Assemble the `agent_info` JSON, keeping the existing seed-row key names."""
    info: dict[str, Any] = dict(extra or {})
    info.update(_clean_v2_fields(info))
    info.update({
        "name": name,
        "summary": description or "",
        "keywords": keywords or [],
        "capabilities": capabilities or [],
        "user_intents": user_intents or [],
        "version": version or "1",
        "source": source,
        "active": active,
    })
    if service_id:
        info["service_id"] = service_id
    _apply_first_party(info)
    return info


def list_agents(active_only: bool = False) -> list[dict]:
    """Return all agents (normalized). `active_only` hides deactivated ones."""
    where = "WHERE coalesce(agent_info->>'active', 'true') <> 'false'" if active_only else ""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            f"SELECT agent_id, agent_info, agent_url FROM agents {where} "
            "ORDER BY lower(coalesce(agent_info->>'name', '')), agent_id"
        )
        rows = cur.fetchall()
        return [_normalize(r[0], r[1], r[2]) for r in rows]
    finally:
        if conn:
            conn.close()


def get_agent(agent_id: str) -> Optional[dict]:
    """Fetch one agent by id (normalized), or None."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT agent_id, agent_info, agent_url FROM agents WHERE agent_id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        return _normalize(row[0], row[1], row[2]) if row else None
    finally:
        if conn:
            conn.close()


def _get_info(cur, agent_id: str) -> Optional[dict]:
    cur.execute("SELECT agent_info FROM agents WHERE agent_id = %s", (agent_id,))
    row = cur.fetchone()
    return (row[0] or {}) if row else None


def create_agent(
    *,
    name: str,
    url: str,
    description: str = "",
    keywords: Optional[list] = None,
    capabilities: Optional[list] = None,
    user_intents: Optional[list] = None,
    service_id: Optional[str] = None,
    version: str = "1",
    source: str = "web",
    extra: Optional[dict] = None,
) -> dict:
    """Insert a new agent and return the normalized row."""
    agent_id = uuid.uuid4()
    info = _build_info(
        name=name, description=description, keywords=keywords,
        capabilities=capabilities, user_intents=user_intents,
        service_id=service_id, version=version, source=source, extra=extra,
    )
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO agents (agent_id, agent_info, agent_url) VALUES (%s, %s, %s)",
            (str(agent_id), Json(info), url),
        )
        conn.commit()
        _after_write(str(agent_id))
        return _normalize(agent_id, info, url)
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


# Developer-facing fields that map into agent_info (vs the top-level url column).
_INFO_FIELDS = {
    "name": "name",
    "description": "summary",
    "keywords": "keywords",
    "capabilities": "capabilities",
    "user_intents": "user_intents",
    "service_id": "service_id",
    "version": "version",
    "active": "active",
}


def update_agent(agent_id: str, fields: dict[str, Any]) -> Optional[dict]:
    """Patch an agent. `url` updates `agent_url`; everything else merges into
    `agent_info`. Returns the normalized row, or None if not found.
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        info = _get_info(cur, agent_id)
        if info is None:
            return None

        for key, info_key in _INFO_FIELDS.items():
            if key in fields:
                info[info_key] = fields[key]
        v2 = _clean_v2_fields(fields)
        if v2:
            info.update(v2)
            # Remember what the owner set by hand so first-party defaults never override it.
            info["_owner_set"] = sorted(set(info.get("_owner_set", [])) | set(v2))

        sets = ["agent_info = %s"]
        values: list[Any] = [Json(info)]
        new_url = None
        if "url" in fields:
            sets.append("agent_url = %s")
            new_url = fields["url"]
            values.append(new_url)
        values.append(agent_id)

        cur.execute(
            f"UPDATE agents SET {', '.join(sets)} WHERE agent_id = %s RETURNING agent_url",
            tuple(values),
        )
        row = cur.fetchone()
        conn.commit()
        if row is None:
            return None
        _after_write(agent_id)
        return _normalize(agent_id, info, row[0])
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def delete_agent(agent_id: str) -> bool:
    """Hard-delete an agent. Returns True if a row was removed."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM agents WHERE agent_id = %s", (agent_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        if deleted:
            _after_write()
        return deleted
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def _find_by_service_id(cur, service_id: str) -> Optional[str]:
    cur.execute(
        "SELECT agent_id FROM agents WHERE agent_info->>'service_id' = %s LIMIT 1",
        (service_id,),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def upsert_registration(
    *,
    service_id: str,
    public_url: str,
    version: str = "1",
    name: Optional[str] = None,
    description: str = "",
    fields: Optional[dict] = None,
) -> dict:
    """Insert-or-update a self-registering service (`/developer/register`).

    Keyed on `agent_info.service_id`. Every call (including the 5-minute
    heartbeat) records `last_seen` and re-activates the row. Only the fields
    the caller sends are overwritten, so a heartbeat never wipes an edit made
    on the website. The router is invalidated (and the embedding refreshed)
    only when something routing-relevant changed, so heartbeats don't churn
    the snapshot.
    """
    fields = dict(fields or {})
    now = datetime.now(timezone.utc).isoformat()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        existing_id = _find_by_service_id(cur, service_id)
        if existing_id:
            info = _get_info(cur, existing_id) or {}
            before = {k: info.get(k) for k in (*V2_FIELDS, "name", "summary", "keywords",
                                               "capabilities", "user_intents", "active")}
            cur.execute("SELECT agent_url FROM agents WHERE agent_id = %s", (existing_id,))
            old_url = (cur.fetchone() or [None])[0]
            info["service_id"] = service_id
            info["version"] = version or "1"
            info["active"] = True
            info["last_seen"] = now
            info.setdefault("source", "self")
            info.setdefault("name", name or service_id)
            if name:
                info["name"] = name
            if description:
                info["summary"] = description
            for key in ("keywords", "capabilities", "user_intents"):
                if fields.get(key) is not None:
                    info[key] = _str_list(fields[key])
            owner_set = set(info.get("_owner_set", []))
            for k, v in _clean_v2_fields(fields).items():
                if k not in owner_set:  # website edits win over self-reported values
                    info[k] = v
            _apply_first_party(info)
            cur.execute(
                "UPDATE agents SET agent_info = %s, agent_url = %s WHERE agent_id = %s",
                (Json(info), public_url, existing_id),
            )
            conn.commit()
            after = {k: info.get(k) for k in before}
            if after != before or old_url != public_url:
                _after_write(existing_id)
            return _normalize(existing_id, info, public_url)

        agent_id = uuid.uuid4()
        extra = {k: fields[k] for k in V2_FIELDS if k in fields}
        info = _build_info(
            name=name or service_id, description=description,
            keywords=_str_list(fields.get("keywords")),
            capabilities=_str_list(fields.get("capabilities")),
            user_intents=_str_list(fields.get("user_intents")),
            service_id=service_id, version=version, source="self", extra=extra,
        )
        info["last_seen"] = now
        cur.execute(
            "INSERT INTO agents (agent_id, agent_info, agent_url) VALUES (%s, %s, %s)",
            (str(agent_id), Json(info), public_url),
        )
        conn.commit()
        _after_write(str(agent_id))
        return _normalize(agent_id, info, public_url)
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def deactivate_registration(service_id: str) -> bool:
    """Mark a self-registered service inactive (`/developer/unregister`).

    Returns True if a matching row was flipped. We deactivate rather than delete
    so registration history (and any web-side edits) survive a service restart.
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        agent_id = _find_by_service_id(cur, service_id)
        if not agent_id:
            return False
        info = _get_info(cur, agent_id) or {}
        info["active"] = False
        cur.execute(
            "UPDATE agents SET agent_info = %s WHERE agent_id = %s",
            (Json(info), agent_id),
        )
        conn.commit()
        _after_write()
        return True
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def resolve_bridge_url(selector: str) -> Optional[str]:
    """Resolve a spoken agent name (or service_id) to its bridge URL, or None.

    Used by the service-ping path. Goes through the in-memory router (no table
    scan) and only returns a confident match; there is no default-agent
    fallback for names (decision A3).
    """
    sel = (selector or "").strip()
    if not sel:
        return None
    from orchestrator.tasks.protocol import MODE_BRIDGE
    from orchestrator.routing.router import get_router

    res = get_router().resolve(sel, mode=MODE_BRIDGE)
    if res.matched and res.best is not None:
        return res.best.url
    return None
