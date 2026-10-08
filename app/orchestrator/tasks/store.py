"""Postgres access for background tasks, the agent outbox and jobs.

Agent tasks are rows in the shared `tasks` table with `kind = 'agent_task'`
(ORCHESTRATOR_V2_TOOL_CALLS.md §1.5). The schema, including `agent_outbox`,
comes from the migrations in deploy/sql/ (deploy/migrate.sh); this module only
reads and writes rows. Everything is synchronous psycopg2; async callers use
`asyncio.to_thread`.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from psycopg2.extras import Json, RealDictCursor

log = logging.getLogger("task_store")


_TASK_COLS = (
    "task_id, user_id, task_info, status, kind, created_by, agent_id, notify, question, result, "
    "deadline_at, finished_at, delivered_at, delivered_via, agent_informed_at, created_at, updated_at"
)
_ACTIVE = ("pending", "dispatching", "running", "input_required")
_TERMINAL = ("completed", "failed", "cancelled", "timed_out")


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def _connect():
    from database import get_db_connection  # app/database.py; imported lazily for tests

    return get_db_connection()


class TaskStore:
    def __init__(self, connect=None) -> None:
        self._connect = connect or _connect

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    yield cur
        finally:
            conn.close()

    # ----- tasks ------------------------------------------------------------

    @staticmethod
    def _row(row: Optional[dict]) -> Optional[dict]:
        if row is None:
            return None
        d = dict(row)
        for k in ("task_id", "user_id", "agent_id"):
            if d.get(k) is not None:
                d[k] = str(d[k])
        d["task_info"] = d.get("task_info") or {}
        return d

    def create_agent_task(
        self,
        *,
        task_id: str,
        user_id: str,
        agent_id: str,
        task_info: dict,
        notify: str,
        deadline_at: Optional[datetime] = None,
        created_by: str = "orchestrator",
        status: str = "pending",
    ) -> dict:
        with self._cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO tasks (task_id, user_id, task_info, status, kind, created_by,
                                   agent_id, notify, deadline_at)
                VALUES (%s, %s, %s, %s, 'agent_task', %s, %s, %s, %s)
                RETURNING {_TASK_COLS}
                """,
                (task_id, user_id, Json(task_info), status, created_by, agent_id, notify, deadline_at),
            )
            return self._row(cur.fetchone())

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._cursor() as cur:
            cur.execute(f"SELECT {_TASK_COLS} FROM tasks WHERE task_id = %s AND kind = 'agent_task'", (task_id,))
            return self._row(cur.fetchone())

    def update_task(self, task_id: str, *, info: Optional[dict] = None, **fields: Any) -> Optional[dict]:
        """Set columns in `fields` and merge `info` into task_info (jsonb ||)."""
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k} = %s")
            vals.append(Json(v) if k == "result" and v is not None else v)
        if info:
            sets.append("task_info = coalesce(task_info, '{}'::jsonb) || %s")
            vals.append(Json(info))
        if not sets:
            return self.get_task(task_id)
        vals.append(task_id)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = %s AND kind = 'agent_task' "
                f"RETURNING {_TASK_COLS}",
                tuple(vals),
            )
            return self._row(cur.fetchone())

    def drop_info_keys(self, task_id: str, *keys: str) -> None:
        with self._cursor() as cur:
            for k in keys:
                cur.execute("UPDATE tasks SET task_info = task_info - %s WHERE task_id = %s", (k, task_id))

    def delete_task(self, task_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE task_id = %s AND kind = 'agent_task'", (task_id,))
            return cur.rowcount > 0

    def list_tasks(self, user_id: str, *, active_only: bool = False, limit: int = 20) -> list[dict]:
        where = "AND status = ANY(%s)" if active_only else ""
        params: tuple = (user_id, list(_ACTIVE), limit) if active_only else (user_id, limit)
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_TASK_COLS} FROM tasks WHERE user_id = %s AND kind = 'agent_task' {where} "
                "AND coalesce(task_info->>'deleted', 'false') <> 'true' "
                "ORDER BY created_at DESC LIMIT %s",
                params,
            )
            return [self._row(r) for r in cur.fetchall()]

    def count_active(self, user_id: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM tasks WHERE user_id = %s AND kind = 'agent_task' "
                "AND status = ANY(%s)",
                (user_id, list(_ACTIVE)),
            )
            return int(cur.fetchone()["n"])

    def undelivered(self, user_id: str, limit: int = 50) -> list[dict]:
        """Finished results the user hasn't heard (notify device or next_session)."""
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT {_TASK_COLS} FROM tasks
                WHERE user_id = %s AND kind = 'agent_task' AND delivered_at IS NULL
                  AND notify IN ('device', 'next_session')
                  AND (status = ANY(%s) OR task_info ? 'pending_announcement')
                ORDER BY coalesce(finished_at, updated_at) ASC
                LIMIT %s
                """,
                (user_id, list(_TERMINAL), limit),
            )
            return [self._row(r) for r in cur.fetchall()]

    def open_tasks_for_agent(self, agent_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_TASK_COLS} FROM tasks WHERE agent_id = %s AND kind = 'agent_task' "
                "AND status = ANY(%s)",
                (agent_id, list(_ACTIVE)),
            )
            return [self._row(r) for r in cur.fetchall()]

    def agents_with_open_tasks(self) -> list[dict]:
        """(agent_id, last_seen) for agents that have active tasks."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT t.agent_id, a.agent_info->>'last_seen' AS last_seen,
                       a.agent_info->>'name' AS name
                FROM tasks t JOIN agents a ON a.agent_id = t.agent_id
                WHERE t.kind = 'agent_task' AND t.status = ANY(%s)
                """,
                (list(_ACTIVE),),
            )
            return [{"agent_id": str(r["agent_id"]), "last_seen": r["last_seen"], "name": r["name"]}
                    for r in cur.fetchall()]

    # ----- users ------------------------------------------------------------

    def user_timezone(self, user_id: str) -> Optional[str]:
        try:
            with self._cursor() as cur:
                cur.execute("SELECT timezone FROM users WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                return (row or {}).get("timezone") or None
        except Exception:
            log.exception("user_timezone lookup failed")
            return None

    # ----- jobs (the worker's queue) ----------------------------------------

    def insert_job(self, kind: str, payload: dict, deliver_at: Optional[datetime] = None) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (kind, payload, deliver_at) VALUES (%s, %s, coalesce(%s, now())) RETURNING id",
                (kind, Json(payload), deliver_at),
            )
            return int(cur.fetchone()["id"])

    def cancel_job(self, job_id: Optional[int]) -> bool:
        if not job_id:
            return False
        with self._cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id = %s AND done_at IS NULL", (int(job_id),))
            return cur.rowcount > 0

    def wakes_last_hour(self, user_id: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM jobs WHERE kind = 'task_result' "
                "AND payload->>'user_id' = %s AND created_at > now() - interval '1 hour'",
                (user_id,),
            )
            return int(cur.fetchone()["n"])

    def recent_wake(self, user_id: str, minutes: int = 15) -> bool:
        """True if a task wake was sent to this user's pin recently (session likely from it)."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT 1 FROM jobs WHERE kind = 'task_result' AND payload->>'user_id' = %s "
                "AND done_at > now() - (%s || ' minutes')::interval LIMIT 1",
                (user_id, str(minutes)),
            )
            return cur.fetchone() is not None

    def pending_wake_exists(self, user_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "SELECT 1 FROM jobs WHERE kind = 'task_result' AND done_at IS NULL "
                "AND payload->>'user_id' = %s LIMIT 1",
                (user_id,),
            )
            return cur.fetchone() is not None

    # ----- outbox -----------------------------------------------------------

    def outbox_add(self, *, agent_id: str, task_id: str, envelope: dict) -> dict:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_outbox (agent_id, task_id, msg_id, type, envelope)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (msg_id) DO NOTHING
                RETURNING *
                """,
                (agent_id, task_id, envelope["msg_id"], envelope["type"], Json(envelope)),
            )
            row = cur.fetchone()
            return self._outbox_row(row) if row else {}

    @staticmethod
    def _outbox_row(row: Optional[dict]) -> dict:
        if not row:
            return {}
        d = dict(row)
        for k in ("agent_id", "task_id"):
            if d.get(k) is not None:
                d[k] = str(d[k])
        return d

    def outbox_heads(self, *, agent_id: Optional[str] = None, due_only: bool = True, limit: int = 100) -> list[dict]:
        """The oldest unfinished row of each task (per-task ordering), optionally due."""
        conds = ["o.acked_at IS NULL", "o.expired_at IS NULL"]
        params: list[Any] = []
        if agent_id:
            conds.append("o.agent_id = %s")
            params.append(agent_id)
        due = "AND h.next_attempt_at <= now()" if due_only else ""
        params.append(limit)
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT h.* FROM (
                    SELECT DISTINCT ON (o.task_id) o.*
                    FROM agent_outbox o
                    WHERE {' AND '.join(conds)}
                    ORDER BY o.task_id, o.id
                ) h
                WHERE true {due}
                ORDER BY h.id
                LIMIT %s
                """,
                tuple(params),
            )
            return [self._outbox_row(r) for r in cur.fetchall()]

    def outbox_mark_sent(self, row_id: int, *, retry_in_s: float) -> dict:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE agent_outbox
                SET attempts = attempts + 1, sent_at = now(),
                    next_attempt_at = now() + (%s || ' seconds')::interval
                WHERE id = %s RETURNING *
                """,
                (str(max(0.5, retry_in_s)), row_id),
            )
            return self._outbox_row(cur.fetchone())

    def outbox_defer(self, row_id: int, *, retry_in_s: float) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE agent_outbox SET next_attempt_at = now() + (%s || ' seconds')::interval WHERE id = %s",
                (str(max(0.5, retry_in_s)), row_id),
            )

    def outbox_ack(self, msg_id: str, reply: dict) -> dict:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE agent_outbox SET acked_at = now(), reply = %s "
                "WHERE msg_id = %s AND acked_at IS NULL RETURNING *",
                (Json(reply), msg_id),
            )
            return self._outbox_row(cur.fetchone())

    def outbox_get(self, msg_id: str) -> dict:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM agent_outbox WHERE msg_id = %s", (msg_id,))
            return self._outbox_row(cur.fetchone())

    def outbox_expire(self, row_id: int) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE agent_outbox SET expired_at = now() WHERE id = %s", (row_id,))

    def outbox_expire_older_than(self, hours: float) -> int:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE agent_outbox SET expired_at = now() WHERE acked_at IS NULL AND expired_at IS NULL "
                "AND created_at < now() - (%s || ' hours')::interval RETURNING task_id",
                (str(hours),),
            )
            return cur.rowcount

    def outbox_drop_pending(self, task_id: str, types: tuple[str, ...]) -> int:
        """Expire queued rows of `types` for a task (e.g. a superseded dispatch)."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE agent_outbox SET expired_at = now() WHERE task_id = %s AND acked_at IS NULL "
                "AND expired_at IS NULL AND type = ANY(%s)",
                (task_id, list(types)),
            )
            return cur.rowcount

    def outbox_open_count(self, task_id: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM agent_outbox WHERE task_id = %s AND acked_at IS NULL "
                "AND expired_at IS NULL",
                (task_id,),
            )
            return int(cur.fetchone()["n"])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


__all__ = ["TaskStore", "hash_token", "utcnow", "parse_ts", "timedelta"]
