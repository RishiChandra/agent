"""Interactive WebSocket client for ``/ws/developer/{user_id}`` (Gemini STT/TTS loop).

Mirrors ``test/app/test_ws.py``: PyAudio mic → server → decode downlink Opus → speaker.
Batches uplink PCM (~1.5s of 16 kHz mono) with ``turn_complete: false`` so the server
coalesces audio until ~1.2s of silence, then runs one STT → Gemini → TTS cycle per utterance.

Run from repo root (with venv that has pyaudio, websockets, opuslib):

    python test/app/developer/test_developer_ws.py

Requires FastAPI listening (e.g. ``uvicorn app.main:app``), ``VOSK_MODEL_PATH``, and Gemini keys in ``.env``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys
from collections import deque

import websockets

# macOS Homebrew: libopus path for opuslib (same as test_ws).
if sys.platform == "darwin":
    _brew_lib = "/opt/homebrew/lib"
    if os.path.isdir(_brew_lib):
        os.environ["DYLD_LIBRARY_PATH"] = (
            _brew_lib + ":" + os.environ.get("DYLD_LIBRARY_PATH", "")
        )

_app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _app_dir)
from utils import (
    CHUNK,
    CHANNELS,
    FORMAT,
    INPUT_RATE,
    OUTPUT_RATE,
    _connection_ring,
    _disconnection_ring,
    decode_websocket_audio_for_playback,
)

# ~1.5s of mic at 16 kHz mono int16: CHUNK samples per read, 2 bytes/sample.
_BYTES_PER_MIC_READ = CHUNK * 2
# 16000 samples/sec * 1.5 sec * 2 bytes ≈ 48000; use chunk-aligned batches.
_BATCH_CHUNKS = 48


class AudioManager:
    """Local PyAudio capture + playback (same pattern as ``test_ws``)."""

    def __init__(self):
        import pyaudio

        self.p = pyaudio.PyAudio()
        self.in_stream = None
        self.out_stream = None
        self.play_queue: deque[bytes] = deque()
        self.playing_task: asyncio.Task | None = None
        self.is_running = True

    async def init(self):
        try:
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
            print("🎤 Mic + 🔈 Speaker initialized (developer_ws test)")
        except Exception as e:
            print(f"Error initializing audio: {e}")
            raise

    def read_mic(self) -> bytes:
        try:
            return self.in_stream.read(CHUNK, exception_on_overflow=False)
        except Exception as e:
            print(f"Error reading microphone: {e}")
            return b"\x00" * _BYTES_PER_MIC_READ

    def queue_audio(self, data: bytes) -> None:
        if not self.is_running:
            return
        self.play_queue.append(data)
        if not self.playing_task or self.playing_task.done():
            self.playing_task = asyncio.create_task(self._playback())

    async def _playback(self) -> None:
        while self.play_queue and self.is_running:
            try:
                data = self.play_queue.popleft()
                await asyncio.to_thread(self.out_stream.write, data)
            except Exception as e:
                print(f"Error playing audio: {e}")
                break

    def interrupt(self) -> None:
        self.play_queue.clear()
        if self.playing_task and not self.playing_task.done():
            self.playing_task.cancel()
        print("🔇 Audio playback interrupted (client)")

    def cleanup(self) -> None:
        self.is_running = False
        if self.in_stream:
            self.in_stream.close()
        if self.out_stream:
            self.out_stream.close()
        if self.p:
            self.p.terminate()
        print("🎤 Mic + 🔈 cleaned up")


async def run_developer_ws_session() -> None:
    user_id = os.environ.get(
        "DEVELOPER_WS_USER_ID", "2ba330c0-a999-46f8-ba2c-855880bdcf5b"
    )
    base = os.environ.get("DEVELOPER_WS_BASE", "ws://localhost:8000").rstrip("/")
    #uri = f"{base}/ws/developer/{user_id}"
    uri = f"wss://websocket-ai-pin-fbbrhfawfkb7ecf3.westus2-01.azurewebsites.net/ws/developer/{user_id}"
    # Example remote: wss://...azurewebsites.net/ws/developer/{user_id}

    audio_mgr = AudioManager()
    try:
        await audio_mgr.init()

        disconnection_played = False

        async with websockets.connect(
            uri, open_timeout=60, ping_interval=20, ping_timeout=20, max_size=None
        ) as ws:
            print(f"✅ Connected to developer WebSocket: {uri}")
            await asyncio.to_thread(_connection_ring, audio_mgr.p)

            async def send_audio() -> None:
                batch = bytearray()
                while audio_mgr.is_running:
                    try:
                        mic_chunk = await asyncio.to_thread(audio_mgr.read_mic)
                        batch.extend(mic_chunk)
                        if len(batch) >= _BYTES_PER_MIC_READ * _BATCH_CHUNKS:
                            msg = {
                                "audio": base64.b64encode(bytes(batch)).decode(
                                    "utf-8"
                                ),
                                "turn_complete": False,
                            }
                            await ws.send(json.dumps(msg))
                            batch.clear()
                            print(
                                f"📤 Sent batched uplink (~{_BATCH_CHUNKS * CHUNK / INPUT_RATE:.1f}s PCM)"
                            )
                            await asyncio.sleep(0.05)
                    except Exception as e:
                        print(f"Error in send_audio: {e}")
                        break

            async def recv_audio() -> None:
                nonlocal disconnection_played
                while audio_mgr.is_running:
                    try:
                        resp = await ws.recv()
                        data = json.loads(resp)

                        if data.get("interrupt"):
                            print("🛑 Interrupt from server")
                            audio_mgr.interrupt()
                            continue
                        if "audio" in data:
                            pcm = decode_websocket_audio_for_playback(data)
                            if pcm:
                                audio_mgr.queue_audio(pcm)
                        elif "turn_complete" in data:
                            print("✅ Turn complete (server message)")
                        elif "error" in data:
                            print(f"❌ Server error: {data['error']}")
                    except websockets.exceptions.ConnectionClosed:
                        print("❌ WebSocket closed")
                        disconnection_played = True
                        await asyncio.to_thread(_disconnection_ring, audio_mgr.p)
                        break
                    except Exception as e:
                        print(f"Error in recv_audio: {e}")
                        break

            await asyncio.gather(send_audio(), recv_audio())

        if not disconnection_played:
            await asyncio.to_thread(_disconnection_ring, audio_mgr.p)
        print("🔔 Disconnected")

    except (ConnectionRefusedError, OSError) as e:
        print(
            f"❌ Connection refused. Start the API (e.g. uvicorn app.main:app). Error: {e}"
        )
    except Exception as e:
        print(f"Error in run_developer_ws_session: {e}")
    finally:
        audio_mgr.cleanup()


def _signal_handler(sig, frame) -> None:
    print("\n🛑 Shutting down…")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _signal_handler)
    try:
        asyncio.run(run_developer_ws_session())
    except KeyboardInterrupt:
        print("\n🛑 Interrupted")
    except Exception as e:
        print(f"Unhandled: {e}")
        sys.exit(1)
