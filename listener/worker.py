#!/usr/bin/env python3
"""
Job-queue worker: polls the Postgres `jobs` table and wakes the device over MQTT.

Replaces the Azure Functions Service Bus trigger (listener/function_app.py) and
IoT Hub C2D (listener/iot_hub_mqtt.py). Same logic, new transport:

  * app/enqueue/*.py INSERT rows into `jobs` (schema: deploy/sql/001_jobs.sql).
  * Every POLL_INTERVAL_SEC this loop claims due rows (FOR UPDATE SKIP LOCKED)
    and, per job:
      - looks up the user's websocket session row;
      - if the session is ACTIVE, pushes deliver_at forward by one minute
        (what the Azure function did by re-scheduling the Service Bus message);
      - otherwise publishes {"command": "start_websocket", "reason": "session_inactive",
        ...} to the device via mqtt_publish.send_to_device;
      - for text_message jobs also sends the {"reason": "text_message",
        "pending_messages": true} wake, exactly as before.
  * A job that raises has `attempts` incremented and is retried after
    RETRY_DELAY; after MAX_ATTEMPTS failures it is marked done and logged.
    Transport errors reaching the broker (connection refused while mosquitto
    still waits for its certificate, DNS, TLS handshake) are transient: the
    job is retried after RETRY_DELAY without spending an attempt, so a device
    wake is never dropped because the broker was down.

Run:  python listener/worker.py
Imports resolve relative to this file, so the working directory does not matter.
Config: DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD and the MQTT_* / DEVICE_ID
variables documented in listener/mqtt_publish.py.
"""
import json
import logging
import os
import signal
import sys
import time
from datetime import timedelta
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv()

from database import execute_query, get_db_connection  # noqa: E402  (listener/database.py)
from mqtt_publish import DEFAULT_DEVICE_ID, send_to_device  # noqa: E402
from session_management_utils import get_session  # noqa: E402

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

POLL_INTERVAL_SEC = float(os.getenv("WORKER_POLL_INTERVAL_SEC", "2"))
BATCH_SIZE = 10
MAX_ATTEMPTS = 5
RETRY_DELAY = timedelta(seconds=30)          # after a failed attempt
ACTIVE_SESSION_DEFER = timedelta(minutes=1)  # session still open -> try again later

# Fallback carried over from function_app.py for payloads that omit user_id.
DEFAULT_USER_ID = "4dd16650-c57a-44c4-b530-fc1c15d50e45"

# deploy/sql/001_jobs.sql (copied into the image by the Dockerfile); applied at
# startup so the worker never polls a database without the table, e.g. on the
# first deploy before deploy.sh's migration step has run.
JOBS_SQL_PATH = Path(__file__).resolve().parent.parent / "deploy" / "sql" / "001_jobs.sql"

CLAIM_SQL = """
    SELECT id, kind, payload, attempts
    FROM jobs
    WHERE deliver_at <= now() AND done_at IS NULL
    ORDER BY deliver_at
    FOR UPDATE SKIP LOCKED
    LIMIT %s
"""

_stop = False


def _request_stop(signum, _frame) -> None:
    global _stop
    log.info("received signal %s, finishing current batch", signum)
    _stop = True


def get_unread_messages_for_chat(chat_id: str):
    """
    Fetch unread messages (is_read = false or null) for the given chat.
    Marking as read is done only on the websocket side after the AI has been told.
    """
    query = """
        SELECT content, created_at
        FROM messages
        WHERE chat_id = %s::uuid
          AND (is_read IS FALSE OR is_read IS NULL)
        ORDER BY created_at ASC
    """
    return execute_query(query, (chat_id,))


def handle_job(job: dict) -> bool:
    """
    Port of function_app.QueueWorker for one job row.

    Returns True when the job is finished, False when it must be re-delivered
    later because the user's websocket session is still active.
    Any exception propagates to process_batch, which counts it as a failed attempt.
    """
    job_id = job["id"]
    data = job["payload"]
    body = json.dumps(data)  # forwarded to the device as system_message, as the raw queue body was
    message_type = data.get("message_type") or job["kind"]
    user_id = data.get("user_id") or DEFAULT_USER_ID
    chat_id = data.get("chat_id")
    log.info("job %s (%s): %s", job_id, job["kind"], body)

    session = get_session(user_id)
    if session is None:
        log.warning("job %s: no session row for user %s", job_id, user_id)
    elif session["is_active"] is True:
        log.info("job %s: session ACTIVE for user %s, deferring %s", job_id, user_id, ACTIVE_SESSION_DEFER)
        return False
    else:
        send_to_device(DEFAULT_DEVICE_ID, {
            "command": "start_websocket",
            "reason": "session_inactive",
            "user_id": user_id,
            "system_message": body,
        })
        log.info("job %s: sent start_websocket (session_inactive) to %s", job_id, DEFAULT_DEVICE_ID)

    if message_type == "text_message":
        if not chat_id:
            log.warning("job %s: text_message missing chat_id, skipping", job_id)
            return True
        rows = get_unread_messages_for_chat(chat_id)
        send_to_device(DEFAULT_DEVICE_ID, {
            "command": "start_websocket",
            "reason": "text_message",
            "user_id": user_id,
            "pending_messages": True,
        })
        # is_read and pending_text_message_jobs are cleared on the websocket side
        # after the AI has been told about the messages.
        log.info("job %s: sent text_message wake for user %s (%d unread)", job_id, user_id, len(rows))

    return True


