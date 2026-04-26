import json
import base64
import asyncio
import audioop
from collections import deque
from fastapi import WebSocket


def _rms_int16_le(pcm: bytes) -> int:
    """RMS of little-endian int16 PCM bytes (C-speed via audioop)."""
    if not pcm:
        return 0
    return audioop.rms(pcm, 2)  # sample width = 2 bytes

# Match gemini_config.RECEIVE_SAMPLE_RATE — Gemini Live emits 24kHz mono int16 PCM.
SAMPLE_RATE = 24000

# Coalesce small Gemini chunks into ~1500ms blocks before sending.
# Each WebSocket frame has TLS + JSON + base64 + WS overhead — at 25 frames/sec
# (Gemini's 40ms chunks) cellular PPP modem chokes on per-message processing.
# Bundling ~37x chunks → <1 frame/sec, same data rate, drastically less overhead.
# Big bundles + ESP32 jitter buffer (1200ms) = ~2.7s underrun tolerance once
# pipeline primed. Survives cellular tower handoff / TCP retransmit stalls.
COALESCE_TARGET_MS = 1500
COALESCE_TARGET_BYTES = int(SAMPLE_RATE * COALESCE_TARGET_MS / 1000) * 2
# Max wait per bundle to fill to target. Gemini streams at ~realtime (1 chunk/40ms),
# so without waiting, queue empty after popleft → bundle = 1 chunk. Waiting up to
# COALESCE_TARGET_MS guarantees bundle fills to target. Trade first-audio latency
# (~1.5s + ESP32 buffer 1.2s = ~2.7s) for steady pipeline immune to cellular jitter.
COALESCE_WAIT_S = COALESCE_TARGET_MS / 1000

# Drop silent Gemini chunks (natural prosody pauses emit PCM zeros).
# Compresses wall-clock by skipping silent regions instead of pacing through them.
# Result: voiced audio plays back-to-back. RMS of int16 PCM; speech ~500+, room ~50, zeros = 0.
SILENCE_DROP_RMS = 30


