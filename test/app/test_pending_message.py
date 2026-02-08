"""
Variant of test_task_reminder.py: connect to the WebSocket and send the initial
message that represents a pending text message (session_inactive with system_message).

Before connecting, the test inserts a new row into messages and a new row into
pending_text_message_jobs so the server has something to load when it sees
pending_messages: true.

The payload simulates what the chip receives from the listener via MQTT:
  {"command": "start_websocket", "reason": "session_inactive", "user_id": "...",
   "system_message": "{\"message_type\": \"text_message\", \"user_id\": \"...\", \"pending_task\": false, \"pending_message\": true}"}

The client then sends to the WebSocket the equivalent first message so the server
loads pending messages (message_type + pending_messages: true).

Run from repo root with .env loaded; ensure backend and DB are up (e.g. localhost:8000).
    python -m test.app.test_pending_message
    python test/app/test_pending_message.py
"""

import asyncio
import sys
import os
import json
import base64
import uuid
from datetime import datetime, timezone
from collections import deque

import websockets
import pyaudio
from dotenv import load_dotenv

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(project_root, "app"))
sys.path.insert(0, project_root)
from app.session_management_utils import get_session
from app.database import execute_update

load_dotenv(os.path.join(project_root, ".env"))

# Same pattern as test_task_reminder: user + WebSocket URL
USER_ID = os.getenv("TEST_PENDING_USER_ID", "4dd16650-c57a-44c4-b530-fc1c15d50e45")
# If set, send initial message in Azure IoT Hub / ESP32 format (turns as JSON string)
USE_IOT_HUB_INIT = os.getenv("USE_IOT_HUB_INIT", "1").lower() in ("1", "true", "yes")
# Chat to attach the test message to (must exist or use a test-only chat_id)
TEST_CHAT_ID = os.getenv("TEST_PENDING_CHAT_ID", "550e8400-e29b-41d4-a716-446655440000")
WS_URI = (
    # f"ws://localhost:8000/ws/{USER_ID}"
    f"wss://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io/ws/{USER_ID}"
)

FORMAT = pyaudio.paInt16
CHANNELS = 1
INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHUNK = 512


def build_pending_message_init():
    """Initial message to send to WebSocket (pending text message, not a task)."""
    return {
        "message_type": "text_message",
        "user_id": USER_ID,
        "pending_task": False,
        "pending_message": True,
        "pending_messages": True,  # server uses this to load from pending_text_message_jobs
    }


def build_iot_hub_init():
    """
    Initial message in the exact format from Azure IoT Hub / ESP32:
    full JSON wrapped in turns as a string (server parses and treats as pending_messages).
    """
    inner = {
        "command": "start_websocket",
        "reason": "text_message",
        "user_id": USER_ID,
        "pending_messages": True,
    }
    return {
        "turns": json.dumps(inner),
        "turn_complete": True,
    }


def insert_test_pending_message(
    user_id: str = USER_ID,
    chat_id: str = TEST_CHAT_ID,
    content: str = "Test pending message for WebSocket.",
) -> str:
    """
    Insert a new row into messages and a new row into pending_text_message_jobs
    so the WebSocket server will find pending messages for this user.
    Returns the message_id (UUID string).
    """
    message_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    execute_update(
        """
        INSERT INTO messages (chat_id, message_id, sender_id, content, created_at, is_read)
        VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s::timestamptz, false)
        """,
        (chat_id, message_id, user_id, content, created_at),
    )
    # Server's get_pending_messages_for_user JOINs on p.message_id; table must have (user_id, message_id).
    execute_update(
        """
        INSERT INTO pending_text_message_jobs (user_id, message_id)
        VALUES (%s::uuid, %s::uuid)
        """,
        (user_id, message_id),
    )
    print(f"📝 Inserted message {message_id} and pending_text_message_job for user {user_id}")
    return message_id


