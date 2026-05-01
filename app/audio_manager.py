import json
import base64
import asyncio
import audioop
import struct
from collections import deque
from fastapi import WebSocket

try:
    import opuslib
    OPUS_AVAILABLE = True
except (ImportError, OSError, Exception) as _opus_err:
    print(f"⚠️ Opus unavailable, falling back to raw PCM: {_opus_err}")
    opuslib = None
    OPUS_AVAILABLE = False


def _rms_int16_le(pcm: bytes) -> int:
    """RMS of little-endian int16 PCM bytes (C-speed via audioop)."""
    if not pcm:
        return 0
    return audioop.rms(pcm, 2)  # sample width = 2 bytes

# Match gemini_config.RECEIVE_SAMPLE_RATE — Gemini Live emits 24kHz mono int16 PCM.
SAMPLE_RATE = 24000
CHANNELS = 1

# Opus frame size: 40ms at 24kHz = 960 samples (matches Gemini chunk cadence).
# Opus supports 2.5/5/10/20/40/60ms frame sizes; 40ms = best compression for speech.
OPUS_FRAME_MS = 40
OPUS_FRAME_SAMPLES = SAMPLE_RATE * OPUS_FRAME_MS // 1000  # 960 samples
OPUS_FRAME_BYTES_PCM = OPUS_FRAME_SAMPLES * 2  # int16 mono = 1920 bytes per frame
# Opus bitrate target. 24 kbps = ~120 bytes per 40ms frame, transparent for speech.
# Cellular 40 KB/s observed; raw PCM @ 24kHz int16 = 48 KB/s baseline + 1.33x base64
# = 64 KB/s required. Opus 24kbps = 3 KB/s payload → 4 KB/s post-base64. 16x headroom.
OPUS_BITRATE = 24000

# Coalesce small Gemini chunks into ~5000ms blocks before sending.
# Each WebSocket frame has TLS + JSON + base64 + WS overhead — Opus shrinks per-bundle
# payload from ~240KB raw PCM to ~15KB Opus, fits cellular MTU/throughput easily.
# Bigger bundle = fewer WS frames per unit audio time = lower per-message overhead.
COALESCE_TARGET_MS = 5000
COALESCE_WAIT_S = COALESCE_TARGET_MS / 1000

# Drop silent Gemini chunks (natural prosody pauses emit PCM zeros).
# RMS metric retained for telemetry only — silent chunks now passed through to keep
# bundle cadence steady (see add_audio docstring).
SILENCE_DROP_RMS = 30


def _pack_opus_tlv(opus_packets: list[bytes]) -> bytes:
    """Pack a list of Opus packets as TLV: [u16 BE len][opus_bytes]... per frame.

    ESP32 walks the packed bytes, decodes each Opus packet to PCM, feeds I2S.
    Length prefix is 2 bytes big-endian. Max Opus packet at 24kbps/40ms ~250B,
    fits in u16 with margin. Total overhead = 2 bytes per 40ms frame = 50B/sec.
    """
    out = bytearray()
    for pkt in opus_packets:
        out += struct.pack(">H", len(pkt))
        out += pkt
    return bytes(out)


