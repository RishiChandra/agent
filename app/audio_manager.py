import json
import base64
import asyncio
from collections import deque
from fastapi import WebSocket

from audio_codec import (
    COALESCE_TARGET_MS,
    COALESCE_WAIT_S,
    DOWNLINK_SAMPLE_RATE,
    OPUS_FRAME_MS,
    SILENCE_DROP_RMS,
    UPLINK_FRAME_SAMPLES,
    UPLINK_SAMPLE_RATE,
    DownlinkOpusEncoder,
    UplinkOpusDecoder,
    pack_opus_tlv,
    rms_int16_le,
)

# Back-compat re-exports for callers that imported constants from audio_manager.
SAMPLE_RATE = DOWNLINK_SAMPLE_RATE


class AudioManager:
    """Audio queues and downlink encoding for the Gemini Live websocket path."""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.audio_queue = asyncio.Queue()
        self.audio_playback_queue = deque()
        self.playback_task = None
        self._turn_active = False
        self._wake_event = asyncio.Event()
        self._emit_seq = 0
        self._emit_t0 = asyncio.get_event_loop().time()
        self._gemini_seq = 0
        self._last_gemini_recv_ms = None
        self._downlink = DownlinkOpusEncoder()
        self._uplink_decoder = UplinkOpusDecoder()

    def decode_uplink_opus(
        self,
        tlv: bytes,
        sample_rate: int = UPLINK_SAMPLE_RATE,
        frame_samples: int = UPLINK_FRAME_SAMPLES,
    ) -> bytes:
        return self._uplink_decoder.decode_tlv(tlv, sample_rate, frame_samples)

    def add_audio(self, audio_data):
        self._turn_active = True
        rms = rms_int16_le(audio_data)
        chunk_ms = int((len(audio_data) / 2) * 1000 / DOWNLINK_SAMPLE_RATE)
        loop = asyncio.get_event_loop()
        t_recv_ms = int((loop.time() - self._emit_t0) * 1000)
        self._gemini_seq += 1
        gseq = self._gemini_seq
        dt_gemini = (
            (t_recv_ms - self._last_gemini_recv_ms)
            if self._last_gemini_recv_ms is not None
            else 0
        )
        self._last_gemini_recv_ms = t_recv_ms
        is_silent = rms < SILENCE_DROP_RMS

        if self._downlink.uses_opus:
            opus_packets = self._downlink.encode_pcm(audio_data)
            if not opus_packets:
                return
            for pkt in opus_packets:
                self.audio_playback_queue.append((pkt, gseq, t_recv_ms, OPUS_FRAME_MS))
            opus_total = sum(len(p) for p in opus_packets)
            print(
                f"🎙️ Gemini chunk in: gseq={gseq} t_recv={t_recv_ms}ms dt_gemini={dt_gemini}ms "
                f"pcm={len(audio_data)}B opus={opus_total}B (~{chunk_ms}ms, {len(opus_packets)} frames) "
                f"rms={rms}{' SILENT' if is_silent else ''} qdepth={len(self.audio_playback_queue)}"
            )
        else:
            self.audio_playback_queue.append((audio_data, gseq, t_recv_ms, chunk_ms))
            print(
                f"🎙️ Gemini chunk in: gseq={gseq} t_recv={t_recv_ms}ms dt_gemini={dt_gemini}ms "
                f"PCM {len(audio_data)}B (~{chunk_ms}ms) "
                f"rms={rms}{' SILENT' if is_silent else ''} qdepth={len(self.audio_playback_queue)}"
            )
        self._wake_event.set()

        if self.playback_task is None or self.playback_task.done():
            self.playback_task = asyncio.create_task(self._play_audio())

    def mark_turn_complete(self):
        for pkt in self._downlink.flush_residual():
            self._gemini_seq += 1
            t_recv_ms = int((asyncio.get_event_loop().time() - self._emit_t0) * 1000)
            self.audio_playback_queue.append(
                (pkt, self._gemini_seq, t_recv_ms, OPUS_FRAME_MS)
            )
            print(
                f"🧹 Flushed downlink PCM residual as final Opus frame ({len(pkt)}B)"
            )

        print(f"🏁 mark_turn_complete called (qdepth={len(self.audio_playback_queue)})")
        self._turn_active = False
        self._wake_event.set()

    async def _play_audio(self):
        loop = asyncio.get_event_loop()
        emit_idx = 0
        print(
            f"🎬 _play_audio START (turn_active={self._turn_active} qdepth={len(self.audio_playback_queue)})"
        )
        try:
            while self._turn_active or self.audio_playback_queue:
                if not self.audio_playback_queue:
                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=0.1)
                    except asyncio.TimeoutError:
                        pass
                    continue

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

                n_bundled = len(opus_packets)
                self._emit_seq += 1
                seq = self._emit_seq
                t_emit_ms = int((loop.time() - self._emit_t0) * 1000)
                dwell_ms = t_emit_ms - t_recv_first

                if self._downlink.uses_opus:
                    packed = pack_opus_tlv(opus_packets)
                    payload = {
                        "audio": base64.b64encode(packed).decode("utf-8"),
                        "codec": "opus",
                        "sample_rate": DOWNLINK_SAMPLE_RATE,
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
                print(
                    f"📤 emit#{emit_idx} seq={seq} t_emit={t_emit_ms}ms "
                    f"gseq=[{gseq_first}..{gseq_last}] n={n_bundled} dwell={dwell_ms}ms "
                    f"{payload_log} (~{bundled_ms}ms) "
                    f"send={send_ms:.0f}ms qdepth={len(self.audio_playback_queue)}"
                )
            print(f"🎬 _play_audio END (emits={emit_idx})")
        except asyncio.CancelledError:
            print(f"🎬 _play_audio CANCELLED (emits={emit_idx})")
            raise
        except Exception as e:
            print(f"❌ Error playing audio: {e}")

    async def interrupt(self):
        print("🛑 Interrupting audio playback...")

        self._turn_active = False
        self.audio_playback_queue.clear()
        self._downlink.clear()
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
        return bool(self.audio_playback_queue) or (
            self.playback_task and not self.playback_task.done()
        )
