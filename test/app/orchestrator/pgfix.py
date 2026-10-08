"""Throwaway Postgres for orchestrator tests.

Uses the `pgserver` package (bundled Postgres binaries; `pip install pgserver`)
to start a local server, creates a fresh database per test class, and loads
`fixtures/schema.sql` (the production DDL) followed by `deploy/sql/*.sql` (the
migrations), so tests run against the schema a deploy produces. Tests that need it are skipped if
pgserver is not installed. Points the app's `database.get_db_connection` at the
test database through the DB_* environment variables.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, "../../.."))
sys.path.insert(0, os.path.join(ROOT, "app"))

try:
    import pgserver  # type: ignore
except ImportError:  # pragma: no cover
    pgserver = None

import psycopg2

_server = None


def server():
    global _server
    if pgserver is None:
        raise unittest.SkipTest("pgserver not installed")
    if _server is None:
        _server = pgserver.get_server(os.path.join(tempfile.gettempdir(), "orchestrator-test-pg"), cleanup_mode=None)
    return _server


def _socket_dir(uri: str) -> str:
    return uri.split("host=", 1)[1].split("&", 1)[0]


def fresh_database() -> str:
    """Create a new database with the production schema; point DB_* at it."""
    srv = server()
    host = _socket_dir(srv.get_uri())
    name = f"t_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(host=host, dbname="postgres", user="postgres")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    conn = psycopg2.connect(host=host, dbname=name, user="postgres")
    with conn, conn.cursor() as cur:
        cur.execute(open(os.path.join(HERE, "fixtures", "schema.sql")).read())
    # Then the repo's migrations, exactly as deploy/migrate.sh applies them.
    sql_dir = os.path.join(ROOT, "deploy", "sql")
    for f in sorted(os.listdir(sql_dir)):
        if f.endswith(".sql"):
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path = public")
                cur.execute(open(os.path.join(sql_dir, f)).read())
    conn.close()
    os.environ.update({"DB_HOST": host, "DB_PORT": "5432", "DB_NAME": name,
                       "DB_USER": "postgres", "DB_PASSWORD": ""})
    return name


def connect():
    return psycopg2.connect(host=os.environ["DB_HOST"], dbname=os.environ["DB_NAME"], user="postgres")


def add_user(user_id: str | None = None, tz: str = "America/Los_Angeles") -> str:
    user_id = user_id or str(uuid.uuid4())
    conn = connect()
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO users (user_id, timezone) VALUES (%s, %s)", (user_id, tz))
    conn.close()
    return user_id


def query(sql: str, params: tuple = ()) -> list[tuple]:
    conn = connect()
    with conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
    conn.close()
    return rows