class AudioManager:
    """Manages audio queues and state for the websocket connection.

    Output uses Opus compression: each Gemini 40ms PCM chunk is encoded to ~120B
    Opus packet (vs 1920B raw → 16x reduction). Bundles ~125 packets (5s) and
    sends as TLV-packed binary inside JSON+base64 envelope. ESP32 decodes per-packet.
    """

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        # Input audio queue: client → Gemini (for user speech)
        self.audio_queue = asyncio.Queue()
        # Output audio queue: Gemini → client (for agent speech)
        # Items: (opus_bytes, gseq, t_recv_ms, chunk_ms, rms)
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
        # Per-connection Opus encoder. VOIP application = optimized for speech.
        # Disabled if opuslib unavailable (libopus.so missing on host) — falls back to raw PCM.
        self._opus_encoder = None
        if OPUS_AVAILABLE:
            try:
                self._opus_encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, opuslib.APPLICATION_VOIP)
                self._opus_encoder.bitrate = OPUS_BITRATE
            except Exception as e:
                print(f"⚠️ Opus encoder init failed, falling back to PCM: {e}")
                self._opus_encoder = None
        # Carry-over PCM buffer: Gemini chunks aren't always exact 40ms multiples,
        # so accumulate samples and emit Opus packets only when we have a full frame.
        self._pcm_residual = b""

    def _encode_pcm_to_opus_packets(self, pcm: bytes) -> list[bytes]:
        """Slice PCM into OPUS_FRAME_BYTES_PCM chunks, encode each. Carry over partial.

        Gemini chunks are typically 1920B (= one 40ms frame), but may vary. Residual
        is held until next add_audio call or turn end (where it's zero-padded).
        """
        if not self._opus_encoder:
            return []
        buf = self._pcm_residual + pcm
        packets = []
        i = 0
        while i + OPUS_FRAME_BYTES_PCM <= len(buf):
            frame = buf[i:i + OPUS_FRAME_BYTES_PCM]
            opus_pkt = self._opus_encoder.encode(frame, OPUS_FRAME_SAMPLES)
            packets.append(opus_pkt)
            i += OPUS_FRAME_BYTES_PCM
        self._pcm_residual = buf[i:]
        return packets

    def add_audio(self, audio_data):
        """Add Gemini PCM chunk to queue, encoded as Opus packets.

        Pass through ALL Gemini chunks (voiced + silent) — silent-drop breaks
        bundle cadence on cellular. Opus encoder handles silence efficiently
        (DTX optional, encoded silence ~10 bytes vs 1920B raw).
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
        is_silent = rms < SILENCE_DROP_RMS

        if self._opus_encoder:
            opus_packets = self._encode_pcm_to_opus_packets(audio_data)
            if not opus_packets:
                return  # sub-frame, residual buffered
            for pkt in opus_packets:
                self.audio_playback_queue.append((pkt, gseq, t_recv_ms, OPUS_FRAME_MS))
            opus_total = sum(len(p) for p in opus_packets)
            print(f"🎙️ Gemini chunk in: gseq={gseq} t_recv={t_recv_ms}ms dt_gemini={dt_gemini}ms "
                  f"pcm={len(audio_data)}B opus={opus_total}B (~{chunk_ms}ms, {len(opus_packets)} frames) "
                  f"rms={rms}{' SILENT' if is_silent else ''} qdepth={len(self.audio_playback_queue)}")
        else:
            # PCM fallback path
            self.audio_playback_queue.append((audio_data, gseq, t_recv_ms, chunk_ms))
            print(f"🎙️ Gemini chunk in: gseq={gseq} t_recv={t_recv_ms}ms dt_gemini={dt_gemini}ms "
                  f"PCM {len(audio_data)}B (~{chunk_ms}ms) "
                  f"rms={rms}{' SILENT' if is_silent else ''} qdepth={len(self.audio_playback_queue)}")
        self._wake_event.set()

        if self.playback_task is None or self.playback_task.done():
            self.playback_task = asyncio.create_task(self._play_audio())

    def mark_turn_complete(self):
        """Signal end of Gemini speech turn. Pacing loop drains queue then exits."""
        # Flush any residual PCM as a final padded Opus frame so no audio is lost.
        if self._pcm_residual:
            pad = OPUS_FRAME_BYTES_PCM - len(self._pcm_residual)
            if pad > 0:
                padded = self._pcm_residual + b"\x00" * pad
            else:
                padded = self._pcm_residual[:OPUS_FRAME_BYTES_PCM]
            try:
                opus_pkt = self._opus_encoder.encode(padded, OPUS_FRAME_SAMPLES)
                self._gemini_seq += 1
                t_recv_ms = int((asyncio.get_event_loop().time() - self._emit_t0) * 1000)
                self.audio_playback_queue.append((opus_pkt, self._gemini_seq, t_recv_ms, OPUS_FRAME_MS))
                print(f"🧹 Flushed {len(self._pcm_residual)}B PCM residual as final Opus frame ({len(opus_pkt)}B)")
            except Exception as e:
                print(f"⚠️  Error flushing residual: {e}")
            self._pcm_residual = b""

        print(f"🏁 mark_turn_complete called (qdepth={len(self.audio_playback_queue)})")
        self._turn_active = False
        self._wake_event.set()

    async def _play_audio(self):
        """Bundle queued Opus packets and emit as JSON+base64 binary blob.

        Wait up to COALESCE_WAIT_S for queue to fill bundle to COALESCE_TARGET_MS
        of audio (counted by frame ms, not bytes — Opus packets are tiny).
        """
        loop = asyncio.get_event_loop()
        emit_idx = 0
        print(f"🎬 _play_audio START (turn_active={self._turn_active} qdepth={len(self.audio_playback_queue)})")
        try:
            while self._turn_active or self.audio_playback_queue:
                if not self.audio_playback_queue:
                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=0.1)
                    except asyncio.TimeoutError:
                        pass
                    continue

                # Force-coalesce: drain queue into one bundle up to COALESCE_TARGET_MS
                # of audio. If queue empty before target hit, wait up to COALESCE_WAIT_S.
                first = self.audio_playback_queue.popleft()
                opus_packets = [first[0]]
                gseq_first = first[1]
                t_recv_first = first[2]
                bundled_ms = first[3]
                gseq_last = gseq_first
                bundle_deadline = loop.time() + COALESCE_WAIT_S
                while bundled_ms < COALESCE_TARGET_MS:
                    if not self.audio_playback_queue:
                        if not self._turn_active:
                            break
                        remaining = bundle_deadline - loop.time()
                        if remaining <= 0:
                            break
                        self._wake_event.clear()
                        try:
                            await asyncio.wait_for(self._wake_event.wait(), timeout=remaining)
                        except asyncio.TimeoutError:
                            break
                        if not self.audio_playback_queue:
                            continue
                    nxt = self.audio_playback_queue.popleft()
                    opus_packets.append(nxt[0])
                    gseq_last = nxt[1]
                    bundled_ms += nxt[3]

                # opus_packets here may be Opus packets OR raw PCM chunks depending on encoder
                n_bundled = len(opus_packets)
                self._emit_seq += 1
                seq = self._emit_seq
                t_emit_ms = int((loop.time() - self._emit_t0) * 1000)
                dwell_ms = t_emit_ms - t_recv_first

                if self._opus_encoder:
                    packed = _pack_opus_tlv(opus_packets)
                    payload = {
                        "audio": base64.b64encode(packed).decode("utf-8"),
                        "codec": "opus",
                        "sample_rate": SAMPLE_RATE,
                        "frame_ms": OPUS_FRAME_MS,
                        "n_frames": n_bundled,
                        "audio_ms": bundled_ms,
                        "seq": seq,
                        "t_emit_ms": t_emit_ms,
                        "gseq_first": gseq_first,
                        "gseq_last": gseq_last,
                    }
                    payload_log = f"OPUS {sum(len(p) for p in opus_packets)}B+TLV→{len(packed)}B"
                else:
                    pcm_blob = b"".join(opus_packets)
                    payload = {
                        "audio": base64.b64encode(pcm_blob).decode("utf-8"),
                        "audio_ms": bundled_ms,
                        "seq": seq,
                        "t_emit_ms": t_emit_ms,
                        "gseq_first": gseq_first,
                        "gseq_last": gseq_last,
                    }
                    payload_log = f"PCM {len(pcm_blob)}B"

                t_send_start = loop.time()
                await self.websocket.send_text(json.dumps(payload))
                send_ms = (loop.time() - t_send_start) * 1000

                emit_idx += 1
                print(f"📤 emit#{emit_idx} seq={seq} t_emit={t_emit_ms}ms "
                      f"gseq=[{gseq_first}..{gseq_last}] n={n_bundled} dwell={dwell_ms}ms "
                      f"{payload_log} (~{bundled_ms}ms) "
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

        self._turn_active = False
        self.audio_playback_queue.clear()
        self._pcm_residual = b""
        self._wake_event.set()

        if self.playback_task and not self.playback_task.done():
            self.playback_task.cancel()
            try:
                await self.playback_task
            except asyncio.CancelledError:
                pass

        self.playback_task = None

        try:
            await self.websocket.send_text(json.dumps({"interrupt": True}))
        except Exception as e:
            print(f"Error sending interrupt signal: {e}")

        print("✅ Audio playback interrupted and cleared")

    def is_playing(self):
        """Check if audio is currently playing or queued."""
        return bool(self.audio_playback_queue) or (self.playback_task and not self.playback_task.done())
