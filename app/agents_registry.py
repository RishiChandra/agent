"""Registry of developer-registered agents (the existing `agents` table).

The database already has an `agents` table used as the orchestrator's routing
registry:

    agent_id   uuid   primary key
    agent_info json   { name, summary, keywords[], capabilities[], user_intents[], ... }
    agent_url  text   the WebSocket URL the orchestrator bridges to

This module is the single access layer over that table. It maps the developer-
facing fields the website collects onto the `agent_info` JSON:

    name         -> agent_info.name
    description  -> agent_info.summary
    url          -> agent_url
    "other recs" -> agent_info.keywords / capabilities / user_intents (+ any extras)

Plus a few operational keys we add: `service_id`, `source` ('web' | 'self'),
`active`, `version`, and (protocol v2) `modes` (['bridge', 'task']),
`max_concurrency`, `last_seen`. Rows that predate these keys (the original seed
agents) are treated as active, bridge-only web agents.

Every write calls `agent_router.invalidate()` so the in-memory routing snapshot
reloads on its next lookup instead of waiting out the refresh interval.

Two writers share the table:
  * The registration website via the CRUD routes in `routes/agent_routes.py`.
  * Self-registering relay services via `/developer/register` (see
    `developer_ws/BUILD_SERVICE_PROMPT.md`), through `upsert_registration`.

The orchestrator resolves which agent to dial through `agent_router` (an
in-memory index over this table); `resolve_bridge_url` here is the back-compat
shim onto it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg2.extras import Json

from database import get_db_connection

log = logging.getLogger("agents_registry")


def ensure_agents_table() -> None:
    """Create the `agents` table if it doesn't exist. Idempotent, and a no-op
    against the existing production table (same shape).

    Called at app startup (lifespan in `main.py`) so a fresh database works
    without a manual migration. Also runnable via `scripts/create_agents_table.py`.
    """
    ddl = """
    CREATE TABLE IF NOT EXISTS agents (
        agent_id  uuid PRIMARY KEY,
        agent_info json,
        agent_url text
    );
    """
    # Expression indexes so the hot lookups (service_id upsert on every
    # heartbeat, name resolution, active filter) stay O(log n) at 1000+ rows.
    # `agent_info` is plain `json` (not jsonb) on the production table, so a
    # GIN index isn't available; btree over the extracted text is enough.
    index_ddl = [
        "CREATE INDEX IF NOT EXISTS agents_service_id_idx ON agents ((agent_info->>'service_id'))",
        "CREATE INDEX IF NOT EXISTS agents_name_lower_idx ON agents (lower(agent_info->>'name'))",
        "CREATE INDEX IF NOT EXISTS agents_active_idx ON agents ((coalesce(agent_info->>'active', 'true')))",
    ]
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(ddl)
        for stmt in index_ddl:
            cur.execute(stmt)
        conn.commit()
        cur.close()
        log.info("agents table ensured")
    except Exception:
        if conn:
            conn.rollback()
        log.exception("ensure_agents_table failed")
        raise
    finally:
        if conn:
            conn.close()


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
        # Protocol v2: which orchestrator modes the agent accepts. Rows that
        # predate the field are live-audio ("bridge") agents.
        "modes": _normalize_modes(info.get("modes")),
        "max_concurrency": _normalize_int(info.get("max_concurrency"), default=8, lo=1, hi=1000),
        "last_seen": info.get("last_seen"),
    }


VALID_MODES = ("bridge", "task")


def _normalize_modes(modes: Any) -> list[str]:
    if isinstance(modes, str):
        modes = [m for m in modes.replace(",", " ").split()]
    if not isinstance(modes, (list, tuple)):
        return ["bridge"]
    out = [str(m).strip().lower() for m in modes if str(m).strip()]
    out = [m for m in out if m in VALID_MODES]
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(out)) or ["bridge"]


def _normalize_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


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
    modes: Optional[list] = None,
    max_concurrency: Optional[int] = None,
) -> dict:
    """Assemble the `agent_info` JSON, keeping the existing seed-row key names."""
    info: dict[str, Any] = dict(extra or {})
    info.update({
        "name": name,
        "summary": description or "",
        "keywords": keywords or [],
        "capabilities": capabilities or [],
        "user_intents": user_intents or [],
        "version": version or "1",
        "source": source,
        "active": active,
        "modes": _normalize_modes(modes),
    })
    if max_concurrency is not None:
        info["max_concurrency"] = _normalize_int(max_concurrency, default=8, lo=1, hi=1000)
    if service_id:
        info["service_id"] = service_id
    return info


def list_agents(
    active_only: bool = False,
    *,
    limit: Optional[int] = None,
    offset: int = 0,
    q: Optional[str] = None,
) -> list[dict]:
    """Return agents (normalized), newest-registered last.

    `active_only` hides deactivated ones. `limit`/`offset` page the result and
    `q` does a cheap case-insensitive substring filter over name/summary/
    service_id for the website's search box. Intent-aware ranking lives in
    `agent_router`, not here — this is the plain listing.
    """
    clauses = []
    params: dict[str, Any] = {}
    if active_only:
        clauses.append("coalesce(agent_info->>'active', 'true') <> 'false'")
    if q and q.strip():
        params["q"] = f"%{q.strip().lower()}%"
        clauses.append(
            "(lower(agent_info->>'name') LIKE %(q)s"
            " OR lower(agent_info->>'summary') LIKE %(q)s"
            " OR lower(agent_info->>'service_id') LIKE %(q)s)"
        )
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    page = ""
    if limit is not None:
        params["limit"] = max(0, int(limit))
        params["offset"] = max(0, int(offset))
        page = "LIMIT %(limit)s OFFSET %(offset)s"
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            f"SELECT agent_id, agent_info, agent_url FROM agents {where} "
            f"ORDER BY lower(agent_info->>'name'), agent_id {page}",
            params,
        )
        rows = cur.fetchall()
        return [_normalize(r[0], r[1], r[2]) for r in rows]
    finally:
        if conn:
            conn.close()


def count_agents(active_only: bool = False) -> int:
    """Total rows (for pagination headers / the router stats endpoint)."""
    where = "WHERE coalesce(agent_info->>'active', 'true') <> 'false'" if active_only else ""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT count(*) FROM agents {where}")
        row = cur.fetchone()
        return int(row[0]) if row else 0
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
    modes: Optional[list] = None,
    max_concurrency: Optional[int] = None,
) -> dict:
    """Insert a new agent and return the normalized row."""
    agent_id = uuid.uuid4()
    info = _build_info(
        name=name, description=description, keywords=keywords,
        capabilities=capabilities, user_intents=user_intents,
        service_id=service_id, version=version, source=source, extra=extra,
        modes=modes, max_concurrency=max_concurrency,
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
        _router_invalidate()
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
    "modes": "modes",
    "max_concurrency": "max_concurrency",
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
        if "modes" in fields:
            info["modes"] = _normalize_modes(fields["modes"])
        if "max_concurrency" in fields:
            info["max_concurrency"] = _normalize_int(
                fields["max_concurrency"], default=8, lo=1, hi=1000
            )

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
        _router_invalidate()
        if row is None:
            return None
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
        _router_invalidate()
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
    modes: Optional[list] = None,
    max_concurrency: Optional[int] = None,
    keywords: Optional[list] = None,
    capabilities: Optional[list] = None,
    user_intents: Optional[list] = None,
) -> dict:
    """Insert-or-update a self-registering service's mapping (`/developer/register`).

    Keyed on `agent_info.service_id`: if a matching row exists, its url is
    refreshed and it is re-activated; otherwise a new `source='self'` row is
    created. `name` defaults to `service_id` so the agent stays routable by name.
    Protocol v2 services also declare `modes` (bridge/task) and routing hints;
    only fields the caller actually sends are overwritten, so a web-side edit
    survives a heartbeat that omits them.
    """
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        existing_id = _find_by_service_id(cur, service_id)
        if existing_id:
            info = _get_info(cur, existing_id) or {}
            info["service_id"] = service_id
            info["version"] = version or "1"
            info["active"] = True
            info["last_seen"] = now_iso
            info.setdefault("source", "self")
            info.setdefault("name", name or service_id)
            if description:
                info["summary"] = description
            if modes is not None:
                info["modes"] = _normalize_modes(modes)
            if max_concurrency is not None:
                info["max_concurrency"] = _normalize_int(max_concurrency, default=8, lo=1, hi=1000)
            if keywords is not None:
                info["keywords"] = list(keywords)
            if capabilities is not None:
                info["capabilities"] = list(capabilities)
            if user_intents is not None:
                info["user_intents"] = list(user_intents)
            cur.execute(
                "UPDATE agents SET agent_info = %s, agent_url = %s WHERE agent_id = %s",
                (Json(info), public_url, existing_id),
            )
            conn.commit()
            _router_invalidate()
            return _normalize(existing_id, info, public_url)

        agent_id = uuid.uuid4()
        info = _build_info(
            name=name or service_id, description=description,
            service_id=service_id, version=version, source="self",
            modes=modes, max_concurrency=max_concurrency,
            keywords=keywords, capabilities=capabilities, user_intents=user_intents,
        )
        info["last_seen"] = now_iso
        cur.execute(
            "INSERT INTO agents (agent_id, agent_info, agent_url) VALUES (%s, %s, %s)",
            (str(agent_id), Json(info), public_url),
        )
        conn.commit()
        _router_invalidate()
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
        _router_invalidate()
        return True
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def resolve_bridge_url(selector: str) -> Optional[str]:
    """Resolve a spoken agent name (or a service_id) to its bridge URL.

    Thin back-compat wrapper over `agent_router` (in-memory snapshot, phonetic +
    fuzzy matching, health-aware). Returns None if nothing matches confidently —
    the caller then falls back to the single env-configured bridge URL. Callers
    that want to *ask the user* on ambiguity should use
    `agent_router.get_router().resolve(...)` directly.
    """
    sel = (selector or "").strip()
    if not sel:
        return None
    try:
        from agent_router import get_router

        return get_router().resolve_url(sel)
    except Exception:
        log.exception("resolve_bridge_url failed for %r", sel)
        return None


def _router_invalidate() -> None:
    """Tell the in-memory router its snapshot is stale. Never raises."""
    try:
        from agent_router import invalidate

        invalidate()
    except Exception:
        log.debug("router invalidate skipped", exc_info=True)
