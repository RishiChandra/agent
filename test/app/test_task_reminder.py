import asyncio
import os
import sys
import json
import base64
from collections import deque

import psycopg2
import websockets
import pyaudio
from dotenv import load_dotenv

# ================================
# Load .env variables
# ================================
load_dotenv()

# ================================
# Constants (hardcoded user + URL)
# ================================
USER_ID = "2ba330c0-a999-46f8-ba2c-855880bdcf5b"
WS_URI = (
    #"ws://localhost:8000/ws/{USER_ID}"
    f"wss://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io/ws/{USER_ID}"
)

# ===== Audio Config =====
FORMAT = pyaudio.paInt16
CHANNELS = 1
INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHUNK = 512


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


# =================================================
# DATABASE HELPERS
# =================================================


def build_postgres_dsn():
    host = os.getenv("DB_HOST")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")

    missing = []
    for var, val in [
        ("DB_HOST", host),
        ("DB_NAME", name),
        ("DB_USER", user),
        ("DB_PASSWORD", password),
    ]:
        if not val:
            missing.append(var)

    if missing:
        print(f"❌ Missing .env variables: {', '.join(missing)}")
        sys.exit(1)

    # Azure Postgres typically needs sslmode=require
    return (
        f"host={host} port={port} dbname={name} "
        f"user={user} password={password} sslmode=require"
    )


def connect_db(dsn: str):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    print("✅ Connected to PostgreSQL")
    return conn


def fetch_is_active_sync(conn, user_id: str) -> bool:
    """
    Simple check for is_active in sessions table.

    Expected table schema:
      sessions(user_id, is_active)
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT is_active
            FROM sessions
            WHERE user_id = %s
            LIMIT 1;
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            # Treat "no row" as inactive
            return False
        return bool(row[0])


async def check_is_active(conn, user_id: str) -> bool:
    try:
        return await asyncio.to_thread(fetch_is_active_sync, conn, user_id)
    except Exception as e:
        print(f"Database error: {e}")
        return False


# =================================================
# WEBSOCKET AUDIO LOOPS
# =================================================


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
    """Connect to WebSocket, send initial message, then stream audio."""
    audio_mgr = AudioManager()

    try:
        await audio_mgr.init()

        async with websockets.connect(WS_URI) as ws:
            print(f"🚀 Connected to WebSocket → {WS_URI}")

            init_msg = {
                "turns": "Remind me of my tasks today",
                "turn_complete": True
            }
            await ws.send(json.dumps(init_msg))
            print("📨 Initial greeting sent")

            await asyncio.gather(
                send_audio(ws, audio_mgr),
                recv_audio(ws, audio_mgr),
            )

    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        audio_mgr.cleanup()


# =================================================
# MAIN LOOP (logic: TRUE = defer, FALSE = connect)
# =================================================


async def main_loop():
    dsn = build_postgres_dsn()
    conn = connect_db(dsn)

    try:
        while True:
            is_active = await check_is_active(conn, USER_ID)

            if is_active:
                print("⏳ is_active = TRUE → deferring 60s")
                await asyncio.sleep(60)
                continue
            else:
                print("✅ is_active = FALSE (or no row) → starting WebSocket client...")
                await run_websocket_client()

            print("ℹ️ WebSocket session ended → recheck in 15s")
            await asyncio.sleep(15)

    except KeyboardInterrupt:
        print("\n🛑 Stopping service...")
    finally:
        conn.close()
        print("🔌 PostgreSQL closed")


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except Exception as e:
        print(f"Unhandled error: {e}")
        sys.exit(1)
