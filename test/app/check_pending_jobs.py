#!/usr/bin/env python3
"""
Run the same query the server uses to see why enqueue says "already pending".
Shows all rows in pending_text_message_jobs for a user (or all users if no arg).

Usage (from repo root with .env loaded):
  python -m test.app.check_pending_jobs [user_id]
  python test/app/check_pending_jobs.py 4dd16650-c57a-44c4-b530-fc1c15d50e45

If user_id is omitted, lists all rows in pending_text_message_jobs.
"""

import os
import sys

from dotenv import load_dotenv

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(project_root, "app"))
load_dotenv(os.path.join(project_root, ".env"))

from database import execute_query


def main():
    user_id = (sys.argv[1].strip() if len(sys.argv) > 1 else None)

    # Same query the server uses to decide "already pending"
    if user_id:
        q = "SELECT * FROM pending_text_message_jobs WHERE user_id = %s::uuid"
        params = (user_id,)
        print(f"Query: {q}")
        print(f"Params: user_id = {user_id}\n")
    else:
        q = "SELECT * FROM pending_text_message_jobs ORDER BY user_id"
        params = None
        print(f"Query: {q}\n")

    try:
        rows = execute_query(q, params)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not rows:
        print("No rows found. So _has_pending_text_message_job would return False and enqueue would NOT be skipped.")
        return

    print(f"Found {len(rows)} row(s). This is why the server reports 'already pending':\n")
    for i, r in enumerate(rows, 1):
        print(f"  Row {i}: {dict(r)}")
    print("\nTo clear the pending slot for a user (so enqueue works again), the listener runs:")
    print("  DELETE FROM pending_text_message_jobs WHERE user_id = <user_id>::uuid")
    print("after processing the message. If the row stays, the listener may not have run or the DELETE failed.")


if __name__ == "__main__":
    main()