class AudioManager:
    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.in_stream = None
        self.out_stream = None
        self.play_queue = deque()
        self.playing_task = None
        self.is_running = True

    async def init(self):
        self.in_stream = self.p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=INPUT_RATE,
            input=True,
            frames_per_buffer=CHUNK,
        )
        self.out_stream = self.p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=OUTPUT_RATE,
            output=True,
        )
        print("🎤 Mic + 🔈 Speaker initialized")

    def read_mic(self) -> bytes:
        try:
            return self.in_stream.read(CHUNK, exception_on_overflow=False)
        except Exception as e:
            print(f"Error reading microphone: {e}")
            return b"\x00" * (CHUNK * 2)

    def queue_audio(self, data: bytes):
        if not self.is_running:
            return
        self.play_queue.append(data)
        if not self.playing_task or self.playing_task.done():
            loop = asyncio.get_running_loop()
            self.playing_task = loop.create_task(self._playback())

    async def _playback(self):
        while self.play_queue and self.is_running:
            try:
                data = self.play_queue.popleft()
                await asyncio.to_thread(self.out_stream.write, data)
            except Exception as e:
                print(f"Error playing audio: {e}")
                break

    def interrupt(self):
        self.play_queue.clear()
        if self.playing_task and not self.playing_task.done():
            self.playing_task.cancel()
        print("🔇 Audio playback interrupted")

    def cleanup(self):
        self.is_running = False
        if self.in_stream:
            self.in_stream.close()
        if self.out_stream:
            self.out_stream.close()
        if self.p:
            self.p.terminate()
        print("🎤 Mic + 🔈 Speaker cleaned up")


async def check_is_active(user_id: str) -> bool:
    try:
        session = await asyncio.to_thread(get_session, user_id)
        if session is None:
            return False
        return bool(session.get("is_active", False))
    except Exception as e:
        print(f"Database error: {e}")
        return False


async def send_audio(ws, audio_mgr: AudioManager):
    while audio_mgr.is_running:
        try:
            mic_chunk = audio_mgr.read_mic()
            msg = {"audio": base64.b64encode(mic_chunk).decode("utf-8")}
            await ws.send(json.dumps(msg))
            await asyncio.sleep(0.01)
        except Exception as e:
            print(f"Error in send_audio: {e}")
            break


async def recv_audio(ws, audio_mgr: AudioManager):
    import websockets as ws_lib

    while audio_mgr.is_running:
        try:
            resp = await ws.recv()
            data = json.loads(resp)

            if data.get("interrupt"):
                print("🛑 Interrupt received")
                audio_mgr.interrupt()
            elif "audio" in data:
                audio_bytes = base64.b64decode(data["audio"])
                audio_mgr.queue_audio(audio_bytes)
            elif "output_text" in data:
                print(f"🗣️ Server: {data['output_text']}")
            elif "input_text" in data:
                print(f"👤 You: {data['input_text']}")
            elif "error" in data:
                print(f"❌ Server error: {data['error']}")
        except ws_lib.exceptions.ConnectionClosed:
            print("❌ WebSocket closed")
            break
        except Exception as e:
            print(f"Error in recv_audio: {e}")
            break


async def run_websocket_client():
    """Connect to WebSocket, send pending-message initial message, then stream audio."""
    audio_mgr = AudioManager()

    try:
        await audio_mgr.init()

        async with websockets.connect(WS_URI) as ws:
            print(f"🚀 Connected to WebSocket → {WS_URI}")

            init_msg = build_iot_hub_init() if USE_IOT_HUB_INIT else build_pending_message_init()
            init_str = json.dumps(init_msg)
            await ws.send(init_str)
            print(f"📤 Initial message (full): {init_msg.get('turns', init_msg)!s}" if USE_IOT_HUB_INIT else "📨 Initial message sent (pending_message, pending_task=false)")

            await asyncio.gather(
                send_audio(ws, audio_mgr),
                recv_audio(ws, audio_mgr),
            )

    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        audio_mgr.cleanup()


async def main_loop():
    try:
        while True:
            is_active = await check_is_active(USER_ID)

            if is_active:
                print("⏳ is_active = TRUE → deferring 60s")
                await asyncio.sleep(60)
                continue
            else:
                print("✅ is_active = FALSE (or no row) → inserting test message + pending job, then starting WebSocket client...")
                await asyncio.to_thread(insert_test_pending_message)
                await run_websocket_client()

            print("ℹ️ WebSocket session ended → recheck in 15s")
            await asyncio.sleep(15)

    except KeyboardInterrupt:
        print("\n🛑 Stopping service...")


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except Exception as e:
        print(f"Unhandled error: {e}")
        sys.exit(1)
