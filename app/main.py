import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# python -m uvicorn app.main:app --host 0.0.0.0 --port \$PORT

# https://ai.google.dev/gemini-api/docs/live-guide
load_dotenv()

from routes.task_routes import router
from routes.messaging_routes import router as messaging_router
from routes.agent_routes import router as agent_router
from websocket_handler import websocket_endpoint
from developer_ws import (
    developer_websocket_endpoint,
    preload_piper_voice,
    preload_silero_vad,
    preload_vosk_model,
)
from developer_ws import registry as developer_registry
import agents_registry

# Directory holding the registration website's static files (repo-root
# agent_directory/, deployable independently of this service). Resolved from
# this file's location so it works whether the app is launched as
# `python app/main.py` (cwd=repo root) or `cd app && uvicorn main:app` (cwd=app/).
_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent_directory"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the developer agent registry table exists before serving requests.
    try:
        agents_registry.ensure_agents_table()
        print("[main] agents table ensured")
    except Exception as e:
        print(f"[main] agents table ensure failed: {e}")
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
    try:
        await preload_silero_vad()
        print("[main] silero vad preloaded")
    except Exception as e:
        print(f"[main] silero vad preload failed: {e}")
    yield


app = FastAPI(lifespan=lifespan)

# Allow the registration site / tooling to call the JSON API from any origin.
# The API is open (no auth) for v1; WebSocket audio is unaffected by CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_stale_site(request, call_next):
    """Force browsers to revalidate the static site on every load.

    The pages are tiny, and a stale cached test.html can silently disagree
    with server-side timing (e.g. uplink batch cadence vs the silence timer),
    which degrades the whole voice loop. ETag revalidation makes unchanged
    loads cheap (304), so no-cache costs almost nothing.
    """
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if request.method == "GET" and ("text/html" in ct or "text/css" in ct):
        response.headers["Cache-Control"] = "no-cache"
    return response

# Include all HTTP endpoints from routes
app.include_router(router)
app.include_router(messaging_router)
app.include_router(agent_router)

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


# Serve the registration website. Mounted LAST so all API/WS routes above take
# precedence; `html=True` serves index.html at "/" and resolves *.html by name.
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="site")

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