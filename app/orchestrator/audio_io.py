"""Per-connection audio I/O.

Uplink: receives raw or Opus-encoded PCM frames from the client and decodes to int16 mono PCM.
Downlink: takes generated PCM (TTS or bridge-relay), Opus-encodes (or falls back to raw),
coalesces into batches, and writes to the client WebSocket. `mark_turn_complete` flushes any
partial Opus residual so the user always hears the tail of an assistant turn.
"""

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

# Queue tuple: (payload_bytes, chunk_seq, t_recv_ms, duration_ms, is_cue).
# is_cue marks the soft "thinking" pulse, which plays through this same downlink
# but must NOT count as the bot speaking (see add_cue_pcm / is_bot_speaking).
QueueItem = Tuple[bytes, int, int, int, bool]


class AudioIO:
    """Per-session audio gateway. One per WebSocket.

    Constructed by: `developer_websocket_endpoint` in endpoint.py.
    Used by:
      - `_decode_audio_payload` in endpoint.py → `decode_uplink_opus(tlv)` for Opus
        uplinks; raw uplinks bypass this.
      - `AudioIOSinkProcessor` (pipecat_bits.py) → `add_playback_pcm(pcm)` to queue
        TTS output and `mark_turn_complete()` on `BotStoppedSpeakingFrame`. Same
        processor calls `interrupt()` on `InterruptionFrame` and `shutdown_playback()`
        on `EndFrame` / `CancelFrame`.
      - `bridge._recv_loop` → `add_playback_pcm(pcm)` to queue remote bridge audio,
        plus `mark_turn_complete()` in its finally block so the last bridge chunk
        reaches the user even after a sudden remote close.
      - `_handle_interrupt` in endpoint.py → `interrupt()` to clear pending playback
        and notify the client (in addition to the pipeline's interrupt frame).
      - `_drain_on_close` in endpoint.py → `shutdown_playback()` on socket teardown.
    """

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
        # Estimated wall-clock time until which the *client* is still playing
        # bot audio. We send faster than real-time, so the server queue drains
        # long before the user stops hearing the bot; barge-in needs the
        # user-perceived window, not the queue state.
        self._audible_until = 0.0
        # Wall-clock horizon until which the client is (estimated) still playing
        # ANY bot output — speech OR the thinking cue. Advanced per emitted
        # bundle like `_audible_until`, but including cue bundles. The self-echo
        # guard is derived purely from this (plus a hangover), NOT from the pump
        # task, so a wedged pump can never leave the guard stuck on / the mic
        # deaf. Reset to 0 whenever playback is cleared.
        self._output_until = 0.0
        # Throttle state for the echo-guard drop log (1/sec).
        self._guard_drop_count = 0
        self._guard_drop_log_at = 0.0

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

    def add_cue_pcm(self, pcm: bytes) -> None:
        """Queue a short UI cue tone (the "thinking" pulse) for downlink.

        Plays through the same Opus/coalesce path as speech (so the client stream
        stays continuous), but — unlike `add_playback_pcm` — does NOT set
        `_turn_active` or advance the client-audible horizon. That keeps the cue
        out of `is_bot_speaking()`, so the barge-in gate never mistakes a soft
        ding for the assistant speaking, and the pump drains the tick and exits
        instead of spinning for the whole reply gap.
        """
        if not pcm or not self.is_alive():
            return
        self._enqueue(pcm, is_cue=True)
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._pump())

    def _enqueue(self, pcm: bytes, is_cue: bool = False) -> None:
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
                self._queue.append((pkt, seq, t_recv_ms, OPUS_FRAME_MS, is_cue))
            log.debug(
                "pcm in seq=%d t_recv=%dms dt=%dms pcm=%dB opus=%dB (~%dms, %d fr) rms=%d%s%s q=%d",
                seq, t_recv_ms, dt, len(pcm), sum(len(p) for p in packets),
                chunk_ms, len(packets), rms, " SILENT" if is_silent else "",
                " CUE" if is_cue else "", len(self._queue),
            )
        else:
            self._queue.append((pcm, seq, t_recv_ms, chunk_ms, is_cue))
            log.debug(
                "PCM out seq=%d t_recv=%dms dt=%dms %dB (~%dms) rms=%d q=%d",
                seq, t_recv_ms, dt, len(pcm), chunk_ms, rms, len(self._queue),
            )

    def mark_turn_complete(self) -> None:
        # Flush any sub-frame Opus residual so the last partial chunk reaches the client.
        for pkt in self._downlink.flush_residual():
            self._chunk_seq += 1
            t_recv_ms = int((time.monotonic() - self._t0) * 1000)
            self._queue.append((pkt, self._chunk_seq, t_recv_ms, OPUS_FRAME_MS, False))
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
        self._audible_until = 0.0
        # Clear the echo guard too, so a deliberate barge-in interrupt lets the
        # user's barging speech be captured immediately (no lingering hangover).
        self._output_until = 0.0
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

    def is_bot_audible(self) -> bool:
        """True while the user is (estimated) still hearing ANY downlink audio.

        Combines server-side state (queue/pump) with the client-side playback
        horizon accumulated in `_emit_bundle`. Includes the soft "thinking" cue,
        so it is used for the end-of-call drain (don't cut audio mid-play), NOT
        for barge-in — see `is_bot_speaking`.
        """
        return self.is_playing() or time.monotonic() < self._audible_until

    def is_bot_speaking(self) -> bool:
        """True while a REAL bot turn is (estimated) audible to the user.

        Unlike `is_bot_audible`, this excludes the "thinking" cue (queued via
        `add_cue_pcm`, which never sets `_turn_active` nor advances the audible
        horizon). The endpoint uses this to decide whether a deliberate loud
        barge-in should interrupt — only real bot speech can be interrupted, not
        the soft ding.
        """
        return self._turn_active or time.monotonic() < self._audible_until

    def output_guard_active(self, hangover_s: float = 0.0) -> bool:
        """True while the bot's OWN audio — speech or the thinking cue — is (or
        is within `hangover_s` of) still playing on the client.

        The endpoint uses this as a self-echo guard: while it is True the open
        mic is mostly hearing the bot, so uplink is dropped rather than fed to
        STT (otherwise the orchestrator transcribes its own greeting/reply/ding
        and answers itself in a loop). Includes the cue (unlike
        `is_bot_speaking`). Derived PURELY from the time-based playback horizon
        `_output_until` — never from the pump task / `is_playing()` — so it
        always decays and a wedged pump can't leave the mic permanently deaf.
        """
        return time.monotonic() < self._output_until + hangover_s

    def note_guard_drop(self, rms: int, user_id: str = "") -> None:
        """Count an echo-guard-dropped uplink frame; log at most once per second
        so a diagnosis shows frames ARE arriving and are being guarded (vs. no
        uplink at all, which logs nothing)."""
        self._guard_drop_count += 1
        now = time.monotonic()
        if now - self._guard_drop_log_at >= 1.0:
            log.info(
                "echo-guard dropped %d uplink frame(s) user_id=%s rms=%d output_in=%.2fs",
                self._guard_drop_count, user_id, rms, self._output_until - now,
            )
            self._guard_drop_log_at = now
            self._guard_drop_count = 0

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
            # Clear turn/queue/guard state so a crashed pump (with items still
            # queued) can't wedge is_playing()/output_guard_active() True forever
            # — which would leave the self-echo guard on and the mic permanently
            # deaf. State is reset inline (we ARE the task; don't cancel self).
            await self._stop_pump_inline()

    async def _collect_bundle(self) -> dict | None:
        """Pull one queue item, then keep pulling until ~COALESCE_TARGET_MS of audio is bundled."""
        first = self._queue.popleft()
        packets = [first[0]]
        gseq_first = first[1]
        t_recv_first = first[2]
        bundled_ms = first[3]
        cue_only = first[4]
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
            cue_only = cue_only and nxt[4]

        return {
            "packets": packets,
            "gseq_first": gseq_first,
            "gseq_last": gseq_last,
            "bundled_ms": bundled_ms,
            "t_recv_first": t_recv_first,
            "cue_only": cue_only,
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

        now = time.monotonic()
        # The client plays this bundle back in real time starting no earlier
        # than now. ALL output (speech + cue) advances the echo-guard horizon;
        # only real speech advances the barge-in audible horizon (cue-only
        # bundles must leave is_bot_speaking() False).
        self._output_until = max(now, self._output_until) + bundle["bundled_ms"] / 1000.0
        if not bundle.get("cue_only"):
            self._audible_until = max(now, self._audible_until) + bundle["bundled_ms"] / 1000.0

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
        self._audible_until = 0.0
        self._output_until = 0.0
