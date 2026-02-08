from dotenv import load_dotenv
from fastapi import FastAPI

# python -m uvicorn app.main:app --host 0.0.0.0 --port \$PORT

# https://ai.google.dev/gemini-api/docs/live-guide
load_dotenv()

from routes.task_routes import router
from routes.messaging_routes import router as messaging_router
from websocket_handler import websocket_endpoint

app = FastAPI()

# Include all HTTP endpoints from routes
app.include_router(router)
app.include_router(messaging_router)

# Register WebSocket endpoint
app.websocket("/ws/{user_id}")(websocket_endpoint)

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