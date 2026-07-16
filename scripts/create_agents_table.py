"""Create the `agents` table (developer agent registry).

The table is also auto-created at app startup (see `ensure_agents_table` wired
into the lifespan in `app/main.py`), so this script is mostly for running the
migration by hand against a database without booting the app.

Run from repo root with the app's env (DB_HOST/DB_NAME/DB_USER/DB_PASSWORD set,
e.g. via .env):

    python scripts/create_agents_table.py
"""

import os
import sys

# Make `app/` importable as the top-level package root (matches how the app runs:
# `cd app && uvicorn main:app`), so `import agents_registry` / `import database`
# resolve the same way here as in production.
_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, _APP_DIR)

from agents_registry import ensure_agents_table  # noqa: E402


if __name__ == "__main__":
    ensure_agents_table()
    print("OK: agents table is present.")
