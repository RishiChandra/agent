import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# python -m uvicorn app.main:app --host 0.0.0.0 --port \$PORT

# https://ai.google.dev/gemini-api/docs/live-guide
load_dotenv()

from routes.task_routes import router
from routes.messaging_routes import router as messaging_router
from websocket_handler import websocket_endpoint
from developer_ws import (
    developer_websocket_endpoint,
    http_router as developer_http_router,
    preload_piper_voice,
    preload_vosk_model,
)


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
app.include_router(developer_http_router)

# Register WebSocket endpoints
app.websocket("/ws/{user_id}")(websocket_endpoint)
app.websocket("/ws/developer/{user_id}")(developer_websocket_endpoint)


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