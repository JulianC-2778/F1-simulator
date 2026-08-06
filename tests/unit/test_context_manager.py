#!/usr/bin/env python3
"""Unit tests for midware/context_manager.py (Direction 1 prompt/context building)."""

import unittest

from midware.context_manager import ContextConfig, ContextManager, format_car_state


class FormatCarStateTests(unittest.TestCase):
    def test_renders_all_fields_and_joins_detected_problems(self):
        car_state = {
            "speed": 182.4,
            "rpm": 8712.0,
            "gear": 5,
            "track_pos": -0.42,
            "damage": 900.0,
            "fuel": 21.3,
            "lap_time": 63.85,
            "problems": ["rpm too high", "fuel low"],
        }
        text = format_car_state(car_state)
        self.assertIn("Speed: 182 km/h", text)
        self.assertIn("RPM: 8712", text)
        self.assertIn("Gear: 5", text)
        self.assertIn("Fuel remaining: 21.3 L", text)
        self.assertIn("Detected issues: rpm too high, fuel low", text)

    def test_reports_no_issues_when_problems_list_is_empty(self):
        text = format_car_state({"problems": []})
        self.assertIn("Detected issues: None -- car is on track and under control.", text)

    def test_reports_no_issues_when_analyzer_reports_normal(self):
        # race_analyzer.analyze_car_state() returns ["normal"], not [], when
        # nothing is wrong -- this must render the same as an empty list,
        # not literally the word "normal" (which the model misread as an
        # issue name and once caused it to claim the car was off-track).
        text = format_car_state({"problems": ["normal"]})
        self.assertIn("Detected issues: None -- car is on track and under control.", text)


class FormatEngineerPromptTests(unittest.TestCase):
    def test_combines_car_state_and_question(self):
        cm = ContextManager()
        prompt = cm.format_engineer_prompt({"speed": 100.0, "problems": []}, "Should I pit now?")
        self.assertIn("Current car data:", prompt)
        self.assertIn("Speed: 100 km/h", prompt)
        self.assertIn("Driver's question:\nShould I pit now?", prompt)


class ContextManagerBudgetTests(unittest.TestCase):
    """
    Exercises the token-budget trimming in ContextManager.build_messages().

    All message contents below are pure ASCII with lengths chosen so
    estimate_tokens() gives an exact, predictable token count (len // 4),
    which lets the test assert precisely which messages survive trimming
    instead of just "some subset".
    """

    def _manager(self, trim_strategy: str) -> ContextManager:
        config = ContextConfig(
            max_context_tokens=16,
            max_response_tokens=10,
            trim_strategy=trim_strategy,
            commentator_persona="sys",  # 3 ASCII chars -> 1 token
        )
        cm = ContextManager(config)
        cm.add_user("pin-00", pinned=True)  # 6 ASCII chars -> 1 token
        cm.add_user("msg-0000")  # 8 ASCII chars -> 2 tokens each
        cm.add_assistant("msg-1111")
        cm.add_user("msg-2222")
        cm.add_assistant("msg-3333")
        return cm

    def test_oldest_first_keeps_pinned_plus_most_recent_messages(self):
        cm = self._manager("oldest_first")
        contents = [m["content"] for m in cm.build_messages()]
        self.assertEqual(contents, ["sys", "pin-00", "msg-2222", "msg-3333"])
        # Building the trimmed prompt must not mutate the underlying history.
        self.assertEqual(len(cm.history), 5)

    def test_newest_first_keeps_pinned_plus_oldest_messages(self):
        cm = self._manager("newest_first")
        contents = [m["content"] for m in cm.build_messages()]
        self.assertEqual(contents, ["sys", "pin-00", "msg-0000", "msg-1111"])

    def test_clear_history_drops_everything_except_pinned_messages(self):
        cm = self._manager("oldest_first")
        cm.clear_history()
        self.assertEqual([m.content for m in cm.history], ["pin-00"])


if __name__ == "__main__":
    unittest.main()
