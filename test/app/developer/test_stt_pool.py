"""Unit tests for the pooled Vosk recognizers in orchestrator/stt.py.

Runs without vosk installed: `RecognizerPool` takes an injected factory, and
these tests drive it with a fake recognizer that mimics the parts of
`KaldiRecognizer` the module uses (`AcceptWaveform`, `FinalResult`, `Reset`).

Run from repo root:

    python -m pytest test/app/developer/test_stt_pool.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import unittest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(project_root, "app"))

# Load stt.py directly by path: the package __init__ pulls in the whole voice
# pipeline (fastapi, pipecat, …), none of which these unit tests need.
_spec = importlib.util.spec_from_file_location(
    "developer_ws_stt_under_test",
    os.path.join(project_root, "app", "orchestrator", "stt.py"),
)
assert _spec is not None and _spec.loader is not None
stt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stt)

SR = 16000


class FakeRecognizer:
    def __init__(self, sample_rate: int, text: str = "hello") -> None:
        self.sample_rate = sample_rate
        self.text = text
        self.fed = 0
        self.final_calls = 0
        self.reset_calls = 0
        self.fail_final = False

    def AcceptWaveform(self, pcm: bytes) -> bool:
        self.fed += len(pcm)
        return False

    def FinalResult(self) -> str:
        if self.fail_final:
            raise RuntimeError("decoder blew up")
        self.final_calls += 1
        self.fed = 0
        return json.dumps({"text": self.text})

    def Reset(self) -> None:
        self.reset_calls += 1
        self.fed = 0


class FakeFactory:
    def __init__(self) -> None:
        self.made: list[FakeRecognizer] = []

    def __call__(self, sample_rate: int) -> FakeRecognizer:
        rec = FakeRecognizer(sample_rate)
        self.made.append(rec)
        return rec


def make_pool(max_idle: int = 4, max_uses: int = 500):
    factory = FakeFactory()
    return stt.RecognizerPool(factory, max_idle=max_idle, max_uses=max_uses), factory


def run(coro):
    return asyncio.run(coro)


class RecognizerPoolTests(unittest.TestCase):
    def test_new_recognizer_is_cold_then_warm_after_decoding(self):
        pool, factory = make_pool()
        rec, warm = pool.acquire(SR)
        self.assertFalse(warm)
        pool.release(SR, rec, decoded=True)
        again, warm = pool.acquire(SR)
        self.assertIs(again, rec)
        self.assertTrue(warm)
        self.assertEqual(len(factory.made), 1)

    def test_released_without_decoding_stays_cold(self):
        pool, _ = make_pool()
        rec, _ = pool.acquire(SR)
        pool.release(SR, rec, decoded=False)
        again, warm = pool.acquire(SR)
        self.assertIs(again, rec)
        self.assertFalse(warm)

    def test_concurrent_acquires_get_distinct_recognizers(self):
        pool, factory = make_pool()
        a, _ = pool.acquire(SR)
        b, _ = pool.acquire(SR)
        self.assertIsNot(a, b)
        self.assertEqual(len(factory.made), 2)

    def test_pools_are_per_sample_rate(self):
        pool, _ = make_pool()
        rec, _ = pool.acquire(SR)
        pool.release(SR, rec, decoded=True)
        other, warm = pool.acquire(8000)
        self.assertIsNot(other, rec)
        self.assertFalse(warm)

    def test_retired_after_max_uses(self):
        pool, factory = make_pool(max_uses=2)
        rec, _ = pool.acquire(SR)
        pool.release(SR, rec, decoded=True)
        rec, _ = pool.acquire(SR)
        pool.release(SR, rec, decoded=True)  # second use: retired
        self.assertEqual(pool.idle_count(SR), 0)
        fresh, warm = pool.acquire(SR)
        self.assertIsNot(fresh, rec)
        self.assertFalse(warm)
        self.assertEqual(len(factory.made), 2)

    def test_idle_capped_at_max_idle(self):
        pool, _ = make_pool(max_idle=1)
        a, _ = pool.acquire(SR)
        b, _ = pool.acquire(SR)
        pool.release(SR, a, decoded=True)
        pool.release(SR, b, decoded=True)
        self.assertEqual(pool.idle_count(SR), 1)

    def test_factory_returning_none_means_stt_unavailable(self):
        pool = stt.RecognizerPool(lambda sr: None, max_idle=4, max_uses=500)
        self.assertEqual(pool.acquire(SR), (None, False))


class StreamingTranscriberTests(unittest.TestCase):
    def test_finalize_returns_text_and_recognizer_to_pool(self):
        pool, factory = make_pool()
        t = stt.StreamingTranscriber(SR, pool=pool)
        run(t.feed(b"\x01\x00" * 4000))
        self.assertFalse(t.warm)
        self.assertEqual(run(t.finalize()), "hello")
        self.assertEqual(pool.idle_count(SR), 1)

        t2 = stt.StreamingTranscriber(SR, pool=pool)
        run(t2.feed(b"\x01\x00" * 4000))
        self.assertTrue(t2.warm)
        self.assertEqual(len(factory.made), 1)

    def test_recognizer_acquired_lazily_on_first_feed(self):
        pool, factory = make_pool()
        t = stt.StreamingTranscriber(SR, pool=pool)
        self.assertEqual(factory.made, [])
        self.assertIsNone(t.warm)
        self.assertEqual(run(t.finalize()), "")
        self.assertEqual(factory.made, [])

    def test_odd_length_chunk_trimmed(self):
        pool, factory = make_pool()
        t = stt.StreamingTranscriber(SR, pool=pool)
        run(t.feed(b"\x01\x00\x02"))
        self.assertEqual(factory.made[0].fed, 2)

    def test_abandon_resets_and_returns_recognizer(self):
        pool, factory = make_pool()
        t = stt.StreamingTranscriber(SR, pool=pool)
        run(t.feed(b"\x01\x00" * 100))
        run(t.abandon())
        rec = factory.made[0]
        self.assertEqual(rec.reset_calls, 1)
        self.assertEqual(rec.fed, 0)
        self.assertEqual(pool.idle_count(SR), 1)
        # Abandoned transcribers don't grab a new recognizer if fed again.
        run(t.feed(b"\x01\x00" * 100))
        self.assertEqual(len(factory.made), 1)
        self.assertEqual(pool.idle_count(SR), 1)

    def test_failed_finalize_drops_recognizer(self):
        pool, factory = make_pool()
        t = stt.StreamingTranscriber(SR, pool=pool)
        run(t.feed(b"\x01\x00" * 100))
        factory.made[0].fail_final = True
        with self.assertRaises(RuntimeError):
            run(t.finalize())
        self.assertEqual(pool.idle_count(SR), 0)

    def test_unavailable_model_yields_empty_transcript(self):
        pool = stt.RecognizerPool(lambda sr: None, max_idle=4, max_uses=500)
        t = stt.StreamingTranscriber(SR, pool=pool)
        run(t.feed(b"\x01\x00" * 100))
        self.assertEqual(run(t.finalize()), "")


class WarmPoolTests(unittest.TestCase):
    def test_warms_distinct_recognizers(self):
        pool, factory = make_pool()
        n = run(stt.warm_recognizer_pool(b"\x01\x00" * 20000, SR, size=2, pool=pool))
        self.assertEqual(n, 2)
        self.assertEqual(len(factory.made), 2)
        self.assertEqual(pool.idle_count(SR), 2)
        for rec in factory.made:
            self.assertEqual(rec.final_calls, 1)
        _, warm = pool.acquire(SR)
        self.assertTrue(warm)

    def test_failed_warmup_drops_only_that_recognizer(self):
        pool, factory = make_pool()
        orig = factory.__call__

        def flaky(sr):
            rec = orig(sr)
            if len(factory.made) == 1:
                rec.fail_final = True
            return rec

        pool._factory = flaky
        n = run(stt.warm_recognizer_pool(b"\x01\x00" * 20000, SR, size=2, pool=pool))
        self.assertEqual(n, 1)
        self.assertEqual(pool.idle_count(SR), 1)

    def test_no_audio_or_zero_size_is_a_no_op(self):
        pool, factory = make_pool()
        self.assertEqual(run(stt.warm_recognizer_pool(b"", SR, size=2, pool=pool)), 0)
        self.assertEqual(run(stt.warm_recognizer_pool(b"\x01\x00", SR, size=0, pool=pool)), 0)
        self.assertEqual(factory.made, [])


if __name__ == "__main__":
    unittest.main()
