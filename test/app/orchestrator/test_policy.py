"""Mode policy, indirect routing, notify and wake rules (§1.3, §1.4, §1.6)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../app")))

from orchestrator.routing import policy  # noqa: E402
from orchestrator.routing.router import AgentRecord  # noqa: E402

BOTH = AgentRecord.from_registry_row({"id": "1", "name": "Tabletop", "url": "ws://x",
                                      "modes": ["bridge", "task"], "events": ["callback"]})
LIVE = AgentRecord.from_registry_row({"id": "2", "name": "Kairos", "url": "ws://x"})
TASK = AgentRecord.from_registry_row({"id": "3", "name": "Ledger", "url": "ws://x", "modes": ["task"],
                                      "events": ["callback"]})
OWNS = AgentRecord.from_registry_row({"id": "4", "name": "Kairos", "url": "ws://x", "routing_policy": "owns_domain"})
ON_REQ = AgentRecord.from_registry_row({"id": "5", "name": "MyFitnessPal", "url": "ws://x", "user_data": True})
NO_DATA = AgentRecord.from_registry_row({"id": "6", "name": "Encyclopedia", "url": "ws://x"})


class ModeTests(unittest.TestCase):
    def test_single_mode_agents(self):
        self.assertEqual(policy.decide_mode(LIVE, "auto", "anything"), (policy.CONNECT, None))
        self.assertEqual(policy.decide_mode(TASK, "auto", "anything"), (policy.DISPATCH, None))
        self.assertEqual(policy.decide_mode(TASK, "connect", "talk to ledger"), (policy.WRONG_MODE, policy.DISPATCH))
        self.assertEqual(policy.decide_mode(LIVE, "dispatch", "have kairos do it"), (policy.WRONG_MODE, policy.CONNECT))

    def test_explicit_hint_wins(self):
        self.assertEqual(policy.decide_mode(BOTH, "connect", "")[0], policy.CONNECT)
        self.assertEqual(policy.decide_mode(BOTH, "dispatch", "")[0], policy.DISPATCH)

    def test_wording(self):
        self.assertEqual(policy.decide_mode(BOTH, "auto", "let me talk to tabletop")[0], policy.CONNECT)
        self.assertEqual(policy.decide_mode(BOTH, "auto", "book a table at nopa for two")[0], policy.DISPATCH)
        self.assertEqual(policy.decide_mode(BOTH, "auto", "help me plan meals this week")[0], policy.CONNECT)
        self.assertEqual(policy.decide_mode(BOTH, "auto", "nopa tomorrow")[0], policy.DISPATCH)

    def test_explicit_dispatch_wording_beats_open_ended(self):
        self.assertEqual(policy.decide_mode(BOTH, "auto", "have tabletop help me plan dinner and let me know",
                                            missing_required=2)[0], policy.DISPATCH)

    def test_many_missing_details_connects(self):
        self.assertEqual(policy.decide_mode(BOTH, "auto", "restaurant thing", missing_required=2)[0], policy.CONNECT)

    def test_unclear_asks(self):
        self.assertEqual(policy.decide_mode(BOTH, "auto", "tabletop")[0], policy.ASK)


class IndirectTests(unittest.TestCase):
    def test_owns_domain_always_routes(self):
        self.assertTrue(policy.indirect_route(OWNS, "what's a reminder"))

    def test_on_request_routes_personal_or_action_only(self):
        self.assertTrue(policy.indirect_route(ON_REQ, "how many calories have I had today"))
        self.assertTrue(policy.indirect_route(ON_REQ, "log a bagel for breakfast"))
        self.assertFalse(policy.indirect_route(ON_REQ, "how many calories in a banana"))
        # "my …" questions only go to agents that hold the user's data.
        self.assertFalse(policy.indirect_route(NO_DATA, "what do I know about my history"))


class NotifyTests(unittest.TestCase):
    def test_grounded_notify(self):
        self.assertEqual(policy.resolve_notify("device", "book it and let me know"), "device")
        self.assertEqual(policy.resolve_notify("", "book it, no need to tell me"), "silent")
        self.assertEqual(policy.resolve_notify("device", "book it"), "next_session")  # not asked
        self.assertEqual(policy.resolve_notify("", "book it"), "next_session")


class WakeTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("ORCHESTRATOR_QUIET_HOURS")
        os.environ["ORCHESTRATOR_QUIET_HOURS"] = "22-7"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("ORCHESTRATOR_QUIET_HOURS", None)
        else:
            os.environ["ORCHESTRATOR_QUIET_HOURS"] = self._old

    def test_quiet_hours_in_user_timezone(self):
        late = datetime(2026, 10, 7, 6, 30, tzinfo=timezone.utc)   # 23:30 in LA
        noon = datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc)   # 12:00 in LA
        self.assertTrue(policy.in_quiet_hours("America/Los_Angeles", late))
        self.assertFalse(policy.in_quiet_hours("America/Los_Angeles", noon))
        self.assertFalse(policy.wake_allowed("America/Los_Angeles", 0, late))
        self.assertTrue(policy.wake_allowed("America/Los_Angeles", 3, noon))
        self.assertFalse(policy.wake_allowed("America/Los_Angeles", 4, noon))


if __name__ == "__main__":
    unittest.main()
