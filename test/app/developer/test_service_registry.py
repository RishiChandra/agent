"""Unit tests for the developer-service registry (app/developer_ws/service_registry.py)
and its three HTTP endpoints (/developer/register, /developer/unregister,
/developer/services) wired in app/main.py.

These tests do not touch the WebSocket plumbing — only the registration map and
the routes around it. Run with:

    pytest test/app/developer/test_service_registry.py -q
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

# Match the import-style used everywhere else in the repo: `app/` is a sys.path root.
_APP_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "app")
sys.path.insert(0, os.path.abspath(_APP_DIR))

# Importing app.main has side effects (FastAPI app construction + lifespan model
# warmups). We don't want the warmups in unit tests; the lifespan only fires when
# the app actually receives a startup event, which TestClient does on first
# request. Wrap TestClient in a context manager that mocks out the warmups.
from unittest.mock import patch  # noqa: E402

with patch("developer_ws.preload_vosk_model"), \
     patch("developer_ws.preload_piper_voice"):
    from main import app  # noqa: E402
    from developer_ws import service_registry  # noqa: E402


@pytest.fixture
def client():
    """Fresh TestClient + cleared registry per test."""
    service_registry.clear()
    with TestClient(app) as c:
        yield c
    service_registry.clear()


# -------------------- registry module --------------------

def test_register_then_get_returns_url():
    service_registry.clear()
    entry = service_registry.register("svc-a", "wss://a.trycloudflare.com/relay")
    assert entry.service_id == "svc-a"
    assert entry.public_url == "wss://a.trycloudflare.com/relay"
    assert service_registry.get_url("svc-a") == "wss://a.trycloudflare.com/relay"


def test_register_overwrites_url_on_reregister():
    service_registry.clear()
    e1 = service_registry.register("svc-a", "wss://old.example/relay")
    e2 = service_registry.register("svc-a", "wss://new.example/relay")
    # Same registered_at (entry persisted), bumped last_seen, new URL.
    assert e2.registered_at == e1.registered_at
    assert e2.last_seen >= e1.last_seen
    assert e2.public_url == "wss://new.example/relay"
    assert service_registry.get_url("svc-a") == "wss://new.example/relay"


def test_unregister_removes_entry():
    service_registry.clear()
    service_registry.register("svc-a", "wss://a.example/relay")
    assert service_registry.unregister("svc-a") is True
    assert service_registry.get("svc-a") is None
    # Idempotent: second unregister returns False but doesn't raise.
    assert service_registry.unregister("svc-a") is False


def test_get_returns_none_for_unknown_service():
    service_registry.clear()
    assert service_registry.get("nope") is None
    assert service_registry.get_url("nope") is None


# -------------------- HTTP routes --------------------

def test_register_route_accepts_valid_payload(client):
    r = client.post("/developer/register", json={
        "service_id": "svc-1", "public_url": "wss://x.trycloudflare.com/relay",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service_id"] == "svc-1"
    assert body["public_url"] == "wss://x.trycloudflare.com/relay"
    assert body["version"] == "1"
    assert "registered_at" in body and "last_seen" in body


def test_register_route_rejects_missing_service_id(client):
    r = client.post("/developer/register", json={"public_url": "wss://x/relay"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": False, "reason": "service_id is required"}


def test_register_route_rejects_bad_url_scheme(client):
    r = client.post("/developer/register", json={
        "service_id": "svc-1", "public_url": "http://x/relay",
    })
    body = r.json()
    assert body["ok"] is False
    assert "ws://" in body["reason"] or "wss://" in body["reason"]


def test_register_route_rejects_empty_url(client):
    r = client.post("/developer/register", json={"service_id": "svc-1", "public_url": ""})
    body = r.json()
    assert body["ok"] is False
    assert "public_url" in body["reason"]


def test_unregister_route_removes_entry(client):
    client.post("/developer/register", json={
        "service_id": "svc-1", "public_url": "wss://x/relay",
    })
    r = client.post("/developer/unregister", json={"service_id": "svc-1"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "service_id": "svc-1", "removed": True}
    # Now /developer/services should not list it.
    listing = client.get("/developer/services").json()
    assert all(s["service_id"] != "svc-1" for s in listing["services"])


def test_unregister_route_is_idempotent(client):
    r = client.post("/developer/unregister", json={"service_id": "never-existed"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "service_id": "never-existed", "removed": False}


def test_unregister_route_rejects_missing_service_id(client):
    r = client.post("/developer/unregister", json={})
    body = r.json()
    assert body == {"ok": False, "reason": "service_id is required"}


def test_services_list_endpoint_reflects_registry(client):
    client.post("/developer/register", json={
        "service_id": "svc-a", "public_url": "wss://a/relay",
    })
    client.post("/developer/register", json={
        "service_id": "svc-b", "public_url": "wss://b/relay",
    })
    listing = client.get("/developer/services").json()
    ids = sorted(s["service_id"] for s in listing["services"])
    assert ids == ["svc-a", "svc-b"]


def test_ping_returns_service_not_registered_when_unknown(client):
    # Note: the existing /developer/ping/{user_id} also requires an active
    # WebSocket user session. The "service not registered" check fires first
    # whenever service_id is non-empty and absent from the registry, so we hit
    # that branch without needing a live pipeline.
    r = client.post("/developer/ping/some-user", json={
        "service_id": "ghost-svc", "version": "1",
    })
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "service not registered"
    assert body["service_id"] == "ghost-svc"


def test_ping_with_no_service_id_falls_through_to_pipeline_lookup(client):
    # When service_id is "unknown" (the default when payload omits it), the
    # service-registry lookup is skipped (back-compat path) and the route
    # proceeds to check for an active user session. With no live pipeline that
    # returns "no active session", proving we did NOT short-circuit on the
    # service registry.
    r = client.post("/developer/ping/some-user", json={})
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "no active session"
