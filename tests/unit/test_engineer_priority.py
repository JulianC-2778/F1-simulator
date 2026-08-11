#!/usr/bin/env python3
"""Unit tests for engineer_priority.py (Direction 1 addition: priority synthesis)."""

import unittest

from engineer_priority import FRESH_INCIDENT_WINDOW_SECONDS, summarize_priority


def _pit_window(urgency="low", reasons=None):
    return {"recommend_pit": urgency != "low", "urgency": urgency, "reasons": reasons or []}


class SummarizePriorityTests(unittest.TestCase):
    def test_no_signals_at_all_gives_the_all_clear(self):
        result = summarize_priority({"problems": []}, _pit_window(), [])
        self.assertEqual(result["top_priority"], "no urgent priority -- car is in good shape")
        self.assertEqual(result["severity"], "low")

    def test_off_track_wins_even_with_no_other_signals(self):
        result = summarize_priority({"problems": ["off track"]}, _pit_window(), [])
        self.assertEqual(result["top_priority"], "get back on track")
        self.assertEqual(result["severity"], "high")

    def test_near_track_edge_also_counts_as_off_track(self):
        result = summarize_priority({"problems": ["near track edge"]}, _pit_window(), [])
        self.assertEqual(result["top_priority"], "get back on track")

    def test_off_track_beats_a_high_urgency_pit_window(self):
        # Driving correction comes first even if tires/fuel are also critical --
        # matches ENGINEER_PERSONA_TAIL's existing "off-track is not a pit
        # matter" rule.
        result = summarize_priority(
            {"problems": ["off track"]}, _pit_window("high", ["tires", "fuel"]), []
        )
        self.assertEqual(result["top_priority"], "get back on track")

    def test_high_pit_urgency_reports_pit_now(self):
        result = summarize_priority({"problems": []}, _pit_window("high", ["tires"]), [])
        self.assertEqual(result["top_priority"], "pit now")
        self.assertEqual(result["severity"], "high")
        self.assertEqual(result["reason"], "tires")

    def test_medium_pit_urgency_reports_plan_a_pit_stop_soon(self):
        result = summarize_priority({"problems": []}, _pit_window("medium", ["fuel"]), [])
        self.assertEqual(result["top_priority"], "plan a pit stop soon")
        self.assertEqual(result["severity"], "medium")

    def test_multiple_pit_reasons_are_joined_in_the_reason_text(self):
        result = summarize_priority({"problems": []}, _pit_window("high", ["tires", "fuel"]), [])
        self.assertEqual(result["reason"], "tires, fuel")

    def test_pit_urgency_beats_a_stale_incident(self):
        stale = [{"type": "damage", "detail": "took damage (+500)", "seconds_ago": 999.0}]
        result = summarize_priority({"problems": []}, _pit_window("high", ["tires"]), stale)
        self.assertEqual(result["top_priority"], "pit now")

    def test_fresh_incident_is_reported_when_nothing_else_is_urgent(self):
        fresh = [{"type": "off_track", "detail": "went off track (track position 1.20)", "seconds_ago": 5.0}]
        result = summarize_priority({"problems": []}, _pit_window(), fresh)
        self.assertEqual(result["top_priority"], "no urgent action, but note a recent incident")
        self.assertEqual(result["severity"], "low")
        self.assertEqual(result["reason"], "went off track (track position 1.20)")

    def test_incident_right_at_the_freshness_boundary_still_counts(self):
        fresh = [{"type": "damage", "detail": "took damage (+300)", "seconds_ago": FRESH_INCIDENT_WINDOW_SECONDS}]
        result = summarize_priority({"problems": []}, _pit_window(), fresh)
        self.assertEqual(result["top_priority"], "no urgent action, but note a recent incident")

    def test_stale_incident_beyond_the_freshness_window_is_ignored(self):
        stale = [{"type": "damage", "detail": "took damage (+300)", "seconds_ago": FRESH_INCIDENT_WINDOW_SECONDS + 0.1}]
        result = summarize_priority({"problems": []}, _pit_window(), stale)
        self.assertEqual(result["top_priority"], "no urgent priority -- car is in good shape")

    def test_off_track_is_categorized_as_physical(self):
        # Distinct from "strategic" (pit decisions) -- alarm-fatigue design
        # (aviation/medical HMI) treats immediate physical danger differently
        # from a strategy reminder; see _next_engineer_alert in runtime.py.
        result = summarize_priority({"problems": ["off track"]}, _pit_window(), [])
        self.assertEqual(result["category"], "physical")

    def test_high_pit_urgency_is_categorized_as_strategic(self):
        result = summarize_priority({"problems": []}, _pit_window("high", ["tires"]), [])
        self.assertEqual(result["category"], "strategic")

    def test_medium_pit_urgency_is_categorized_as_strategic(self):
        result = summarize_priority({"problems": []}, _pit_window("medium", ["fuel"]), [])
        self.assertEqual(result["category"], "strategic")

    def test_fresh_incident_with_nothing_urgent_is_categorized_as_informational(self):
        fresh = [{"type": "damage", "detail": "took damage (+300)", "seconds_ago": 1.0}]
        result = summarize_priority({"problems": []}, _pit_window(), fresh)
        self.assertEqual(result["category"], "informational")

    def test_all_clear_is_categorized_as_informational(self):
        result = summarize_priority({"problems": []}, _pit_window(), [])
        self.assertEqual(result["category"], "informational")

    def test_most_recent_fresh_incident_is_used_when_several_are_fresh(self):
        # recent_events from engineer_events.py is newest-first.
        incidents = [
            {"type": "damage", "detail": "took damage (+300)", "seconds_ago": 2.0},
            {"type": "off_track", "detail": "went off track (track position 1.10)", "seconds_ago": 8.0},
        ]
        result = summarize_priority({"problems": []}, _pit_window(), incidents)
        self.assertEqual(result["reason"], "took damage (+300)")


if __name__ == "__main__":
    unittest.main()
