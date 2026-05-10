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
        """After end_silence_s without re-arm, invoke on_fire once."""
        await self.cancel_timer()
        self._arm_id += 1
        my_id = self._arm_id

        async def _watch() -> None:
            try:
                await asyncio.sleep(self._end_silence_s)
            except asyncio.CancelledError:
                return
            # If anyone else re-armed or cancelled, stand down.
            if my_id != self._arm_id:
                return
            result = on_fire()
            if asyncio.iscoroutine(result):
                await result

        self._timer = asyncio.create_task(_watch())

    async def bump_arm_id(self) -> None:
        """Invalidate any in-flight watcher without scheduling a new one."""
        self._arm_id += 1
        await self.cancel_timer()
