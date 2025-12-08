import asyncio
import json
import base64
from collections import deque

import websockets
import pyaudio

# ================================
# Constants
# ================================
# WS_URI is now built dynamically based on user_id parameter

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


async def run_websocket_client(user_id: str, message: str = "Remind me of my tasks today"):
    """Connect to WebSocket, send initial message, then stream audio.
    
    Args:
        user_id: The user ID to connect with
        message: The text message to send to the websocket
    """
    # Build WS URI for the user
    ws_uri = (
        f"wss://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io/ws/{user_id}"
    )
    
    audio_mgr = AudioManager()

    try:
        await audio_mgr.init()

        async with websockets.connect(ws_uri) as ws:
            print(f"🚀 Connected to WebSocket → {ws_uri}")

            init_msg = {
                "turns": message,
                "turn_complete": True
            }
            await ws.send(json.dumps(init_msg))
            print(f"📨 Sent message: {message}")

            await asyncio.gather(
                send_audio(ws, audio_mgr),
                recv_audio(ws, audio_mgr),
            )

    except Exception as e:
        print(f"WebSocket error: {e}")
        raise
    finally:
        audio_mgr.cleanup()


def start_websocket_connection(user_id: str, message: str = "Remind me of my tasks today"):
    """Synchronous wrapper to start websocket connection.
    
    This function can be called from Azure Functions.
    
    Args:
        user_id: The user ID to connect with
        message: The text message to send to the websocket
    """
    try:
        asyncio.run(run_websocket_client(user_id, message))
    except Exception as e:
        print(f"Error starting websocket connection: {e}")
        raise


# =================================================
# STANDALONE EXECUTION (for testing)
# =================================================


if __name__ == "__main__":
    """Standalone execution for testing purposes."""
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    # Default user_id for testing
    user_id = os.getenv("USER_ID", "2ba330c0-a999-46f8-ba2c-855880bdcf5b")
    message = "Remind me of my tasks today"
    
    try:
        print(f"🧪 Testing websocket connection for user: {user_id}")
        start_websocket_connection(user_id, message)
    except Exception as e:
        print(f"Unhandled error: {e}")
        import sys
        sys.exit(1)
