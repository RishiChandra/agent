"""Database queries scoped to the developer_ws subsystem.

Each function is a blocking psycopg2 call; callers should run them via
`asyncio.to_thread` from inside the WebSocket event loop.
"""

from __future__ import annotations

from database import execute_query, execute_update


def fetch_user_agents(user_id: str) -> list[dict]:
    """Two-step lookup: Agent_Registry → Agents.

    1. SELECT agent_id FROM Agent_Registry WHERE user_id = %s
    2. SELECT * FROM Agents WHERE agent_id IN (...)

    The `SELECT *` in step 2 picks up agent_url along with the rest of agent_info,
    since agent_url lives on the Agents table. Empty list if user has no registered
    agents.
    """
    registry_rows = execute_query(
        "SELECT agent_id FROM Agent_Registry WHERE user_id = %s",
        (user_id,),
    )
    agent_ids = [r["agent_id"] for r in registry_rows if r.get("agent_id") is not None]
    if not agent_ids:
        return []
    placeholders = ",".join(["%s"] * len(agent_ids))
    return execute_query(
        f"SELECT * FROM Agents WHERE agent_id IN ({placeholders})",
        tuple(agent_ids),
    )


def set_agent_url(agent_id: str, agent_url: str) -> int:
    """Update the agent_url for an existing Agents row. Returns rowcount.

    Called by: `routes.developer_register` when a service publishes its current
    tunnel URL. The service's `service_id` is treated as the `agent_id` — the
    matching row in Agents is updated in place. Returns 0 if no row matches,
    which the caller should surface as `ok: false`.
    """
    return execute_update(
        "UPDATE Agents SET agent_url = %s WHERE agent_id = %s",
        (agent_url, agent_id),
    )


def clear_agent_url(agent_id: str) -> int:
    """Null out the agent_url for an Agents row. Returns rowcount.

    Called by: `routes.developer_unregister` on graceful service shutdown.
    Idempotent — returns 0 if no row matches (caller treats that as already-clean).
    """
    return execute_update(
        "UPDATE Agents SET agent_url = NULL WHERE agent_id = %s",
        (agent_id,),
    )
