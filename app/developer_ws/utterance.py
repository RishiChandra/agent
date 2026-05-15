"""Speech buffer + silence timer for one developer-WS connection.

Owns the bytes accumulated between flushes, the lock that guards them, and the
"end-of-utterance" timer that fires a flush after a quiet gap.
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable

from audio_codec import rms_int16_le

FlushCallback = Callable[[], Awaitable[None] | None]


class UtteranceBuffer:
    """One per voice session. Holds raw PCM bytes between flushes.

    Constructed by: `developer_websocket_endpoint` in endpoint.py.
    Mutated by:
      - `extend(pcm)` — called from `_handle_audio` for each non-bridge frame.
      - `snapshot_and_clear()` — called from `pipeline.flush()` at the start of a turn.
      - `arm_timer(on_fire)` — called from `_handle_audio` when a batch has speech;
        `on_fire` is bound to `pipeline.flush` so the timer triggers a turn.
      - `bump_arm_id()` — called from `_drain_on_close`, `_handle_interrupt`, and
        when `turn_complete:true` arrives, to invalidate any in-flight watcher.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        # Short critical section: extend / snapshot+clear only — never held across STT/LLM/TTS.
        self._lock = asyncio.Lock()
        # Each new arm bumps the id; a stale watcher checks the id and returns without flushing.
        self._arm_id = 0
        self._timer: asyncio.Task | None = None
        self._end_silence_s = float(os.environ.get("DEVELOPER_WS_END_SILENCE_SEC", "2.0"))
        self._vad_rms = float(os.environ.get("DEVELOPER_WS_VAD_RMS", "20"))

    async def extend(self, pcm: bytes) -> None:
        async with self._lock:
            self._buf.extend(pcm)

    async def snapshot_and_clear(self) -> bytes:
        async with self._lock:
            data = bytes(self._buf)
            self._buf.clear()
            return data

    async def has_data(self) -> bool:
        async with self._lock:
            return len(self._buf) > 0

    def has_speech(self, pcm: bytes) -> bool:
        """True if the batch's RMS clears the VAD threshold (skip silent batches)."""
        return rms_int16_le(pcm) >= self._vad_rms

    async def cancel_timer(self) -> None:
        t = self._timer
        self._timer = None
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def arm_timer(self, on_fire: FlushCallback) -> None:
        """Schedule `on_fire` to run after `_end_silence_s` of no re-arm.

        Called by: `_handle_audio` in endpoint.py, once per batch that clears the
        VAD threshold. Each call cancels any prior watcher (so continuous speech
        keeps deferring the fire) and bumps `_arm_id` so a stale watcher about to
        execute can detect that it lost the race and return without firing.
        `on_fire` is `pipeline.flush` in normal use.
        """
        import logging
        _dlog = logging.getLogger("developer_ws")
        await self.cancel_timer()
        self._arm_id += 1
        my_id = self._arm_id
        _dlog.info("timer ARMED id=%d sleep=%.2fs", my_id, self._end_silence_s)

        async def _watch() -> None:
            try:
                await asyncio.sleep(self._end_silence_s)
            except asyncio.CancelledError:
                _dlog.info("timer CANCELLED id=%d", my_id)
                return
            # If anyone else re-armed or cancelled, stand down.
            if my_id != self._arm_id:
                _dlog.info("timer STALE id=%d current=%d", my_id, self._arm_id)
                return
            _dlog.info("timer FIRING id=%d -> on_fire()", my_id)
            try:
                result = on_fire()
                if asyncio.iscoroutine(result):
                    await result
                _dlog.info("timer DONE id=%d", my_id)
            except Exception:
                _dlog.exception("timer on_fire raised id=%d", my_id)

        self._timer = asyncio.create_task(_watch())

    async def bump_arm_id(self) -> None:
        """Invalidate any in-flight watcher without scheduling a new one.

        Called by: `_drain_on_close` (shutdown), `_handle_interrupt` (user said "stop"),
        and `_handle_audio` when `turn_complete:true` arrives (we're about to flush
        immediately, so the silence timer would be redundant).
        """
        self._arm_id += 1
        await self.cancel_timer()
