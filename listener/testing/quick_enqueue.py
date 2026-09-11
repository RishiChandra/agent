#!/usr/bin/env python3
"""
Insert a scheduled task job into the Postgres `jobs` table so listener/worker.py
picks it up (replaces the old Azure Service Bus test script).

Usage:
    python quick_enqueue.py <minutes>
    (schedules the job for the specified minutes from now)

Reads DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD from the environment / .env.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from psycopg2.extras import Json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # listener/
from database import get_db_connection  # noqa: E402

load_dotenv()


def main():
    if len(sys.argv) != 2:
        print("Usage: python quick_enqueue.py <minutes>")
        print("Example: python quick_enqueue.py 5")
        sys.exit(1)

    try:
        minutes = int(sys.argv[1])
    except ValueError:
        print("Error: minutes must be a number")
        sys.exit(1)

    USER_ID = "4dd16650-c57a-44c4-b530-fc1c15d50e45"
    TASK_ID = "253b01f6-67f9-4696-82d3-20581e0926d0"
    # Same shape app/enqueue/task_enqueue.prepare_message_contents produces.
    payload = {
        "task_id": TASK_ID,
        "user_id": USER_ID,
        "pending_task": True,
        "pending_message": False,
        "title": "Take my medicine",
        "description": "Take my medicine",
    }
    scheduled_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO jobs (kind, payload, deliver_at) VALUES (%s, %s, %s) RETURNING id",
                    ("task", Json(payload), scheduled_time),
                )
                job_id = cur.fetchone()[0]
        print(f"✅ Job {job_id} scheduled for {scheduled_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
