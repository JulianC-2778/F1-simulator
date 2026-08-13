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

    def test_renders_pit_window_including_estimated_laps_remaining(self):
        car_state = {
            "problems": [],
            "pit_window": {
                "recommend_pit": True,
                "urgency": "high",
                "reasons": ["tires", "fuel"],
                "laps_of_fuel_left": 3.0,
                "laps_of_tire_left": 1.5,
                "estimated_laps_remaining": 1.5,
            },
        }
        text = format_car_state(car_state)
        self.assertIn(
            "Pit window analysis: recommend pit = yes, urgency = high, reasons = tires, fuel, "
            "estimated laps of fuel left = 3.0, estimated laps until critical tire wear = 1.5, "
            "estimated laps remaining before a pit is needed = 1.5",
            text,
        )

    def test_pit_window_shows_unknown_when_laps_estimates_are_not_available(self):
        car_state = {
            "problems": [],
            "pit_window": {
                "recommend_pit": False, "urgency": "low", "reasons": [],
                "laps_of_fuel_left": None, "laps_of_tire_left": None, "estimated_laps_remaining": None,
            },
        }
        text = format_car_state(car_state)
        self.assertIn("estimated laps of fuel left = unknown", text)
        self.assertIn("estimated laps until critical tire wear = unknown", text)
        self.assertIn("estimated laps remaining before a pit is needed = unknown", text)

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
            safety_margin_tokens=0,  # exact-arithmetic tests below don't exercise the margin
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


class ContextManagerSafetyMarginTests(unittest.TestCase):
    """Regression test for a real defect found during work package C: with
    the production defaults (max_context_tokens=4096, max_response_tokens=512),
    build_messages() filled the budget with zero headroom
    (4096 - 512 = 3584, used exactly). estimate_tokens() is a heuristic (1
    token ~= 4 ASCII chars), not a real tokenizer, so any underestimate of
    even a single token overflowed the model's actual hard context limit
    and the request failed outright -- observed after ~15-20 minutes of a
    long-lived commentary session once history grew large enough to
    saturate the budget. See docs/commentary_test_handoff_2.md section 3.
    """

    def test_default_config_leaves_a_real_safety_margin(self):
        self.assertGreater(ContextConfig().safety_margin_tokens, 0)

    def test_build_messages_never_fills_the_budget_to_the_exact_edge(self):
        # Saturate history with enough long messages that, pre-fix, trimming
        # would fill right up to max_context_tokens - max_response_tokens
        # with no room to spare -- exactly the condition that triggered the
        # real 400 error against the served model.
        config = ContextConfig()  # production defaults
        cm = ContextManager(config)
        filler = "x" * 400  # ASCII, estimate_tokens() -> 100 tokens/message
        for _ in range(60):  # 6000 tokens of history, far more than the budget
            cm.add_user(filler)
            cm.add_assistant(filler)

        messages = cm.build_messages()
        from midware.context_manager import estimate_tokens
        total_prompt_tokens = sum(estimate_tokens(m["content"]) for m in messages)

        # The real failure mode was total_prompt_tokens + max_response_tokens
        # landing exactly on max_context_tokens (zero headroom). This must
        # now leave a real, non-trivial margin -- not just "not negative".
        headroom = config.max_context_tokens - config.max_response_tokens - total_prompt_tokens
        self.assertGreaterEqual(
            headroom, config.safety_margin_tokens,
            f"only {headroom} tokens of headroom, expected at least "
            f"safety_margin_tokens={config.safety_margin_tokens}",
        )


if __name__ == "__main__":
    unittest.main()
