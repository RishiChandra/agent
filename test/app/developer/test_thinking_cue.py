"""Unit tests for the "thinking" pulse.

Two layers:
  1. `ThinkingPulse` (developer_ws/thinking_cue.py) — the timing/state core. It
     is deliberately dependency-free (stdlib only), so it is loaded directly by
     path (the package `__init__` pulls in the whole voice stack). Driven with a
     fake audio sink that records the calls the real `AudioIO` would receive.
  2. `ThinkingCueProcessor._dispatch` — the frame -> pulse mapping. It needs the
     real pipecat frame classes, so that class is skipped unless pipecat is
     importable (it is, in the app venv on a dev machine).

Run from repo root:

    python -m pytest test/app/developer/test_thinking_cue.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))

_spec = importlib.util.spec_from_file_location(
    "developer_ws_thinking_cue_under_test",
    os.path.join(project_root, "app", "developer_ws", "thinking_cue.py"),
)
assert _spec is not None and _spec.loader is not None
tc_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc_mod)

ThinkingPulse = tc_mod.ThinkingPulse

TICK = b"\x11\x22" * 480  # arbitrary non-empty tick payload


class FakeAudio:
    """Records the AudioIO surface ThinkingPulse touches.

    mark_turn_complete is included so the tests can assert the cue NEVER touches
    turn state (it plays via add_cue_pcm, which does not open a bot turn).
    """

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.ticks = 0
        self.turn_completes = 0

    def add_cue_pcm(self, pcm: bytes) -> None:
        self.ticks += 1

    def mark_turn_complete(self) -> None:
        self.turn_completes += 1

    def is_alive(self) -> bool:
        return self.alive


def _fast_pulse(audio, **over):
    """A pulse with tiny timings so tests run in milliseconds."""
    kw = dict(tick_pcm=TICK, delay_s=0.02, interval_s=0.02, max_s=10.0, enabled=True)
    kw.update(over)
    return ThinkingPulse(audio, **kw)


class ThinkingPulseTests(unittest.TestCase):
    def test_fast_reply_before_delay_makes_no_sound(self):
        """stop() before the first tick fires -> zero ticks."""
        async def drive():
            audio = FakeAudio()
            pulse = _fast_pulse(audio, delay_s=0.20)
            pulse.start()
            await asyncio.sleep(0.02)          # well under the 0.20 s delay
            pulse.stop()                       # BotStarted arrived first
            await asyncio.sleep(0.05)
            return audio
        audio = asyncio.run(drive())
        self.assertEqual(audio.ticks, 0)

    def test_pulses_repeat_until_stopped(self):
        """A slow turn keeps ticking on the interval; a stop frame ends it."""
        async def drive():
            audio = FakeAudio()
            pulse = _fast_pulse(audio, delay_s=0.02, interval_s=0.02)
            pulse.start()
            await asyncio.sleep(0.11)          # ~ delay + ~4 intervals
            pulse.stop()
            settled = audio.ticks
            await asyncio.sleep(0.06)
            return audio, settled
        audio, settled = asyncio.run(drive())
        self.assertGreaterEqual(settled, 2)              # multiple pulses played
        self.assertEqual(audio.ticks, settled)           # none after stop()

    def test_cue_never_marks_turn_complete(self):
        """The pulse must never touch bot-turn state (plays via add_cue_pcm)."""
        async def drive():
            audio = FakeAudio()
            pulse = _fast_pulse(audio, delay_s=0.01, interval_s=0.02, max_s=0.08)
            pulse.start()
            await asyncio.sleep(0.2)           # run several ticks + hit the cap
            pulse.stop()
            return audio
        audio = asyncio.run(drive())
        self.assertGreater(audio.ticks, 0)
        self.assertEqual(audio.turn_completes, 0)

    def test_stop_when_not_running_is_noop(self):
        async def drive():
            audio = FakeAudio()
            pulse = _fast_pulse(audio)
            pulse.start()
            await asyncio.sleep(0.06)
            pulse.stop()
            pulse.stop()                       # second stop must be harmless
            await asyncio.sleep(0.03)
            return pulse
        pulse = asyncio.run(drive())
        self.assertFalse(pulse.running)

    def test_max_cap_stops(self):
        """Reaching max_s stops pulsing on its own even with no stop frame."""
        async def drive():
            audio = FakeAudio()
            pulse = _fast_pulse(audio, delay_s=0.01, interval_s=0.02, max_s=0.08)
            pulse.start()
            await asyncio.sleep(0.2)           # well past max_s
            return audio, pulse
        audio, pulse = asyncio.run(drive())
        self.assertGreater(audio.ticks, 0)
        self.assertFalse(pulse.running)                  # loop ended on its own

    def test_dead_socket_stops_without_ticks(self):
        async def drive():
            audio = FakeAudio(alive=False)
            pulse = _fast_pulse(audio, delay_s=0.01)
            pulse.start()
            await asyncio.sleep(0.06)
            return audio
        audio = asyncio.run(drive())
        self.assertEqual(audio.ticks, 0)

    def test_disabled_never_starts(self):
        async def drive():
            audio = FakeAudio()
            pulse = _fast_pulse(audio, enabled=False)
            pulse.start()
            await asyncio.sleep(0.06)
            return audio, pulse
        audio, pulse = asyncio.run(drive())
        self.assertFalse(pulse.running)
        self.assertEqual(audio.ticks, 0)

    def test_empty_tick_disables(self):
        pulse = ThinkingPulse(
            FakeAudio(), tick_pcm=b"", delay_s=0.01, interval_s=0.02, max_s=1.0
        )
        self.assertFalse(pulse.enabled)

    def test_start_is_idempotent_while_running(self):
        """A second start() while pulsing doesn't spawn a parallel loop."""
        async def drive():
            audio = FakeAudio()
            pulse = _fast_pulse(audio, delay_s=0.02, interval_s=0.05)
            pulse.start()
            pulse.start()                      # second start — should be ignored
            await asyncio.sleep(0.08)          # delay + ~1 interval
            pulse.stop()
            return audio
        audio = asyncio.run(drive())
        # Two loops would roughly double the ticks; keep it loose but bounded.
        self.assertLessEqual(audio.ticks, 2)


