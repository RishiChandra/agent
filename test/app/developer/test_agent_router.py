"""Unit tests for agent_router.py — routing at 1000+ registered agents.

Pure Python: the router is fed synthetic registry rows through its `loader`
hook, so no database is needed.

Run from repo root:

    python -m pytest test/app/developer/test_agent_router.py
"""

from __future__ import annotations

import os
import random
import sys
import time
import unittest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(project_root, "app"))

import agent_router as ar  # noqa: E402

WORDS = (
    "weather flight hotel pizza taxi bank stock news music movie recipe doctor "
    "plumber lawyer tutor gym yoga travel visa tax"
).split()


def synthetic_rows(n: int, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        w1, w2 = rnd.choice(WORDS), rnd.choice(WORDS)
        rows.append({
            "id": f"id{i}",
            "name": f"{w1.title()} {w2.title()} {i}",
            "url": f"wss://agent{i}.example/relay",
            "description": f"Helps with {w1} and {w2} questions",
            "keywords": [w1, w2],
            "modes": ["bridge", "task"] if i % 3 == 0 else ["bridge"],
        })
    return rows


NAMED = [
    {"id": "kairos", "name": "Kairos", "url": "wss://kairos/relay",
     "description": "Personal scheduling and calendar assistant",
     "keywords": ["calendar", "schedule"], "service_id": "kairos-3a7f"},
    {"id": "wb", "name": "Weather Bot", "url": "wss://wb/relay",
     "description": "Answers questions about the weather for any city",
     "keywords": ["weather", "forecast"],
     "user_intents": ["What's the weather in Boston?"]},
    {"id": "ledger", "name": "Ledger", "url": "wss://ledger/relay",
     "description": "Files expense reports", "modes": ["task"]},
]


def make_router(n: int = 1500, **kw) -> ar.AgentRouter:
    rows = synthetic_rows(n) + NAMED
    r = ar.AgentRouter(loader=lambda: rows, **kw)
    r.load_now()
    return r


class RouterAtScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = make_router(1500)

    def test_snapshot_size(self):
        self.assertEqual(self.router.stats()["agents"], 1503)

    def test_exact_name(self):
        res = self.router.resolve("Kairos")
        self.assertEqual(res.decision, ar.DECISION_MATCHED)
        self.assertEqual(res.best.id, "kairos")

    def test_service_id(self):
        res = self.router.resolve("kairos-3a7f")
        self.assertEqual(res.decision, ar.DECISION_MATCHED)
        self.assertEqual(res.best.id, "kairos")

    def test_stt_garbles_resolve(self):
        for garbled in ("cairo's", "kai ross", "Kyros"):
            with self.subTest(garbled=garbled):
                res = self.router.resolve(garbled)
                self.assertEqual(res.decision, ar.DECISION_MATCHED, res.candidates[:3])
                self.assertEqual(res.best.id, "kairos")

    def test_multiword_exact_beats_token_overlap(self):
        res = self.router.resolve("weather bot")
        self.assertEqual(res.decision, ar.DECISION_MATCHED)
        self.assertEqual(res.best.id, "wb")

    def test_bare_common_word_is_ambiguous_not_a_guess(self):
        res = self.router.resolve("weather")
        self.assertEqual(res.decision, ar.DECISION_AMBIGUOUS)
        self.assertGreaterEqual(len(res.candidates), 2)

    def test_gibberish_is_none(self):
        res = self.router.resolve("xyzzy plugh")
        self.assertEqual(res.decision, ar.DECISION_NONE)
        self.assertIsNone(self.router.resolve_url("xyzzy plugh"))

    def test_intent_only_routing(self):
        res = self.router.resolve("", intent="what's the weather in boston")
        self.assertIn(res.decision, (ar.DECISION_MATCHED, ar.DECISION_AMBIGUOUS))
        self.assertEqual(res.best.id, "wb")

    def test_mode_filter_and_wrong_mode(self):
        res = self.router.resolve("ledger", mode=ar.MODE_BRIDGE)
        self.assertEqual(res.decision, ar.DECISION_WRONG_MODE)
        self.assertEqual(res.wrong_mode.id, "ledger")
        res = self.router.resolve("ledger", mode=ar.MODE_TASK)
        self.assertEqual(res.decision, ar.DECISION_MATCHED)
        for c in self.router.resolve("", intent="weather", mode=ar.MODE_TASK, k=20).candidates:
            self.assertTrue(c.agent.supports(ar.MODE_TASK))

    def test_latency_budget(self):
        queries = ["Kairos", "cairo's", "weather bot", "pizza taxi 12", "xyzzy", "tax lawyer"]
        t0 = time.perf_counter()
        n = 0
        for _ in range(10):
            for q in queries:
                self.router.resolve(q, intent=q)
                n += 1
        avg_ms = (time.perf_counter() - t0) * 1000 / n
        # Generous bound for slow CI; typical is a few ms.
        self.assertLess(avg_ms, 60.0, f"avg resolve {avg_ms:.1f}ms")

    def test_prompt_summary_is_bounded(self):
        s = self.router.prompt_summary(limit=30)
        self.assertLess(len(s), 1000)
        self.assertIn("1503", s)
        self.assertNotIn("Weather Bot", s)

    def test_search_across_modes(self):
        names = [c.agent.name for c in self.router.search("expense reports")]
        self.assertIn("Ledger", names)


class RouterBehaviourTests(unittest.TestCase):
    def test_small_registry_prompt_lists_agents(self):
        r = ar.AgentRouter(loader=lambda: list(NAMED))
        s = r.prompt_summary()
        self.assertIn("Kairos", s)
        self.assertIn("Ledger [task]", s)

    def test_empty_registry(self):
        r = ar.AgentRouter(loader=lambda: [])
        self.assertEqual(r.resolve("anything").decision, ar.DECISION_NONE)
        self.assertEqual(r.prompt_summary(), "")

    def test_rows_without_name_or_url_are_skipped(self):
        r = ar.AgentRouter(loader=lambda: [{"id": "x", "name": "", "url": "wss://x"},
                                           {"id": "y", "name": "Y", "url": ""}])
        r.load_now()
        self.assertEqual(r.stats()["agents"], 0)

    def test_loader_failure_keeps_last_good_snapshot(self):
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("db down")
            return list(NAMED)

        r = ar.AgentRouter(loader=loader)
        r.load_now()
        r.invalidate()
        res = r.resolve("Kairos")  # triggers a failing reload
        self.assertEqual(res.best.id, "kairos")
        self.assertEqual(r.stats()["last_refresh_error"], "db down")

    def test_failed_first_load_backs_off(self):
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            raise RuntimeError("db down")

        r = ar.AgentRouter(loader=loader, refresh_s=30)
        for _ in range(5):
            self.assertEqual(r.resolve("x").decision, ar.DECISION_NONE)
        self.assertEqual(calls["n"], 1)  # not retried on every lookup

    def test_prompt_summary_does_not_block_on_reload(self):
        import threading

        gate = threading.Event()
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            if calls["n"] > 1:
                gate.wait(2)  # a slow reload
            return list(NAMED)

        r = ar.AgentRouter(loader=loader, refresh_s=0.0)
        r.load_now()
        time.sleep(0.01)
        t0 = time.perf_counter()
        self.assertIn("Kairos", r.prompt_summary())
        self.assertLess(time.perf_counter() - t0, 0.5)
        gate.set()

    def test_invalidate_picks_up_new_agents(self):
        rows = list(NAMED)
        r = ar.AgentRouter(loader=lambda: rows, refresh_s=3600)
        r.load_now()
        self.assertEqual(r.resolve("Nimbus").decision, ar.DECISION_NONE)
        rows.append({"id": "n", "name": "Nimbus", "url": "wss://n"})
        self.assertEqual(r.resolve("Nimbus").decision, ar.DECISION_NONE)  # cached
        r.invalidate()
        self.assertEqual(r.resolve("Nimbus").best.id, "n")

    def test_ttl_refresh(self):
        rows = list(NAMED)
        r = ar.AgentRouter(loader=lambda: rows, refresh_s=0.0)
        r.load_now()
        rows.append({"id": "n", "name": "Nimbus", "url": "wss://n"})
        time.sleep(0.01)
        self.assertEqual(r.resolve("Nimbus").best.id, "n")

    def test_duplicate_names_are_ambiguous(self):
        rows = [
            {"id": "a", "name": "Atlas", "url": "wss://a"},
            {"id": "b", "name": "Atlas", "url": "wss://b"},
        ]
        r = ar.AgentRouter(loader=lambda: rows)
        self.assertEqual(r.resolve("atlas").decision, ar.DECISION_AMBIGUOUS)

    def test_circuit_breaker(self):
        r = ar.AgentRouter(loader=lambda: list(NAMED), failure_threshold=2, cooldown_s=60)
        r.load_now()
        r.report_failure("kairos", "connect refused")
        self.assertTrue(r.is_healthy("kairos"))
        r.report_failure("kairos", "connect refused")
        self.assertFalse(r.is_healthy("kairos"))
        self.assertIn("kairos", r.stats()["open_circuits"])
        res = r.resolve("Kairos")
        self.assertIn("unhealthy", res.candidates[0].reasons)
        r.report_success("kairos")
        self.assertTrue(r.is_healthy("kairos"))

    def test_circuit_cooldown_expires(self):
        r = ar.AgentRouter(loader=lambda: list(NAMED), failure_threshold=1, cooldown_s=0.01)
        r.load_now()
        r.report_failure("kairos")
        time.sleep(0.02)
        self.assertTrue(r.is_healthy("kairos"))

    def test_malformed_rows_do_not_break_loading(self):
        rows = list(NAMED) + [{"id": "bad", "name": "Bad", "url": "wss://b", "max_concurrency": "lots"}]
        r = ar.AgentRouter(loader=lambda: rows)
        r.load_now()
        self.assertEqual(r.get("bad").max_concurrency, 8)

    def test_phonetic_key(self):
        self.assertEqual(ar.phonetic_key("kairos"), ar.phonetic_key("cairos"))
        self.assertEqual(ar.phonetic_key("kai ross"), ar.phonetic_key("kairos"))
        self.assertEqual(ar.phonetic_key(""), "")


if __name__ == "__main__":
    unittest.main()
