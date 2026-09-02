"""Unit tests for developer_ws/vad.py (Silero VAD endpointing wrapper).

Runs without pipecat/onnxruntime installed: `SileroVAD` takes an injected
analyzer, and these tests drive it with a scripted fake that replays the
VADState sequence the real `SileroVADAnalyzer` would emit. State names
(QUIET/STARTING/SPEAKING/STOPPING) mirror pipecat's `VADState` enum, which
the wrapper reads via `.name`.

Run from repo root:

    python -m pytest test/app/developer/test_vad.py
"""

from __future__ import annotations

import asyncio
import enum
import importlib.util
import os
import sys
import unittest
from unittest.mock import patch

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(project_root, "app"))

# Load vad.py directly by path rather than `from developer_ws.vad import ...`:
# the package __init__ pulls in the whole voice pipeline (fastapi, pipecat, …),
# none of which these unit tests need — vad.py itself only needs audio_codec.
_spec = importlib.util.spec_from_file_location(
    "developer_ws_vad_under_test",
    os.path.join(project_root, "app", "developer_ws", "vad.py"),
)
assert _spec is not None and _spec.loader is not None
vad_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vad_mod)

SileroVAD = vad_mod.SileroVAD
VADEvent = vad_mod.VADEvent
create_silero_vad = vad_mod.create_silero_vad


class FakeState(enum.Enum):
    """Stands in for pipecat's VADState — the wrapper only looks at `.name`."""

    QUIET = 1
    STARTING = 2
    SPEAKING = 3
    STOPPING = 4


class FakeAnalyzer:
    """Scripted analyzer: returns the next state per analyze_audio call."""

    def __init__(self, states: list[FakeState]):
        self.states = list(states)
        self.params = object()
        self.set_params_calls = 0
        self.fed: list[bytes] = []

    async def analyze_audio(self, buffer: bytes) -> FakeState:
        self.fed.append(buffer)
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    def set_params(self, params) -> None:
        self.set_params_calls += 1


def run(coro):
    return asyncio.run(coro)


PCM = b"\x00\x01" * 256  # arbitrary non-empty batch


class TestSileroVADEvents(unittest.TestCase):
    def test_full_utterance_emits_started_then_stopped(self):
        vad = SileroVAD(analyzer=FakeAnalyzer([
            FakeState.QUIET,      # leading silence
            FakeState.STARTING,   # hysteresis warming up
            FakeState.SPEAKING,   # confirmed → STARTED
            FakeState.SPEAKING,
            FakeState.STOPPING,   # trailing hysteresis
            FakeState.QUIET,      # confirmed stop → STOPPED
        ]))

        async def drive():
            return [await vad.process(PCM) for _ in range(6)]

        events = run(drive())
        self.assertEqual(events, [
            VADEvent.NONE, VADEvent.NONE, VADEvent.STARTED,
            VADEvent.NONE, VADEvent.NONE, VADEvent.STOPPED,
        ])
        self.assertFalse(vad.speaking)

    def test_speaking_property_tracks_confirmed_speech(self):
        vad = SileroVAD(analyzer=FakeAnalyzer([FakeState.SPEAKING, FakeState.QUIET]))

        async def drive():
            self.assertFalse(vad.speaking)
            await vad.process(PCM)
            self.assertTrue(vad.speaking)
            await vad.process(PCM)
            self.assertFalse(vad.speaking)

        run(drive())

    def test_starting_that_never_confirms_emits_nothing(self):
        # Noise blip: QUIET → STARTING → QUIET must not produce STOPPED.
        vad = SileroVAD(analyzer=FakeAnalyzer([
            FakeState.QUIET, FakeState.STARTING, FakeState.QUIET,
        ]))

        async def drive():
            return [await vad.process(PCM) for _ in range(3)]

        self.assertEqual(run(drive()), [VADEvent.NONE] * 3)

    def test_empty_batch_is_ignored(self):
        analyzer = FakeAnalyzer([FakeState.SPEAKING])
        vad = SileroVAD(analyzer=analyzer)
        self.assertIs(run(vad.process(b"")), VADEvent.NONE)
        self.assertEqual(analyzer.fed, [])

    def test_stopped_fires_again_for_next_utterance(self):
        vad = SileroVAD(analyzer=FakeAnalyzer([
            FakeState.SPEAKING, FakeState.QUIET,   # utterance 1
            FakeState.SPEAKING, FakeState.QUIET,   # utterance 2
        ]))

        async def drive():
            return [await vad.process(PCM) for _ in range(4)]

        self.assertEqual(run(drive()), [
            VADEvent.STARTED, VADEvent.STOPPED,
            VADEvent.STARTED, VADEvent.STOPPED,
        ])


class TestSileroVADReset(unittest.TestCase):
    def test_reset_clears_speaking_and_rearms_analyzer(self):
        analyzer = FakeAnalyzer([FakeState.SPEAKING])
        vad = SileroVAD(analyzer=analyzer)
        run(vad.process(PCM))
        self.assertTrue(vad.speaking)

        vad.reset()
        self.assertFalse(vad.speaking)
        self.assertEqual(analyzer.set_params_calls, 1)

    def test_reset_noop_when_nothing_processed(self):
        # Bridge mode calls reset per batch; it must not touch the analyzer
        # (nor log-spam set_params) when there's nothing to clear.
        analyzer = FakeAnalyzer([FakeState.QUIET])
        vad = SileroVAD(analyzer=analyzer)
        vad.reset()
        vad.reset()
        self.assertEqual(analyzer.set_params_calls, 0)

        run(vad.process(PCM))
        vad.reset()
        vad.reset()  # second call: nothing new processed → no-op
        self.assertEqual(analyzer.set_params_calls, 1)

    def test_no_stopped_event_for_speech_cancelled_by_reset(self):
        # Interrupt mid-speech: after reset, a later QUIET must not emit STOPPED.
        vad = SileroVAD(analyzer=FakeAnalyzer([FakeState.SPEAKING, FakeState.QUIET]))
        run(vad.process(PCM))
        vad.reset()
        self.assertIs(run(vad.process(PCM)), VADEvent.NONE)


class TestCreateSileroVAD(unittest.TestCase):
    def test_disabled_via_env_returns_none(self):
        with patch.dict(os.environ, {"DEVELOPER_WS_USE_SILERO_VAD": "0"}):
            self.assertIsNone(create_silero_vad(user_id="u1"))

    def test_missing_deps_returns_none_not_raise(self):
        # Force the lazy dep loader down its failure path regardless of
        # whether pipecat/onnxruntime are installed in this venv.
        with patch.dict(os.environ, {"DEVELOPER_WS_USE_SILERO_VAD": "1"}):
            with patch.object(vad_mod, "_load_analyzer_deps", return_value=None):
                self.assertIsNone(create_silero_vad(user_id="u1"))


if __name__ == "__main__":
    unittest.main()
