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
`active`, `version`. Rows that predate these keys (the original seed agents) are
treated as active web agents.

Two writers share the table:
  * The registration website via the CRUD routes in `routes/agent_routes.py`.
  * Self-registering relay services via `/developer/register` (see
    `developer_ws/BUILD_SERVICE_PROMPT.md`), through `upsert_registration`.

The orchestrator resolves which URL to dial with `resolve_bridge_url`, matching a
spoken agent name (or a service_id) against `agent_info`.
"""

from __future__ import annotations

import logging
import uuid
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
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(ddl)
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
    }


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
    return info


def list_agents(active_only: bool = False) -> list[dict]:
    """Return all agents (normalized). `active_only` hides deactivated ones."""
    where = "WHERE coalesce(agent_info->>'active', 'true') <> 'false'" if active_only else ""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT agent_id, agent_info, agent_url FROM agents {where}")
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
) -> dict:
    """Insert-or-update a self-registering service's mapping (`/developer/register`).

    Keyed on `agent_info.service_id`: if a matching row exists, its url is
    refreshed and it is re-activated; otherwise a new `source='self'` row is
    created. `name` defaults to `service_id` so the agent stays routable by name.
    """
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
            info.setdefault("source", "self")
            info.setdefault("name", name or service_id)
            if description:
                info["summary"] = description
            cur.execute(
                "UPDATE agents SET agent_info = %s, agent_url = %s WHERE agent_id = %s",
                (Json(info), public_url, existing_id),
            )
            conn.commit()
            return _normalize(existing_id, info, public_url)

        agent_id = uuid.uuid4()
        info = _build_info(
            name=name or service_id, description=description,
            service_id=service_id, version=version, source="self",
        )
        cur.execute(
            "INSERT INTO agents (agent_id, agent_info, agent_url) VALUES (%s, %s, %s)",
            (str(agent_id), Json(info), public_url),
        )
        conn.commit()
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

    Active agents only. Matching order: exact service_id, then exact name
    (case-insensitive), then name prefix, then fuzzy similarity — spoken names
    reach us through STT, which garbles out-of-vocabulary words ("Kairos" →
    "cut in"), so a near-miss should still route rather than fall through.
    Returns None if nothing matches — the caller then falls back to the single
    env-configured bridge URL.
    """
    sel = (selector or "").strip()
    if not sel:
        return None
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT agent_url FROM agents
            WHERE coalesce(agent_info->>'active', 'true') <> 'false'
              AND (
                    agent_info->>'service_id' = %(sel)s
                 OR lower(agent_info->>'name') = lower(%(sel)s)
                 OR lower(agent_info->>'name') LIKE lower(%(sel)s) || '%%'
              )
            ORDER BY
                (agent_info->>'service_id' = %(sel)s) DESC,
                (lower(agent_info->>'name') = lower(%(sel)s)) DESC
            LIMIT 1
            """,
            {"sel": sel},
        )
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
        return _resolve_fuzzy(cur, sel)
    except Exception:
        log.exception("resolve_bridge_url failed for %r", sel)
        return None
    finally:
        if conn:
            conn.close()


def _resolve_fuzzy(cur, sel: str) -> Optional[str]:
    """Best fuzzy match of `sel` against active agent names (STT-garble tolerance)."""
    import difflib

    cur.execute(
        """
        SELECT agent_info->>'name', agent_url FROM agents
        WHERE coalesce(agent_info->>'active', 'true') <> 'false'
          AND agent_info->>'name' IS NOT NULL AND agent_url IS NOT NULL
        """
    )
    rows = cur.fetchall()
    sel_l = sel.lower()
    best_url, best_score = None, 0.0
    for name, url in rows:
        score = difflib.SequenceMatcher(None, sel_l, (name or "").lower()).ratio()
        if score > best_score:
            best_url, best_score = url, score
    # 0.5 is deliberately forgiving: the selector only exists because the LLM
    # decided the user named *some* agent, so the best candidate beats falling
    # back to the env default. Still require a floor so junk doesn't match.
    if best_url and best_score >= 0.5:
        log.info("fuzzy agent match %r (score=%.2f)", sel, best_score)
        return best_url
    return None