# ---------------------------------------------------------------------------
# ThinkingCueProcessor._dispatch — the frame -> pulse mapping. Needs the real
# pipecat frame classes, so skip when pipecat isn't installed.
# ---------------------------------------------------------------------------
try:
    if project_root + "/app" not in sys.path:
        sys.path.insert(0, os.path.join(project_root, "app"))
    from developer_ws.pipecat_bits import ThinkingCueProcessor  # noqa: E402
    from pipecat.frames.frames import (  # noqa: E402
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        CancelFrame,
        EndFrame,
        InterruptionFrame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        TranscriptionFrame,
        UserStartedSpeakingFrame,
    )
    _PIPECAT_OK = True
except Exception:  # pragma: no cover - env without pipecat/fastapi
    _PIPECAT_OK = False


class RecordingPulse:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


@unittest.skipUnless(_PIPECAT_OK, "pipecat not importable in this environment")
class ThinkingCueProcessorDispatchTests(unittest.TestCase):
    def _act(self, frame):
        pulse = RecordingPulse()
        ThinkingCueProcessor._dispatch(frame, pulse)
        return pulse.calls

    def test_llm_start_starts_pulse(self):
        self.assertEqual(self._act(LLMFullResponseStartFrame()), ["start"])

    def test_stop_frames_stop_pulse(self):
        for frame in (
            BotStartedSpeakingFrame(),
            UserStartedSpeakingFrame(),
            LLMFullResponseEndFrame(),
            InterruptionFrame(),
            CancelFrame(),
            EndFrame(),
        ):
            with self.subTest(frame=type(frame).__name__):
                self.assertEqual(self._act(frame), ["stop"])

    def test_unrelated_frames_are_ignored(self):
        # A frame the cue does not care about must neither start nor stop it.
        self.assertEqual(
            self._act(TranscriptionFrame(text="hi", user_id="u", timestamp="0")), []
        )
        self.assertEqual(self._act(BotStoppedSpeakingFrame()), [])


if __name__ == "__main__":
    unittest.main()