def process_batch(conn) -> int:
    """
    Claim up to BATCH_SIZE due jobs in one transaction, handle each, and record
    the outcome. Each job runs inside a savepoint so one failure cannot poison
    the others. Returns the number of jobs claimed.
    """
    with conn:  # commit on success, rollback if the batch itself blows up
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(CLAIM_SQL, (BATCH_SIZE,))
            jobs = cur.fetchall()
            for job in jobs:
                cur.execute("SAVEPOINT job")
                try:
                    if handle_job(job):
                        cur.execute("UPDATE jobs SET done_at = now() WHERE id = %s", (job["id"],))
                    else:
                        cur.execute(
                            "UPDATE jobs SET deliver_at = now() + %s WHERE id = %s",
                            (ACTIVE_SESSION_DEFER, job["id"]),
                        )
                except OSError as e:
                    # Socket/TLS-level failure reaching the broker (ConnectionError,
                    # socket.gaierror, ssl.SSLError are all OSError): retry later
                    # without counting an attempt.
                    cur.execute("ROLLBACK TO SAVEPOINT job")
                    log.warning("job %s: broker unreachable (%s), retrying in %s", job["id"], e, RETRY_DELAY)
                    cur.execute(
                        "UPDATE jobs SET deliver_at = now() + %s WHERE id = %s",
                        (RETRY_DELAY, job["id"]),
                    )
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT job")
                    attempts = job["attempts"] + 1
                    if attempts >= MAX_ATTEMPTS:
                        log.exception("job %s failed %d/%d times, giving up", job["id"], attempts, MAX_ATTEMPTS)
                        cur.execute(
                            "UPDATE jobs SET attempts = %s, done_at = now() WHERE id = %s",
                            (attempts, job["id"]),
                        )
                    else:
                        log.exception(
                            "job %s failed (attempt %d/%d), retrying in %s",
                            job["id"], attempts, MAX_ATTEMPTS, RETRY_DELAY,
                        )
                        cur.execute(
                            "UPDATE jobs SET attempts = %s, deliver_at = now() + %s WHERE id = %s",
                            (attempts, RETRY_DELAY, job["id"]),
                        )
    return len(jobs)


def ensure_jobs_table(conn) -> None:
    """Apply deploy/sql/001_jobs.sql (CREATE ... IF NOT EXISTS) when the file is present."""
    if not JOBS_SQL_PATH.is_file():
        log.info("%s not found; assuming the jobs table already exists", JOBS_SQL_PATH)
        return
    with conn:
        with conn.cursor() as cur:
            cur.execute(JOBS_SQL_PATH.read_text())
    log.info("applied %s", JOBS_SQL_PATH)


def main() -> None:
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    log.info(
        "worker starting: poll=%ss batch=%d max_attempts=%d device=%s mqtt=%s:%s",
        POLL_INTERVAL_SEC, BATCH_SIZE, MAX_ATTEMPTS, DEFAULT_DEVICE_ID,
        os.getenv("MQTT_HOST", "mosquitto"), os.getenv("MQTT_PORT", "8883"),
    )

    conn = None
    while not _stop:
        try:
            if conn is None or conn.closed:
                conn = get_db_connection()
                ensure_jobs_table(conn)
                log.info("connected to postgres at %s", os.environ.get("DB_HOST"))
            claimed = process_batch(conn)
            if claimed:
                log.info("processed %d job(s)", claimed)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            log.error("database connection error: %s (reconnecting in %ss)", e, POLL_INTERVAL_SEC)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = None
        except Exception:
            log.exception("unexpected error in poll loop")
        time.sleep(POLL_INTERVAL_SEC)

    if conn is not None and not conn.closed:
        conn.close()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
