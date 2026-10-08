"""Validation gate (ORCHESTRATOR_V2_TOOL_CALLS.md §2.4)."""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../app")))

from orchestrator.routing import gate  # noqa: E402

SCHEMA = [{"name": "restaurant", "required": True}, {"name": "party_size", "required": True,
                                                     "question": "For how many people?"},
          {"name": "time", "required": False}]


class GroundingTests(unittest.TestCase):
    def test_numbers_in_words_and_digits(self):
        heard = "book nopa for two at seven tonight"
        self.assertTrue(gate.grounded(2, heard))
        self.assertTrue(gate.grounded("7pm", heard))
        self.assertTrue(gate.grounded("19:00", heard))
        self.assertTrue(gate.grounded("2026-10-07T19:00:00-07:00", heard))
        self.assertFalse(gate.grounded(4, heard))
        self.assertFalse(gate.grounded("20:00", heard))

    def test_names_case_punctuation_and_phonetics(self):
        heard = "can you get me a table at nopa"
        self.assertTrue(gate.grounded("Nopa", heard))
        self.assertTrue(gate.grounded("NOPA!", heard))
        self.assertFalse(gate.grounded("Zuni Cafe", heard))
        self.assertTrue(gate.grounded("Kairos", "talk to cairo's"))

    def test_half_of_content_words(self):
        self.assertTrue(gate.grounded("the Nopa restaurant downtown", "nopa downtown please"))
        self.assertFalse(gate.grounded("Nopa downtown on Divisadero", "a table please"))

    def test_relative_times(self):
        self.assertTrue(gate.grounded("2026-10-07T18:00:00Z", "forget it if it's not done in an hour"))
        self.assertFalse(gate.grounded("2026-10-07T18:00:00Z", "book a table"))

    def test_booleans_and_empty_always_grounded(self):
        self.assertTrue(gate.grounded(True, ""))
        self.assertTrue(gate.grounded("", "anything"))


class CheckSlotsTests(unittest.TestCase):
    def test_complete_request_passes(self):
        r = gate.check_slots({"restaurant": "Nopa", "party_size": 2, "time": "19:00"},
                             ["book nopa for two at seven"], SCHEMA)
        self.assertTrue(r.ok)

    def test_missing_required_asks(self):
        r = gate.check_slots({"restaurant": "Nopa"}, ["book nopa"], SCHEMA)
        self.assertFalse(r.ok)
        self.assertEqual((r.slot, r.reason, r.question), ("party_size", "missing", "For how many people?"))

    def test_invented_value_asks(self):
        r = gate.check_slots({"restaurant": "Nopa", "party_size": 2, "time": "19:00"},
                             ["book nopa for two"], SCHEMA)
        self.assertFalse(r.ok)
        self.assertEqual((r.slot, r.reason), ("time", "ungrounded"))
        self.assertEqual(r.question, "For what time?")

    def test_earlier_turns_count(self):
        r = gate.check_slots({"restaurant": "Nopa", "party_size": 2},
                             ["I want dinner at nopa", "for two people"], SCHEMA)
        self.assertTrue(r.ok)

    def test_late_self_correction(self):
        r = gate.check_slots({"restaurant": "Nopa", "party_size": 2, "time": "19:00"},
                             ["book nopa for two at seven, no, eight"], SCHEMA)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "correction")
        self.assertIn("eight", r.question)

    def test_later_value_passes_after_correction(self):
        r = gate.check_slots({"restaurant": "Nopa", "party_size": 2, "time": "20:00"},
                             ["book nopa for two at seven, no, eight"], SCHEMA)
        self.assertTrue(r.ok)

    def test_skip(self):
        r = gate.check_slots({"x": "invented"}, ["hello"], skip=["x"])
        self.assertTrue(r.ok)

    def test_fast(self):
        t = time.perf_counter()
        for _ in range(200):
            gate.check_slots({"restaurant": "Nopa", "party_size": 2, "time": "19:00"},
                             ["I want dinner", "book nopa for two at seven, no, eight"], SCHEMA)
        self.assertLess((time.perf_counter() - t) / 200, 0.02)

    def test_read_back_summary(self):
        self.assertEqual(gate.summarize_request("book Nopa for 2 at 7", "Tabletop"),
                         "Book Nopa for 2 at 7, with Tabletop")


if __name__ == "__main__":
    unittest.main()
