"""In-memory registry of developer services, keyed by service_id.

Separate from `registry.py` (which tracks end-user pipelines by user_id). This
registry stores the **outbound dial URL** a developer service currently lives
at, so the orchestrator can route `request_orchestrator_call(user_id)` traffic
to whatever transient `cloudflared` URL (or self-hosted equivalent) the service
self-published on startup.

Lifecycle:
  - Service starts → POST /developer/register {service_id, public_url} →
    `register(service_id, public_url)` stores the mapping.
  - Service heartbeats (~every 5 min) → re-POST /developer/register with the
    current URL → `register(...)` overwrites and bumps `last_seen`.
  - Service shuts down → POST /developer/unregister {service_id} →
    `unregister(service_id)` deletes the mapping.
  - HTTP ping `/developer/ping/{user_id}` body carries `service_id` →
    `get(service_id)` returns the current URL or None.

No authentication. Last-write-wins. Intended for development and demo. A
production deployment should layer auth + per-service-id ownership on top.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Dict, List, Optional

log = logging.getLogger("developer_ws")


@dataclass
class ServiceEntry:
    """One row in the service registry."""
    service_id: str
    public_url: str
    registered_at: float  # epoch seconds, first register call for this service_id
    last_seen: float      # epoch seconds, most recent register/heartbeat
    version: str = "1"


_entries: Dict[str, ServiceEntry] = {}
_lock = Lock()


def register(service_id: str, public_url: str, version: str = "1") -> ServiceEntry:
    """Called by the HTTP `/developer/register` route in `app/main.py`.

    Idempotent: re-registering an existing service_id overwrites `public_url`
    and bumps `last_seen` (this is how heartbeats keep the entry fresh).
    Returns the stored entry so the caller can echo `registered_at` in the
    response if it wants to.
    """
    now = time.time()
    with _lock:
        existing = _entries.get(service_id)
        if existing is None:
            entry = ServiceEntry(
                service_id=service_id,
                public_url=public_url,
                registered_at=now,
                last_seen=now,
                version=version,
            )
            _entries[service_id] = entry
            log.info(
                "service registered service_id=%s public_url=%s (active=%d)",
                service_id, public_url, len(_entries),
            )
            return entry
        # Heartbeat / URL change path.
        url_changed = existing.public_url != public_url
        existing.public_url = public_url
        existing.last_seen = now
        existing.version = version
        if url_changed:
            log.info(
                "service URL changed service_id=%s public_url=%s (active=%d)",
                service_id, public_url, len(_entries),
            )
        else:
            log.debug(
                "service heartbeat service_id=%s (active=%d)",
                service_id, len(_entries),
            )
        return existing


def unregister(service_id: str) -> bool:
    """Called by the HTTP `/developer/unregister` route in `app/main.py`.

    Returns True if an entry was removed, False if no such service was
    registered (idempotent — the route still returns 200 either way).
    """
    with _lock:
        removed = _entries.pop(service_id, None)
    if removed is None:
        log.info("unregister called for unknown service_id=%s", service_id)
        return False
    log.info(
        "service unregistered service_id=%s (active=%d)",
        service_id, len(_entries),
    )
    return True


def get(service_id: str) -> Optional[ServiceEntry]:
    """Called by the HTTP `/developer/ping/{user_id}` route to resolve where to
    dial. Returns None if the service is not currently registered (the route
    then responds with `{"ok": false, "reason": "service not registered"}`).
    """
    with _lock:
        return _entries.get(service_id)


def get_url(service_id: str) -> Optional[str]:
    """Convenience wrapper returning just the URL."""
    entry = get(service_id)
    return entry.public_url if entry is not None else None


def list_all() -> List[ServiceEntry]:
    """Snapshot of all registered services. Used by a debug endpoint."""
    with _lock:
        return list(_entries.values())


def clear() -> None:
    """Test hook — drops every registered service."""
    with _lock:
        _entries.clear()
