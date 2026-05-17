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


@app.post("/developer/ping/{user_id}")
async def developer_ping(user_id: str, payload: dict | None = Body(default=None)):
    """Service-initiated call hook.

    Called by: any external service that wants main to initiate the bridge back to it.
    The reference caller is `_ping_main` in `developer_ws/testing/echo_server.py`,
    but any HTTP client can hit this route. See `developer_ws/BRIDGE_PROTOCOL.md`.

    Flow:
      1. Read `service_id` + `version` from the JSON body (if any).
      2. Look up the live session via `developer_ws.registry.get(user_id)`.
      3. If a session exists, call `pipeline.on_service_ping(service_id=...)` which
         speaks the announcement and dials the bridge to `DEVELOPER_WS_REMOTE_BRIDGE_URL`.
    """
    body = payload or {}
    service_id = str(body.get("service_id", "unknown"))
    caller_version = str(body.get("version", "?"))
    ping_log = logging.getLogger("developer_ws")
    ping_log.info(
        "ping received user_id=%s service_id=%s caller_version=%s",
        user_id, service_id, caller_version,
    )
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
    ok = await pipeline.on_service_ping(service_id=service_id)
    return {"ok": ok, "user_id": user_id, "service_id": service_id}

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