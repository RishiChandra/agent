"""Timing/state core of the "thinking" pulse, kept free of pipecat.

Isolated here (mirroring how `vad.py` keeps its logic injectable) so the pulse
cadence and start/stop semantics can be unit-tested with a fake audio sink and
no voice stack installed. `ThinkingCueProcessor` in `pipecat_bits.py` is a thin
FrameProcessor shell that maps pipeline frames onto `start()`/`stop()`.

The only surface it needs from `AudioIO` is two methods: `add_cue_pcm` (plays a
tick without engaging turn/barge-in state) and `is_alive`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

log = logging.getLogger("developer_ws")


class ThinkingPulse:
    """Repeating soft-tick loop for the "orchestrator is thinking" gap.

    Lifecycle (driven by the owning FrameProcessor):
      - `start()` — begin the loop. Idempotent while already running.
      - `stop()`  — cancel the loop.

    Ticks go out via `AudioIO.add_cue_pcm`, which plays them without marking a
    bot turn active, so there is nothing to flush on stop — `stop()` just cancels
    the task. The first tick is delayed by `delay_s`, so a reply that lands
    sooner than that never makes a sound. Ticks then repeat every `interval_s`
    until stopped or until `max_s` elapses (a safety cap so a wedged turn can't
    pulse forever).
    """

    def __init__(
        self,
        audio,
        *,
        tick_pcm: bytes,
        delay_s: float,
        interval_s: float,
        max_s: float,
        enabled: bool = True,
    ) -> None:
        self._audio = audio
        self._tick_pcm = tick_pcm
        self._delay_s = max(0.0, delay_s)
        # Floor the interval just enough to rule out a 0/negative busy-loop from a
        # misconfigured env var; the production default is ~1.5 s.
        self._interval_s = max(0.02, interval_s)
        self._max_s = max(self._interval_s, max_s)
        self._enabled = enabled and bool(tick_pcm)
        self._task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self._enabled or self.running:
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            # Silence before the first tick: replies faster than delay_s stay
            # completely silent.
            await asyncio.sleep(self._delay_s)
            elapsed = self._delay_s
            while elapsed < self._max_s:
                if not self._audio.is_alive():
                    return
                self._audio.add_cue_pcm(self._tick_pcm)
                await asyncio.sleep(self._interval_s)
                elapsed += self._interval_s
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("thinking pulse loop failed")

    def stop(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
