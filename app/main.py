import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Body, FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# python -m uvicorn app.main:app --host 0.0.0.0 --port \$PORT

# https://ai.google.dev/gemini-api/docs/live-guide
load_dotenv()

from routes.task_routes import router
from routes.messaging_routes import router as messaging_router
from websocket_handler import websocket_endpoint
from developer_ws import (
    developer_websocket_endpoint,
    preload_piper_voice,
    preload_vosk_model,
)
from developer_ws import registry as developer_registry
from developer_ws import service_registry as developer_service_registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm Vosk during startup so the first STT call doesn't pay 5–10s of cold-load.
    try:
        await preload_vosk_model()
        print("[main] vosk model preloaded")
    except Exception as e:
        print(f"[main] vosk preload failed: {e}")
    try:
        await preload_piper_voice()
        print("[main] piper voice preloaded")
    except Exception as e:
        print(f"[main] piper preload failed: {e}")
    yield


app = FastAPI(lifespan=lifespan)

# Include all HTTP endpoints from routes
app.include_router(router)
app.include_router(messaging_router)

# Register WebSocket endpoints
app.websocket("/ws/{user_id}")(websocket_endpoint)
app.websocket("/ws/developer/{user_id}")(developer_websocket_endpoint)


_ALLOWED_URL_SCHEMES = ("ws://", "wss://")


def _validate_public_url(url: str) -> str | None:
    """Return None if the URL is acceptable to register, otherwise a short reason."""
    if not url:
        return "public_url is required"
    if not isinstance(url, str):
        return "public_url must be a string"
    if not url.startswith(_ALLOWED_URL_SCHEMES):
        return f"public_url must start with one of {_ALLOWED_URL_SCHEMES}"
    return None


@app.post("/developer/register")
async def developer_register(payload: dict | None = Body(default=None)):
    """Developer service registers its current public dial URL.

    Called by: a service implementing BUILD_SERVICE_PROMPT_V2 on startup (and
    every ~5 min as a heartbeat). The dial URL is typically a fresh
    `wss://<random>.trycloudflare.com/<path>` that changes on every restart, so
    the service tells the orchestrator where to find it instead of hardcoding
    one URL on either side.

    Body: `{service_id, public_url, version}`. Returns `{ok, service_id,
    registered_at, last_seen, ...}` or `{ok: false, reason: ...}` on validation
    failure. Idempotent — re-registering an existing service_id overwrites the
    URL and bumps `last_seen`.
    """
    body = payload or {}
    service_id = str(body.get("service_id", "")).strip()
    public_url = str(body.get("public_url", "")).strip()
    version = str(body.get("version", "1"))
    reg_log = logging.getLogger("developer_ws")
    if not service_id:
        reg_log.warning("register rejected: missing service_id payload=%r", body)
        return {"ok": False, "reason": "service_id is required"}
    url_err = _validate_public_url(public_url)
    if url_err:
        reg_log.warning(
            "register rejected: %s service_id=%s public_url=%r",
            url_err, service_id, public_url,
        )
        return {"ok": False, "reason": url_err}
    entry = developer_service_registry.register(service_id, public_url, version=version)
    reg_log.info(
        "register accepted service_id=%s public_url=%s version=%s",
        entry.service_id, entry.public_url, entry.version,
    )
    return {
        "ok": True,
        "service_id": entry.service_id,
        "public_url": entry.public_url,
        "registered_at": entry.registered_at,
        "last_seen": entry.last_seen,
        "version": entry.version,
    }


@app.post("/developer/unregister")
async def developer_unregister(payload: dict | None = Body(default=None)):
    """Developer service removes its registration on graceful shutdown.

    Called by: a service's SIGINT/SIGTERM/atexit handler. Idempotent — returns
    `{ok: true}` whether or not the service was actually registered, so the
    caller can fire-and-forget without branching on the response.
    """
    body = payload or {}
    service_id = str(body.get("service_id", "")).strip()
    reg_log = logging.getLogger("developer_ws")
    if not service_id:
        reg_log.warning("unregister rejected: missing service_id payload=%r", body)
        return {"ok": False, "reason": "service_id is required"}
    removed = developer_service_registry.unregister(service_id)
    return {"ok": True, "service_id": service_id, "removed": removed}


@app.get("/developer/services")
async def developer_services_list():
    """Debug endpoint: snapshot of all registered services."""
    return {
        "services": [
            {
                "service_id":    e.service_id,
                "public_url":    e.public_url,
                "registered_at": e.registered_at,
                "last_seen":     e.last_seen,
                "version":       e.version,
            }
            for e in developer_service_registry.list_all()
        ],
    }


@app.post("/developer/ping/{user_id}")
async def developer_ping(user_id: str, payload: dict | None = Body(default=None)):
    """Service-initiated call hook.

    Called by: any external service that wants main to initiate the bridge back to it.
    The reference caller is `_ping_main` in `developer_ws/testing/echo_server.py`,
    but any HTTP client can hit this route. See `developer_ws/BRIDGE_PROTOCOL.md`.

    Flow:
      1. Read `service_id` + `version` from the JSON body (if any).
      2. Look up the live user session via `developer_ws.registry.get(user_id)`.
      3. Look up the developer-service dial URL via
         `developer_ws.service_registry.get(service_id)`. If unset, the caller
         likely forgot to POST `/developer/register` first — short-circuit with
         `reason: "service not registered"`. As a back-compat fallback, if the
         service_id is unset (older clients), fall through to the legacy
         `DEVELOPER_WS_REMOTE_BRIDGE_URL` env var that `bridge.py` defaults to.
      4. Pass the resolved URL into `pipeline.on_service_ping(service_id, public_url)`
         so the announce-then-bridge flow dials the right place.
    """
    body = payload or {}
    service_id = str(body.get("service_id", "unknown"))
    caller_version = str(body.get("version", "?"))
    ping_log = logging.getLogger("developer_ws")
    ping_log.info(
        "ping received user_id=%s service_id=%s caller_version=%s",
        user_id, service_id, caller_version,
    )

    # Resolve the developer-service dial URL from the service registry.
    public_url: str | None = None
    if service_id and service_id != "unknown":
        public_url = developer_service_registry.get_url(service_id)
        if public_url is None:
            ping_log.info(
                "ping rejected: service not registered user_id=%s service_id=%s",
                user_id, service_id,
            )
            return {
                "ok": False,
                "reason": "service not registered",
                "user_id": user_id,
                "service_id": service_id,
            }

    pipeline = developer_registry.get(user_id)
    if pipeline is None:
        ping_log.info(
            "ping rejected: no active session user_id=%s service_id=%s",
            user_id, service_id,
        )
        return {
            "ok": False,
            "reason": "no active session",
            "user_id": user_id,
            "service_id": service_id,
        }
    ok = await pipeline.on_service_ping(service_id=service_id, public_url=public_url)
    return {
        "ok": ok,
        "user_id": user_id,
        "service_id": service_id,
        "public_url": public_url,
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        ws="websockets",          # ensure the websockets backend
        ws_ping_interval=None,    # completely disable server pings
        ws_ping_timeout=None,      # disable timeout

    )