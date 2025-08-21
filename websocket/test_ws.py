import asyncio
import websockets
import json
import base64
import pyaudio
from collections import deque
import signal
import sys

# ===== Audio Config =====
FORMAT = pyaudio.paInt16
CHANNELS = 1
INPUT_RATE = 16000   # matches Gemini SEND_SAMPLE_RATE
OUTPUT_RATE = 24000  # matches Gemini RECEIVE_SAMPLE_RATE
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
        try:
            # Mic input
            self.in_stream = self.p.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=INPUT_RATE,
                input=True,
                frames_per_buffer=CHUNK,
            )
            # Speaker output
            self.out_stream = self.p.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=OUTPUT_RATE,
                output=True,
            )
            print("🎤 Mic + 🔈 Speaker initialized")
        except Exception as e:
            print(f"Error initializing audio: {e}")
            raise

    def read_mic(self):
        try:
            return self.in_stream.read(CHUNK, exception_on_overflow=False)
        except Exception as e:
            print(f"Error reading microphone: {e}")
            return b'\x00' * (CHUNK * 2)  # Return silence on error

    def queue_audio(self, data: bytes):
        if not self.is_running:
            return
        self.play_queue.append(data)
        if not self.playing_task or self.playing_task.done():
            self.playing_task = asyncio.create_task(self._playback())

    async def _playback(self):
        while self.play_queue and self.is_running:
            try:
                data = self.play_queue.popleft()
                await asyncio.to_thread(self.out_stream.write, data)
            except Exception as e:
                print(f"Error playing audio: {e}")
                break

    def interrupt(self):
        """Handle interruption by stopping playback and clearing queue"""
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

async def test_ws():
    uri = "ws://localhost:8000/ws"
    audio_mgr = AudioManager()
    
    try:
        await audio_mgr.init()
        
        async with websockets.connect(uri) as ws:
            print("✅ Connected to FastAPI WebSocket")

            async def send_audio():
                """Continuously capture mic and send to server"""
                while audio_mgr.is_running:
                    try:
                        mic_chunk = audio_mgr.read_mic()
                        msg = {
                            "audio": base64.b64encode(mic_chunk).decode("utf-8")
                        }
                        await ws.send(json.dumps(msg))
                        await asyncio.sleep(0.01)  # throttle to avoid flooding
                    except Exception as e:
                        print(f"Error in send_audio: {e}")
                        break

            async def recv_audio():
                """Receive Gemini audio and play through speakers"""
                while audio_mgr.is_running:
                    try:
                        resp = await ws.recv()
                        data = json.loads(resp)
                        
                        if "interrupt" in data and data["interrupt"]:
                            print("🛑 Interrupt signal received from server")
                            audio_mgr.interrupt()  # Clear client-side audio queue
                            continue
                        elif "audio" in data:
                            audio_bytes = base64.b64decode(data["audio"])
                            audio_mgr.queue_audio(audio_bytes)
                        elif "turn_complete" in data:
                            print("✅ Turn complete")
                        elif "output_text" in data:
                            print(f"🗣️ Gemini said: {data['output_text']}")
                        elif "input_text" in data:
                            print(f"👤 You said: {data['input_text']}")
                        elif "error" in data:
                            print(f"❌ Server error: {data['error']}")
                            
                    except websockets.exceptions.ConnectionClosed:
                        print("❌ WebSocket connection closed")
                        break
                    except Exception as e:
                        print(f"Error in recv_audio: {e}")
                        break

            # Run both tasks concurrently
            await asyncio.gather(send_audio(), recv_audio())
            
    except websockets.exceptions.ConnectionRefused:
        print("❌ Connection refused. Make sure the FastAPI server is running on localhost:8000")
    except Exception as e:
        print(f"Error in test_ws: {e}")
    finally:
        audio_mgr.cleanup()

def signal_handler(sig, frame):
    print("\n🛑 Shutting down gracefully...")
    sys.exit(0)

if __name__ == "__main__":
    # Set up signal handler for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        asyncio.run(test_ws())
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user")
    except Exception as e:
        print(f"Unhandled exception: {e}")
        sys.exit(1)
