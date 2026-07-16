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
        # Clients batch uplink audio; if the configured silence window is
        # shorter than the batch cadence, the timer fires between batches and
        # chops every utterance into per-batch fragments. Track the observed
        # inter-arm gap (EMA) and never sleep less than ~1.3x that cadence.
        self._last_arm_t: float | None = None
        self._gap_ema_s = 0.0
        self._max_adaptive_s = float(os.environ.get("DEVELOPER_WS_MAX_SILENCE_SEC", "3.0"))
        self._vad_rms = float(os.environ.get("DEVELOPER_WS_VAD_RMS", "20"))
        # Barge-in: interrupting the bot needs a deliberately higher bar than
        # the normal VAD gate — loud speech (well above residual speaker echo)
        # sustained across consecutive batches — so playback bleed doesn't
        # self-interrupt the bot.
        self._barge_rms = float(os.environ.get("DEVELOPER_WS_BARGE_RMS", "500"))
        self._barge_batches = int(os.environ.get("DEVELOPER_WS_BARGE_BATCHES", "2"))
        self._barge_streak = 0

    def has_speech(self, pcm: bytes) -> bool:
        """True if the batch's RMS clears the VAD threshold (skip silent batches)."""
        return rms_int16_le(pcm) >= self._vad_rms

    def barge_in_hit(self, pcm: bytes) -> bool:
        """Track consecutive loud batches while the bot is speaking.

        Called by `_handle_audio` in endpoint.py for each uplink batch that
        arrives while `audio.is_bot_audible()`. Returns True (and resets) once
        `_barge_batches` consecutive batches clear the barge-in RMS threshold —
        the caller then interrupts the bot mid-speech.
        """
        if rms_int16_le(pcm) >= self._barge_rms:
            self._barge_streak += 1
        else:
            self._barge_streak = 0
        if self._barge_streak >= self._barge_batches:
            self._barge_streak = 0
            return True
        return False

    def reset_barge_in(self) -> None:
        """Clear the barge-in streak (bot stopped talking, or bridge active)."""
        self._barge_streak = 0

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

        # Update the inter-batch cadence estimate (gaps >5s are pauses between
        # utterances, not batch cadence — ignore them).
        now = asyncio.get_running_loop().time()
        if self._last_arm_t is not None:
            gap = now - self._last_arm_t
            if 0.0 < gap <= 5.0:
                self._gap_ema_s = gap if not self._gap_ema_s else (0.7 * self._gap_ema_s + 0.3 * gap)
        self._last_arm_t = now

        sleep_s = max(
            self._end_silence_s,
            min(1.3 * self._gap_ema_s, self._max_adaptive_s),
        )
        log.info("timer ARMED id=%d sleep=%.2fs (gap_ema=%.2fs)", my_id, sleep_s, self._gap_ema_s)

        async def _watch() -> None:
            try:
                await asyncio.sleep(sleep_s)
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
