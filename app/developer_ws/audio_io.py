"""Per-connection audio I/O: uplink Opus decode + downlink Opus encode/coalesce/send."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import traceback
from collections import deque
from typing import Tuple

from fastapi import WebSocket
from starlette.websockets import WebSocketState

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

log = logging.getLogger("developer_ws")

# Queue tuple: (payload_bytes, chunk_seq, t_recv_ms, duration_ms).
QueueItem = Tuple[bytes, int, int, int]


class AudioIO:
    """Decodes uplink frames and pumps TTS PCM downlink, coalescing Opus frames into one message."""

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        self._queue: deque[QueueItem] = deque()
        self._task: asyncio.Task | None = None
        self._turn_active = False
        self._wake = asyncio.Event()
        self._t0 = time.monotonic()
        self._emit_seq = 0
        self._chunk_seq = 0
        self._last_chunk_recv_ms: int | None = None
        self._downlink = DownlinkOpusEncoder()
        self._uplink = UplinkOpusDecoder()

    def is_alive(self) -> bool:
        try:
            return self._ws.client_state == WebSocketState.CONNECTED
        except Exception:
            return False

    def decode_uplink_opus(
        self,
        tlv: bytes,
        sample_rate: int = UPLINK_SAMPLE_RATE,
        frame_samples: int = UPLINK_FRAME_SAMPLES,
    ) -> bytes:
        return self._uplink.decode_tlv(tlv, sample_rate, frame_samples)

    def add_playback_pcm(self, pcm: bytes) -> None:
        """Queue int16 mono PCM at DOWNLINK_SAMPLE_RATE for Opus encode + downlink."""
        if not pcm or not self.is_alive():
            return
        self._turn_active = True
        self._enqueue(pcm)
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._pump())

    def _enqueue(self, pcm: bytes) -> None:
        rms = rms_int16_le(pcm)
        chunk_ms = int((len(pcm) / 2) * 1000 / DOWNLINK_SAMPLE_RATE)
        t_recv_ms = int((time.monotonic() - self._t0) * 1000)
        self._chunk_seq += 1
        seq = self._chunk_seq
        dt = (t_recv_ms - self._last_chunk_recv_ms) if self._last_chunk_recv_ms is not None else 0
        self._last_chunk_recv_ms = t_recv_ms
        is_silent = rms < SILENCE_DROP_RMS

        if self._downlink.uses_opus:
            packets = self._downlink.encode_pcm(pcm)
            if not packets:
                return
            for pkt in packets:
                self._queue.append((pkt, seq, t_recv_ms, OPUS_FRAME_MS))
            log.debug(
                "pcm in seq=%d t_recv=%dms dt=%dms pcm=%dB opus=%dB (~%dms, %d fr) rms=%d%s q=%d",
                seq, t_recv_ms, dt, len(pcm), sum(len(p) for p in packets),
                chunk_ms, len(packets), rms, " SILENT" if is_silent else "", len(self._queue),
            )
        else:
            self._queue.append((pcm, seq, t_recv_ms, chunk_ms))
            log.debug(
                "PCM out seq=%d t_recv=%dms dt=%dms %dB (~%dms) rms=%d q=%d",
                seq, t_recv_ms, dt, len(pcm), chunk_ms, rms, len(self._queue),
            )

    def mark_turn_complete(self) -> None:
        # Flush any sub-frame Opus residual so the last partial chunk reaches the client.
        for pkt in self._downlink.flush_residual():
            self._chunk_seq += 1
            t_recv_ms = int((time.monotonic() - self._t0) * 1000)
            self._queue.append((pkt, self._chunk_seq, t_recv_ms, OPUS_FRAME_MS))
            log.debug("flushed residual Opus (%dB)", len(pkt))
        self._turn_active = False
        self._wake.set()

    async def shutdown_playback(self) -> None:
        """Stop the pump silently (no client notification) — for shutdown / dead socket."""
        await self._stop_pump()

    async def interrupt(self) -> None:
        """Stop the pump and notify the client to clear its playback buffer."""
        await self._stop_pump()
        if self.is_alive():
            try:
                await self._ws.send_text(json.dumps({"interrupt": True}))
            except Exception as e:
                log.warning("interrupt notify failed: %s", e)

    async def _stop_pump(self) -> None:
        self._turn_active = False
        self._queue.clear()
        self._downlink.clear()
        self._wake.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def is_playing(self) -> bool:
        return bool(self._queue) or (self._task is not None and not self._task.done())

    async def _pump(self) -> None:
        emit_idx = 0
        try:
            while self._turn_active or self._queue:
                if not self._queue:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=0.1)
                    except asyncio.TimeoutError:
                        pass
                    continue

                bundle = await self._collect_bundle()
                if bundle is None:
                    continue
                if not await self._emit_bundle(bundle, emit_idx):
                    break
                emit_idx += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("playback error: %s", e)
            traceback.print_exc()

    async def _collect_bundle(self) -> dict | None:
        """Pull one queue item, then keep pulling until ~COALESCE_TARGET_MS of audio is bundled."""
        first = self._queue.popleft()
        packets = [first[0]]
        gseq_first = first[1]
        t_recv_first = first[2]
        bundled_ms = first[3]
        gseq_last = gseq_first

        # Brief wait for late-arriving frames so first emit isn't a 20ms blip.
        deadline = asyncio.get_running_loop().time() + COALESCE_WAIT_S
        while bundled_ms < COALESCE_TARGET_MS:
            if not self._queue:
                if not self._turn_active:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if not self._queue:
                    continue
            nxt = self._queue.popleft()
            packets.append(nxt[0])
            gseq_last = nxt[1]
            bundled_ms += nxt[3]

        return {
            "packets": packets,
            "gseq_first": gseq_first,
            "gseq_last": gseq_last,
            "bundled_ms": bundled_ms,
            "t_recv_first": t_recv_first,
        }

    def _build_payload(self, bundle: dict) -> Tuple[dict, str]:
        self._emit_seq += 1
        seq = self._emit_seq
        t_emit_ms = int((time.monotonic() - self._t0) * 1000)
        n = len(bundle["packets"])

        if self._downlink.uses_opus:
            packed = pack_opus_tlv(bundle["packets"])
            payload = {
                "audio": base64.b64encode(packed).decode("utf-8"),
                "codec": "opus",
                "sample_rate": DOWNLINK_SAMPLE_RATE,
                "frame_ms": OPUS_FRAME_MS,
                "n_frames": n,
                "audio_ms": bundle["bundled_ms"],
                "seq": seq,
                "t_emit_ms": t_emit_ms,
                "gseq_first": bundle["gseq_first"],
                "gseq_last": bundle["gseq_last"],
            }
            log_blob = f"OPUS {sum(len(p) for p in bundle['packets'])}B+TLV→{len(packed)}B"
        else:
            blob = b"".join(bundle["packets"])
            payload = {
                "audio": base64.b64encode(blob).decode("utf-8"),
                "audio_ms": bundle["bundled_ms"],
                "seq": seq,
                "t_emit_ms": t_emit_ms,
                "gseq_first": bundle["gseq_first"],
                "gseq_last": bundle["gseq_last"],
            }
            log_blob = f"PCM {len(blob)}B"
        return payload, log_blob

    async def _emit_bundle(self, bundle: dict, emit_idx: int) -> bool:
        """Send one bundled message. Returns False if the socket is dead/the send fails (caller exits)."""
        payload, log_blob = self._build_payload(bundle)
        if not self.is_alive():
            await self._stop_pump_inline()
            return False

        loop = asyncio.get_running_loop()
        t_send_start = loop.time()
        try:
            await self._ws.send_text(json.dumps(payload))
        except Exception as e:
            log.info("send stopped (%s): %r", type(e).__name__, e)
            await self._stop_pump_inline()
            return False
        send_ms = (loop.time() - t_send_start) * 1000

        log.debug(
            "emit#%d seq=%d t_emit=%dms chunk_seq=[%d..%d] n=%d dwell=%dms %s (~%dms) send=%.0fms q=%d",
            emit_idx + 1, payload["seq"], payload["t_emit_ms"],
            bundle["gseq_first"], bundle["gseq_last"], len(bundle["packets"]),
            payload["t_emit_ms"] - bundle["t_recv_first"],
            log_blob, bundle["bundled_ms"], send_ms, len(self._queue),
        )
        return True

    async def _stop_pump_inline(self) -> None:
        """Like _stop_pump but skips cancelling self._task (we are inside it)."""
        self._turn_active = False
        self._queue.clear()
        self._downlink.clear()
