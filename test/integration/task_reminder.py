import asyncio
from datetime import datetime, UTC
import json
import os
import sys
import contextlib
from uuid import uuid4

from dotenv import load_dotenv
import uvicorn
import websockets  # type: ignore[import]

# Ensure project root and app package are on sys.path so this can be run
# either from the repository root (python -m test.integration.task_reminder)
# or from within the test directory (python -m integration.task_reminder).
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
app_path = os.path.join(project_root, "app")
if app_path not in sys.path:
    sys.path.insert(0, app_path)

# Directory to store per-session transcription logs (alongside this test file)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "test_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Load environment variables (including GOOGLE_API_KEY, DB settings, etc.) from the project .env
load_dotenv(os.path.join(project_root, ".env"))

from app.main import app
import app.websocket_handler as app_websocket_handler
from app.user_session_manager import UserSessionManager as RealUserSessionManager
import app.database as db_module
import session_management_utils as root_session_utils
import websocket_handler as root_websocket_handler

WS_HOST = "127.0.0.1"
WS_PORT = 8765
WS_URL_TEMPLATE = f"ws://{WS_HOST}:{WS_PORT}/ws/{{user_id}}"


def _build_pending_task_payloads(now_iso: str) -> list[dict]:
    """Build pending_task-style payloads that websocket_handler understands."""
    return [
        {
            "pending_task": True,
            "title": "Take my medicine",
            "description": "Take your morning medication with a glass of water.",
            "time_to_execute": now_iso,
        },
        {
            "pending_task": True,
            "title": "Review today's reminders",
            "description": "Summarize all of the user's scheduled tasks and reminders for today.",
            "time_to_execute": now_iso,
        },
        {
            "pending_task": True,
            "title": "Stretch break",
            "description": "Stand up, stretch your legs and shoulders, and rest your eyes from screens.",
            "time_to_execute": now_iso,
        },
    ]


def _install_db_mocks() -> None:
    """Patch database helpers so no real Postgres queries are executed."""

    def fake_execute_query(query, params=None):
        print(f"[MOCK execute_query] {query!r} {params!r}")
        return []

    def fake_execute_update(query, params=None):
        print(f"[MOCK execute_update] {query!r} {params!r}")
        return 1

    # Patch both the app.database module and the root-level session_management_utils
    db_module.execute_query = fake_execute_query  # type: ignore[assignment]
    db_module.execute_update = fake_execute_update  # type: ignore[assignment]
    root_session_utils.execute_query = fake_execute_query  # type: ignore[assignment]
    root_session_utils.execute_update = fake_execute_update  # type: ignore[assignment]


def _install_mock_user_session_manager() -> None:
    """Replace UserSessionManager used by websocket_handler with a version that uses mock DB access."""

    def mock_get_session(user_id: str):
        print(f"[MOCK get_session] user_id={user_id}")
        return None

    def mock_create_session(user_id: str):
        print(f"[MOCK create_session] user_id={user_id}")
        return {"user_id": user_id, "is_active": True}

    def mock_update_status(user_id: str, is_active: bool):
        print(f"[MOCK update_session_status] user_id={user_id}, is_active={is_active}")

    def mock_get_user_by_id(user_id: str):
        print(f"[MOCK get_user_by_id] user_id={user_id}")
        return {
            "user_id": user_id,
            "first_name": "Test",
            "last_name": "User",
            "timezone": "UTC",
        }

    class TestUserSessionManager(RealUserSessionManager):
        def __init__(self, user_id: str):
            super().__init__(
                user_id,
                get_session_fn=mock_get_session,
                create_session_fn=mock_create_session,
                update_session_status_fn=mock_update_status,
                get_user_by_id_fn=mock_get_user_by_id,
            )

    # Monkey-patch the classes that websocket handlers reference (both app.* and root imports)
    app_websocket_handler.UserSessionManager = TestUserSessionManager  # type: ignore[assignment]
    root_websocket_handler.UserSessionManager = TestUserSessionManager  # type: ignore[assignment]


async def _run_single_session(user_id: str, label: str, payload: dict, follow_up_text: str | None = None) -> None:
    """Open a websocket session, send one prompt (plus optional follow-up), then close."""
    ws_url = WS_URL_TEMPLATE.format(user_id=user_id)
    log_path = os.path.join(OUTPUT_DIR, f"{label}_{uuid4().hex}.log")

    async with websockets.connect(ws_url) as ws:
        with open(log_path, "w", encoding="utf-8") as log_file:
            # Reader task: writes whatever websocket_handler sends (TranscriptionHandler outputs) to a log file
            async def reader():
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        log_file.write(f"RAW: {raw}\n")
                        log_file.flush()
                        continue

                    if "output_text" in msg:
                        log_file.write(f"AGENT: {msg['output_text']}\n")
                        log_file.flush()
                    if "input_text" in msg:
                        log_file.write(f"USER: {msg['input_text']}\n")
                        log_file.flush()
                    if msg.get("end_conversation"):
                        log_file.write("END: Conversation ended by server\n")
                        log_file.flush()
                        break

            reader_task = asyncio.create_task(reader())

            # Send the pending_task payload
            await ws.send(json.dumps(payload))
            # Let the agent speak; tune this delay as needed
            await asyncio.sleep(8)

            if follow_up_text:
                follow_up_turn = {
                    "turns": {
                        "message": follow_up_text,
                    },
                    "turn_complete": True,
                }
                await ws.send(json.dumps(follow_up_turn))
                await asyncio.sleep(10)

            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task


async def _websocket_client_test(user_id: str) -> None:
    """Connect to the FastAPI websocket endpoint and exercise the full agent stack.

    Opens a fresh websocket (and thus Gemini) session for each prompt, running them serially.
    """
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    pending_task_payloads = _build_pending_task_payloads(now_iso)

    # Run three separate sessions (one per payload) sequentially.
    await _run_single_session(
        user_id=user_id,
        label="take_my_medicine",
        payload=pending_task_payloads[0],
        follow_up_text="I finished taking my medicine. Please mark that task complete in my task list.",
    )

    await _run_single_session(
        user_id=user_id,
        label="review_todays_reminders",
        payload=pending_task_payloads[1],
    )

    await _run_single_session(
        user_id=user_id,
        label="stretch_break",
        payload=pending_task_payloads[2],
    )


async def run_server_and_test() -> None:
    """Start uvicorn in-process and run the websocket client test against it."""
    # Install DB and session manager mocks so no real Postgres access occurs
    _install_db_mocks()
    _install_mock_user_session_manager()

    config = uvicorn.Config(app, host=WS_HOST, port=WS_PORT, log_level="info", ws="websockets")
    server = uvicorn.Server(config)

    server_task = asyncio.create_task(server.serve())
    # Give the server a moment to start
    await asyncio.sleep(1.5)

    try:
        await _websocket_client_test(user_id="test-user-id")
    finally:
        server.should_exit = True
        await server_task


def main() -> None:
    """
    Convenience entrypoint so you can run:

        python -m test.integration.task_reminder

    This will start the FastAPI/uvicorn server in-process, open a websocket
    connection to `/ws/{user_id}`, send pending-task reminders, and then a
    follow-up user message that should exercise `general_thinking_agent` and
    downstream agentic tools. All agent speech transcriptions are printed.
    """
    asyncio.run(run_server_and_test())


if __name__ == "__main__":
    main()

