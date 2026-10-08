"""Deploy-time data backfill for agent routing (run once per release, after migrations).

* Applies the first-party routing data (`agents_registry.FIRST_PARTY_ROUTING`:
  Kairos, MyFitnessPal) to agents that don't have routing fields yet.
* Embeds every agent whose routing text changed or has no embedding
  (`gemini-embedding-001`; one API call per agent that needs it).

Safe to re-run; it only fills missing fields and stale embeddings. The app
itself never does this at startup. Inside the app container:

    python /app/deploy/app_backend/backfill_agent_routing.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "app"))

import agents_registry  # noqa: E402


def main() -> int:
    changed = agents_registry.backfill_routing_and_embeddings()
    print(f'{{"ok": true, "changed": {changed}}}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
