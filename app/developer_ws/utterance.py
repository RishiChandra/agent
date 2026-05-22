"""End-of-utterance silence timer for one developer-WS connection.

Audio accumulation now lives inside the Pipecat pipeline (in
`VoskUtteranceSTTProcessor`). What's left here is the silence-timer state
machine that drives "user stopped speaking" — `arm_timer` reschedules each
energetic batch, `bump_arm_id` invalidates any in-flight watcher, and on
quiet-gap expiry the timer fires the callback (`pipeline.signal_user_stopped`
in normal use).

The class name is preserved to avoid churn across the endpoint; conceptually
this is now a `SilenceTimer`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable

from audio_codec import rms_int16_le

FireCallback = Callable[[], Awaitable[None] | None]

log = logging.getLogger("developer_ws")


class UtteranceBuffer:
    """One per voice session. End-of-utterance silence timer + RMS gate.

    Constructed by: `developer_websocket_endpoint` in endpoint.py.
    Used by:
      - `arm_timer(on_fire)` — called from `_handle_audio` for each batch that
        clears the VAD threshold; `on_fire` is `pipeline.signal_user_stopped`.
      - `bump_arm_id()` — called from `_drain_on_close`, `_handle_interrupt`,
        and when `turn_complete:true` arrives, to invalidate any in-flight watcher.
      - `has_speech(pcm)` — RMS gate the endpoint uses to decide whether to
        (re-)arm the silence timer.
    """

    def __init__(self) -> None:
        # Each new arm bumps the id; a stale watcher checks the id and returns
        # without firing if it lost the race against a newer arm.
        self._arm_id = 0
        self._timer: asyncio.Task | None = None
        self._end_silence_s = float(os.environ.get("DEVELOPER_WS_END_SILENCE_SEC", "2.0"))
        self._vad_rms = float(os.environ.get("DEVELOPER_WS_VAD_RMS", "20"))

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

    async def arm_timer(self, on_fire: FireCallback) -> None:
        """Schedule `on_fire` to run after `_end_silence_s` of no re-arm.

        Called by: `_handle_audio` in endpoint.py, once per batch that clears the
        VAD threshold. Each call cancels any prior watcher (so continuous speech
        keeps deferring the fire) and bumps `_arm_id` so a stale watcher about to
        execute can detect that it lost the race and return without firing.
        `on_fire` is `pipeline.signal_user_stopped` in normal use.
        """
        await self.cancel_timer()
        self._arm_id += 1
        my_id = self._arm_id
        log.info("timer ARMED id=%d sleep=%.2fs", my_id, self._end_silence_s)

        async def _watch() -> None:
            try:
                await asyncio.sleep(self._end_silence_s)
            except asyncio.CancelledError:
                log.info("timer CANCELLED id=%d", my_id)
                return
            # If anyone else re-armed or cancelled, stand down.
            if my_id != self._arm_id:
                log.info("timer STALE id=%d current=%d", my_id, self._arm_id)
                return
            log.info("timer FIRING id=%d -> on_fire()", my_id)
            try:
                result = on_fire()
                if asyncio.iscoroutine(result):
                    await result
                log.info("timer DONE id=%d", my_id)
            except Exception:
                log.exception("timer on_fire raised id=%d", my_id)

        self._timer = asyncio.create_task(_watch())

    async def bump_arm_id(self) -> None:
        """Invalidate any in-flight watcher without scheduling a new one.

        Called by: `_drain_on_close` (shutdown), `_handle_interrupt` (user said
        "stop"), and `_handle_audio` when `turn_complete:true` arrives (we're
        about to fire `signal_user_stopped` immediately, so the silence timer
        would be redundant).
        """
        self._arm_id += 1
        await self.cancel_timer()