class AudioManager:
    """Manages audio queues and state for the websocket connection.

    Output is paced at real-time but only voiced audio is sent — silent Gemini chunks
    are dropped at input. This compresses Gemini's natural inter-phrase pauses
    (~750ms PCM zeros) into back-to-back voiced playback, since Gemini Live emits
    chunks faster than realtime and our pacing only consumes voiced ones.
    Brief network gaps on the client are absorbed by ESP32 DMA silence-bridging.
    """

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        # Input audio queue: client → Gemini (for user speech)
        self.audio_queue = asyncio.Queue()
        # Output audio queue: Gemini → client (for agent speech)
        self.audio_playback_queue = deque()
        self.playback_task = None
        # Turn state: pacing loop exits when False AND queue empty.
        self._turn_active = False
        self._wake_event = asyncio.Event()
        # Per-chunk emit sequence + timestamp (ms since AudioManager init).
        # ESP32 echoes seq in its arrival logs so we can compute network latency
        # and inter-arrival jitter end-to-end.
        self._emit_seq = 0
        self._emit_t0 = asyncio.get_event_loop().time()
        # Gemini-side trace: per-chunk arrival seq + timestamp.
        # Carried alongside audio through queue so emit log can compute dwell
        # (t_emit - t_recv) and prove Gemini cadence (realtime vs burst).
        self._gemini_seq = 0
        self._last_gemini_recv_ms = None

    def add_audio(self, audio_data):
        """Add audio data to the playback queue.

        Silent chunks (RMS < SILENCE_DROP_RMS) are dropped — Gemini emits PCM zeros
        during prosody pauses, and pacing through them adds wall-clock silence the
        user perceives as a long gap. Dropping compresses to back-to-back voiced audio.
        """
        self._turn_active = True
        rms = _rms_int16_le(audio_data)
        chunk_ms = int((len(audio_data) / 2) * 1000 / SAMPLE_RATE)
        loop = asyncio.get_event_loop()
        t_recv_ms = int((loop.time() - self._emit_t0) * 1000)
        self._gemini_seq += 1
        gseq = self._gemini_seq
        dt_gemini = (t_recv_ms - self._last_gemini_recv_ms) if self._last_gemini_recv_ms is not None else 0
        self._last_gemini_recv_ms = t_recv_ms
        if rms < SILENCE_DROP_RMS:
            self._dropped_silent_chunks = getattr(self, "_dropped_silent_chunks", 0) + 1
            if self._dropped_silent_chunks % 25 == 1:
                print(f"🤫 Dropped silent Gemini chunk: gseq={gseq} t_recv={t_recv_ms}ms dt_gemini={dt_gemini}ms rms={rms} (~{chunk_ms}ms) total_dropped={self._dropped_silent_chunks}")
            return
        self.audio_playback_queue.append((audio_data, gseq, t_recv_ms))
        self._wake_event.set()
        print(f"🎙️ Gemini chunk in: gseq={gseq} t_recv={t_recv_ms}ms dt_gemini={dt_gemini}ms {len(audio_data)}B (~{chunk_ms}ms) rms={rms} qdepth={len(self.audio_playback_queue)}")

        if self.playback_task is None or self.playback_task.done():
            self.playback_task = asyncio.create_task(self._play_audio())

    def mark_turn_complete(self):
        """Signal end of Gemini speech turn. Pacing loop drains queue then exits."""
        print(f"🏁 mark_turn_complete called (qdepth={len(self.audio_playback_queue)})")
        self._turn_active = False
        self._wake_event.set()

    async def _play_audio(self):
        """Burst output: forward voiced Gemini audio ASAP — no pacing.

        Network is the rate limiter; ESP32 jitter buffer (400ms) + I2S DMA absorb
        bursts and play at strict 24kHz realtime. Sending faster than realtime fills
        ESP32's buffer cushion against cellular jitter.
        Silent Gemini chunks already dropped at add_audio().
        """
        loop = asyncio.get_event_loop()
        emit_idx = 0
        print(f"🎬 _play_audio START (turn_active={self._turn_active} qdepth={len(self.audio_playback_queue)})")
        try:
            while self._turn_active or self.audio_playback_queue:
                if not self.audio_playback_queue:
                    # Wait briefly for next voiced chunk (silent ones already dropped).
                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=0.1)
                    except asyncio.TimeoutError:
                        pass
                    continue

                # Force-coalesce: drain queue into one bundle up to COALESCE_TARGET_BYTES.
                # If queue empty before target hit, wait up to COALESCE_WAIT_S for next
                # Gemini chunk. Bail early if turn ends (drain remaining + flush).
                first = self.audio_playback_queue.popleft()
                parts = [first[0]]
                gseq_first = first[1]
                t_recv_first = first[2]
                bundled = len(parts[0])
                gseq_last = gseq_first
                bundle_deadline = loop.time() + COALESCE_WAIT_S
                while bundled < COALESCE_TARGET_BYTES:
                    if not self.audio_playback_queue:
                        if not self._turn_active:
                            break  # turn ended, flush partial bundle
                        remaining = bundle_deadline - loop.time()
                        if remaining <= 0:
                            break  # timeout, flush partial bundle
                        self._wake_event.clear()
                        try:
                            await asyncio.wait_for(self._wake_event.wait(), timeout=remaining)
                        except asyncio.TimeoutError:
                            break
                        if not self.audio_playback_queue:
                            continue  # woke for turn-end signal
                    nxt = self.audio_playback_queue.popleft()
                    parts.append(nxt[0])
                    gseq_last = nxt[1]
                    bundled += len(nxt[0])
                audio_data = b"".join(parts) if len(parts) > 1 else parts[0]
                n_bundled = len(parts)

                self._emit_seq += 1
                seq = self._emit_seq
                t_emit_ms = int((loop.time() - self._emit_t0) * 1000)
                dwell_ms = t_emit_ms - t_recv_first
                t_send_start = loop.time()
                await self.websocket.send_text(json.dumps({
                    "audio": base64.b64encode(audio_data).decode("utf-8"),
                    "seq": seq,
                    "t_emit_ms": t_emit_ms,
                    "gseq_first": gseq_first,
                    "gseq_last": gseq_last,
                }))
                send_ms = (loop.time() - t_send_start) * 1000

                emit_idx += 1
                chunk_ms = (len(audio_data) / 2) / SAMPLE_RATE * 1000
                print(f"📤 emit#{emit_idx} seq={seq} t_emit={t_emit_ms}ms "
                      f"gseq=[{gseq_first}..{gseq_last}] n={n_bundled} dwell={dwell_ms}ms "
                      f"AUD {len(audio_data)}B (~{int(chunk_ms)}ms) "
                      f"send={send_ms:.0f}ms qdepth={len(self.audio_playback_queue)}")
            print(f"🎬 _play_audio END (emits={emit_idx})")
        except asyncio.CancelledError:
            print(f"🎬 _play_audio CANCELLED (emits={emit_idx})")
            raise
        except Exception as e:
            print(f"❌ Error playing audio: {e}")

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
