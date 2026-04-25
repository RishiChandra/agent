import json
import base64
import asyncio
from collections import deque
from fastapi import WebSocket

# Match gemini_config.RECEIVE_SAMPLE_RATE — Gemini Live emits 24kHz mono int16 PCM.
SAMPLE_RATE = 24000
SILENCE_CHUNK_MS = 40
SILENCE_CHUNK_SAMPLES = int(SAMPLE_RATE * SILENCE_CHUNK_MS / 1000)
SILENCE_CHUNK = b"\x00\x00" * SILENCE_CHUNK_SAMPLES  # int16 little-endian zeros


class AudioManager:
    """Manages audio queues and state for the websocket connection.

    Output is paced at real-time and gaps are bridged with silence chunks while
    a turn is active. Embedded clients (ESP32 over cellular) cannot absorb
    Gemini's natural inter-phrase pauses + cellular jitter without DMA underrun;
    desktop clients with PortAudio's large internal ring buffer don't notice.
    Continuous paced output makes both behave identically.
    """

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        # Input audio queue: client → Gemini (for user speech)
        self.audio_queue = asyncio.Queue()
        # Output audio queue: Gemini → client (for agent speech)
        self.audio_playback_queue = deque()
        self.playback_task = None
        # Turn state: when active, _play_audio emits silence between real chunks
        # so the client sees a continuous 24kHz stream.
        self._turn_active = False
        self._wake_event = asyncio.Event()

    def add_audio(self, audio_data):
        """Add audio data to the playback queue."""
        self.audio_playback_queue.append(audio_data)
        self._turn_active = True
        self._wake_event.set()

        if self.playback_task is None or self.playback_task.done():
            self.playback_task = asyncio.create_task(self._play_audio())

    def mark_turn_complete(self):
        """Signal end of Gemini speech turn. Pacing loop drains queue then exits."""
        self._turn_active = False
        self._wake_event.set()

    async def _play_audio(self):
        """Paced output: emit one chunk every chunk_duration; bridge gaps with silence."""
        loop = asyncio.get_event_loop()
        next_emit = loop.time()
        try:
            while self._turn_active or self.audio_playback_queue:
                if self.audio_playback_queue:
                    audio_data = self.audio_playback_queue.popleft()
                else:
                    # Queue empty but turn still active → emit silence to keep stream alive.
                    audio_data = SILENCE_CHUNK

                await self.websocket.send_text(json.dumps({
                    "audio": base64.b64encode(audio_data).decode("utf-8")
                }))

                # Pace at chunk's real-time duration (int16 mono → bytes/2 = samples).
                chunk_duration = (len(audio_data) / 2) / SAMPLE_RATE
                next_emit += chunk_duration
                sleep_for = next_emit - loop.time()
                if sleep_for > 0:
                    # Sleep, but wake early if new audio arrives or turn ends.
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=sleep_for)
                    except asyncio.TimeoutError:
                        pass
                    self._wake_event.clear()
                else:
                    # Behind schedule (slow network) — resync, no sleep.
                    next_emit = loop.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Error playing audio: {e}")

    async def interrupt(self):
        """Handle interruption by stopping playback and clearing queue."""
        print("🛑 Interrupting audio playback...")

        # Stop emitting silence and clear queued audio
        self._turn_active = False
        self.audio_playback_queue.clear()
        self._wake_event.set()

        # Cancel the playback task if it's running
        if self.playback_task and not self.playback_task.done():
            self.playback_task.cancel()
            try:
                await self.playback_task
            except asyncio.CancelledError:
                pass

        # Reset playback task to None so a new one can be created
        self.playback_task = None

        # Send interrupt signal to client
        try:
            await self.websocket.send_text(json.dumps({"interrupt": True}))
        except Exception as e:
            print(f"Error sending interrupt signal: {e}")

        print("✅ Audio playback interrupted and cleared")

    def is_playing(self):
        """Check if audio is currently playing or queued."""
        return bool(self.audio_playback_queue) or (self.playback_task and not self.playback_task.done())
