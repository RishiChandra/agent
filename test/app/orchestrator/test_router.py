"""Router decisions (ORCHESTRATOR_V2_TOOL_CALLS.md §1.2 rows 2–9, §1.3)."""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../app")))

from orchestrator.routing.router import (  # noqa: E402
    DECISION_AMBIGUOUS, DECISION_MATCHED, DECISION_NONE, DECISION_WRONG_MODE,
    AgentRouter, phonetic_key,
)


def agent(i, name, **kw):
    row = {"id": f"a{i}", "name": name, "url": f"ws://x/{i}", "description": kw.pop("description", ""),
           "modes": kw.pop("modes", ["bridge"])}
    row.update(kw)
    return row


TASK = {"modes": ["task", "bridge"], "events": ["callback"]}

AGENTS = [
    agent(1, "Kairos", description="Voice task and reminder scheduler",
          domains=["tasks", "reminders"], intent_aliases=["my list", "remind me"], routing_policy="owns_domain",
          user_data=True),
    agent(2, "MyFitnessPal", description="Nutrition and food logging", domains=["nutrition", "calories"],
          intent_aliases=["calories", "what did i eat"], user_data=True, **TASK),
    agent(3, "Tabletop", description="Restaurant reservations, book a table", **TASK),
    agent(4, "Atlas", description="Maps"),
    agent(5, "Atlas Travel", description="Flights and hotels"),
    agent(6, "Ledger", description="Accounting", modes=["task"], events=["callback"]),
    agent(7, "Weather Bot", description="Weather forecasts"),
    agent(8, "Weather Alerts", description="Severe weather warnings"),
    agent(9, "Weather History", description="Past weather"),
]


def make_router(rows=AGENTS, **kw):
    return AgentRouter(loader=lambda: list(rows), **kw)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.r = make_router()

    def test_exact_name(self):
        res = self.r.resolve("Kairos")
        self.assertEqual((res.decision, res.best.name), (DECISION_MATCHED, "Kairos"))

    def test_stt_garbles_match_phonetically(self):
        self.assertEqual(phonetic_key("kairos"), phonetic_key("cairo's"))
        for said in ("cairo's", "kai ross"):
            res = self.r.resolve(said)
            self.assertEqual((res.decision, res.best.name), (DECISION_MATCHED, "Kairos"), said)

    def test_ambiguous_prefix_asks(self):
        res = self.r.resolve("weather")
        self.assertEqual(res.decision, DECISION_AMBIGUOUS)
        self.assertGreaterEqual(len(res.names()), 2)

    def test_exact_name_beats_prefix_family(self):
        res = self.r.resolve("Weather Bot")
        self.assertEqual((res.decision, res.best.name), (DECISION_MATCHED, "Weather Bot"))

    def test_unknown_name_is_none_not_a_default(self):
        self.assertEqual(self.r.resolve("Zorblax").decision, DECISION_NONE)

    def test_duplicate_names_are_ambiguous(self):
        r = make_router(AGENTS + [agent(10, "Atlas", description="Another Atlas")])
        self.assertEqual(r.resolve("Atlas").decision, DECISION_AMBIGUOUS)

    def test_wrong_mode(self):
        res = self.r.resolve("Ledger", mode="bridge")
        self.assertEqual(res.decision, DECISION_WRONG_MODE)
        self.assertEqual(res.wrong_mode.name, "Ledger")

    def test_task_mode_requires_callback_events(self):
        r = make_router([agent(1, "Pollster", modes=["task"], events=["ws"])])
        self.assertEqual(r.resolve("Pollster", mode="task").decision, DECISION_WRONG_MODE)

    def test_intent_alias_routes_unnamed_request(self):
        res = self.r.resolve("", "how many calories have I had today")
        self.assertEqual(res.best.name, "MyFitnessPal")
        self.assertEqual(res.decision, DECISION_MATCHED)

    def test_owned_domain_alias(self):
        res = self.r.resolve("", "what's on my list today")
        self.assertEqual(res.best.name, "Kairos")

    def test_lexical_intent(self):
        res = self.r.resolve("", "book a table for two")
        self.assertEqual(res.best.name, "Tabletop")

    def test_embeddings_route_requests_without_shared_words(self):
        rows = [dict(a) for a in AGENTS]
        for a in rows:
            a["routing_embedding"] = [1.0, 0.0, 0.0] if a["name"] == "MyFitnessPal" else [0.0, 1.0, 0.0]
        r = make_router(rows)
        res = r.resolve("", "I'm starving, how much have I eaten", query_embedding=[0.95, 0.05, 0.0])
        self.assertEqual(res.best.name, "MyFitnessPal")
        self.assertTrue(any(x.startswith("embedding") for x in res.candidates[0].reasons))
        # Named requests never use the embedding.
        named = r.resolve("Tabletop", "food", query_embedding=[1.0, 0.0, 0.0])
        self.assertEqual(named.best.name, "Tabletop")

    def test_intent_never_outranks_exact_name(self):
        res = self.r.resolve("Tabletop", "calories")
        self.assertEqual(res.best.name, "Tabletop")

    def test_unhealthy_agent_is_ranked_lower_but_still_resolvable_by_name(self):
        r = make_router(failure_threshold=1)
        r.report_failure("a3", "down")
        self.assertFalse(r.is_healthy("a3"))
        res = r.resolve("Tabletop")
        self.assertEqual((res.decision, res.best.name), (DECISION_MATCHED, "Tabletop"))
        self.assertIn("unhealthy", r.resolve("", "book a table for two").candidates[0].reasons)
        r.report_success("a3")
        self.assertTrue(r.is_healthy("a3"))

    def test_failed_refresh_serves_stale_snapshot(self):
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("db down")
            return list(AGENTS)

        r = AgentRouter(loader=loader, refresh_s=0)
        self.assertEqual(r.resolve("Kairos").best.name, "Kairos")
        r.invalidate()
        self.assertEqual(r.resolve("Kairos").best.name, "Kairos")
        self.assertIn("db down", r.stats()["last_refresh_error"])

    def test_bounded_prompt(self):
        small = self.r.prompt_summary(limit=30)
        self.assertIn("Kairos", small)
        big = make_router([agent(i, f"Agent {i}") for i in range(100)]).prompt_summary(limit=30)
        self.assertIn("too many to list", big)
        self.assertLess(len(big), 400)

    def test_side_effects_default_true(self):
        self.assertTrue(self.r.get("a3").side_effects)
        r = make_router([agent(1, "Reader", side_effects=False)])
        self.assertFalse(r.get("a1").side_effects)

    def test_latency_budget_at_1500_agents(self):
        rows = [agent(i, f"Service {i} {w}", description=f"does {w} things")
                for i, w in enumerate(["alpha", "beta", "gamma", "delta", "omega"] * 300)]
        r = make_router(rows + AGENTS)
        r.load_now()
        t = time.perf_counter()
        for q in ("cairo's", "weather", "zorblax", "Service 42 gamma"):
            r.resolve(q)
        per = (time.perf_counter() - t) / 4
        self.assertLess(per, 0.25, f"{per*1000:.0f} ms per lookup")


if __name__ == "__main__":
    unittest.main()
