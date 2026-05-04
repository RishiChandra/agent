#!/usr/bin/env python3
"""
Create the local `local` database (if missing) and all application tables.

Prerequisites: PostgreSQL installed and running (default localhost:5432).

Configuration (optional): repo root `.env` with LOCAL_DB_* (or DB_* for host/port/user/password only):

  LOCAL_DB_HOST, LOCAL_DB_PORT, LOCAL_DB_USER, LOCAL_DB_PASSWORD, LOCAL_DB_NAME

The database to create and populate is **LOCAL_DB_NAME** (default `local` only — `DB_NAME` is not used so you do not accidentally apply a dev schema to a production database name).
Defaults: host localhost, port 5432, user postgres, password empty.
Run from repository root:  python test/setup_local_postgres.py
Or from the `test` folder:  python setup_local_postgres.py
"""
from __future__ import annotations

import os
import sys

try:
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
except ImportError:
    print("Install dependencies first: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_env() -> None:
    env_path = os.path.join(_repo_root(), ".env")
    if os.path.isfile(env_path):
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path)
        except ImportError:
            pass


def _cfg() -> dict[str, str]:
    _load_env()
    return {
        "host": os.environ.get("LOCAL_DB_HOST") or os.environ.get("DB_HOST", "localhost"),
        "port": os.environ.get("LOCAL_DB_PORT") or os.environ.get("DB_PORT", "5432"),
        "user": os.environ.get("LOCAL_DB_USER") or os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("LOCAL_DB_PASSWORD") or os.environ.get("DB_PASSWORD", ""),
        # Target DB for this script only (never fall back to DB_NAME).
        "dbname": os.environ.get("LOCAL_DB_NAME", "local"),
    }


def _connect(dbname: str, c: dict[str, str]):
    return psycopg2.connect(
        host=c["host"],
        port=c["port"],
        user=c["user"],
        password=c["password"],
        dbname=dbname,
    )


def _ensure_database(c: dict[str, str]) -> None:
    maintenance = "postgres"
    conn = _connect(maintenance, c)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (c["dbname"],),
        )
        exists = cur.fetchone() is not None
        cur.close()
        if exists:
            print(f'Database "{c["dbname"]}" already exists.')
            return
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(c["dbname"]))
            )
        print(f'Created database "{c["dbname"]}".')
    finally:
        conn.close()


def _migrate_users_partial_rows(cur) -> None:
    """Allow integration Excel setup rows that only set some user columns (e.g. first_name + timezone)."""
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'users'"
    )
    if cur.fetchone() is None:
        return
    for col in ("last_name", "firebase_uid", "username"):
        cur.execute(
            sql.SQL("ALTER TABLE users ALTER COLUMN {} DROP NOT NULL").format(sql.Identifier(col))
        )


def _apply_schema(c: dict[str, str]) -> None:
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id UUID PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT,
            firebase_uid TEXT,
            username TEXT,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            device_prefix TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT users_firebase_uid_key UNIQUE (firebase_uid),
            CONSTRAINT users_username_key UNIQUE (username)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id UUID PRIMARY KEY DEFAULT gen_random_uuid()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS chat_members (
            chat_id UUID NOT NULL REFERENCES chats (chat_id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
            PRIMARY KEY (chat_id, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS relationships (
            uid1 UUID NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
            uid2 UUID NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
            rel_type TEXT NOT NULL,
            PRIMARY KEY (uid1, uid2, rel_type)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sessions (
            user_id UUID PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
            scratchpad TEXT NOT NULL DEFAULT '',
            is_active BOOLEAN NOT NULL DEFAULT true
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
            task_info JSONB,
            status TEXT NOT NULL DEFAULT 'pending',
            time_to_execute TIMESTAMPTZ,
            enqueue_sequence_id BIGINT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS messages (
            chat_id UUID NOT NULL REFERENCES chats (chat_id) ON DELETE CASCADE,
            message_id UUID NOT NULL,
            sender_id UUID NOT NULL REFERENCES users (user_id),
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            is_read BOOLEAN DEFAULT false,
            PRIMARY KEY (chat_id, message_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pending_text_message_jobs (
            user_id UUID NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
            message_id UUID NOT NULL,
            PRIMARY KEY (user_id, message_id)
        )
        """,
    ]

    conn = _connect(c["dbname"], c)
    try:
        with conn.cursor() as cur:
            for stmt in ddl:
                cur.execute(stmt)
            _migrate_users_partial_rows(cur)
        conn.commit()
        print(f'Schema applied in database "{c["dbname"]}" (CREATE TABLE IF NOT EXISTS).')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    c = _cfg()
    print(
        f"Connecting to PostgreSQL at {c['host']}:{c['port']} as {c['user']!r} "
        f"(maintenance db: postgres, target db: {c['dbname']!r})..."
    )
    try:
        _connect("postgres", c).close()
    except psycopg2.OperationalError as e:
        print(
            "Could not connect to PostgreSQL. Start the service and check host/port/user/password.\n"
            f"Error: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    _ensure_database(c)
    _apply_schema(c)

    print("\nNote:")
    print("  Removing a server entry in pgAdmin only deletes that *connection* in pgAdmin.")
    print("  It does not uninstall PostgreSQL or drop databases; data stays on disk until you DROP DATABASE or uninstall.")
    print('  Every PostgreSQL install has a default database named "postgres" (used for admin tasks like CREATE DATABASE).')
    print(f'  Your application schema is in "{c["dbname"]}" — that is separate from "postgres".')

    print("\nNext steps:")
    print("  In repo root `.env`, set LOCAL_DB_* to match the values above (especially LOCAL_DB_NAME).")
    print("  From the `test` folder:  python integration/integration_test.py")


if __name__ == "__main__":
    main()
