"""Pure functions around the Granite strategy call: prompt construction,
response parsing and strategy debouncing. None of these do any I/O, so none
of these tests need a real (or even mocked) network/Granite connection — see
docs/bot_test_plan.md section 4.5. Ports the corresponding assertions from
ai_bot.py's built-in ``_run_tests()``.
"""

import unittest

from ai_bot import (
    ATTACK,
    DEFEND,
    NORMAL,
    SAVE_FUEL,
    _build_strategy_prompt,
    _next_debounced_strategy,
    _parse_strategy_response,
)


class ParseStrategyResponseTests(unittest.TestCase):
    def test_valid_json_extracts_strategy_and_reason(self):
        strategy, reason = _parse_strategy_response(
            '{"strategy": "ATTACK", "reason": "clear track ahead"}'
        )
        self.assertEqual(strategy, ATTACK)
        self.assertEqual(reason, "clear track ahead")

    def test_lower_case_strategy_is_normalised(self):
        strategy, _ = _parse_strategy_response('{"strategy": "defend", "reason": "opponent close"}')
        self.assertEqual(strategy, DEFEND)

    def test_unknown_strategy_name_falls_back_to_normal(self):
        strategy, _ = _parse_strategy_response('{"strategy": "TURBO", "reason": "go fast"}')
        self.assertEqual(strategy, NORMAL)

    def test_non_json_text_falls_back_to_normal(self):
        strategy, _ = _parse_strategy_response("Sorry, I cannot help with that.")
        self.assertEqual(strategy, NORMAL)

    def test_block_is_rejected_even_if_granite_says_it(self):
        # BLOCK is system-only (see safety_filter's own priority-1 rule) —
        # this is the same constraint enforced at the parsing layer, so a
        # hallucinated "BLOCK" from the model text is rejected here too,
        # independently of safety_filter ever running.
        strategy, _ = _parse_strategy_response('{"strategy": "BLOCK", "reason": "defending"}')
        self.assertEqual(strategy, NORMAL)

    def test_missing_reason_field_is_empty_string(self):
        strategy, reason = _parse_strategy_response('{"strategy": "SAVE_FUEL"}')
        self.assertEqual(strategy, SAVE_FUEL)
        self.assertEqual(reason, "")

    def test_empty_text_falls_back_to_normal(self):
        strategy, reason = _parse_strategy_response("")
        self.assertEqual(strategy, NORMAL)
        self.assertEqual(reason, "parse error")

    def test_strategy_field_missing_entirely_falls_back_to_normal(self):
        strategy, _ = _parse_strategy_response('{"reason": "no strategy given"}')
        self.assertEqual(strategy, NORMAL)


class NextDebouncedStrategyTests(unittest.TestCase):
    """Pure transition function; _STRATEGY_CONFIRM is currently 1, so a
    single differing proposal switches immediately (no smoothing)."""

    def test_single_flip_switches_immediately(self):
        active, candidate, count, switched = _next_debounced_strategy(NORMAL, None, 0, ATTACK)
        self.assertEqual(active, ATTACK)
        self.assertTrue(switched)
        self.assertIsNone(candidate)
        self.assertEqual(count, 0)

    def test_back_to_back_flip_also_switches_immediately(self):
        active, candidate, count, switched = _next_debounced_strategy(ATTACK, None, 0, DEFEND)
        self.assertEqual(active, DEFEND)
        self.assertTrue(switched)

    def test_reconfirming_the_active_strategy_is_a_noop(self):
        active, candidate, count, switched = _next_debounced_strategy(NORMAL, DEFEND, 1, NORMAL)
        self.assertEqual(active, NORMAL)
        self.assertFalse(switched)
        self.assertIsNone(candidate, "a stale candidate must be cleared on re-confirm")
        self.assertEqual(count, 0)

    def test_proposed_equal_to_active_short_circuits_without_touching_candidate(self):
        active, candidate, count, switched = _next_debounced_strategy(ATTACK, "DEFEND", 3, ATTACK)
        self.assertEqual((active, candidate, count, switched), (ATTACK, None, 0, False))


class BuildStrategyPromptTests(unittest.TestCase):
    def test_prompt_contains_key_live_data_fields(self):
        state = {
            "speed_x": 120.0, "fuel": 18.0, "damage": 500.0,
            "track_pos": 0.1, "gear": 4, "race_pos": 3,
            "dist_raced": 1200.0,
            "track": [200.0] * 19,
            "opponents": [200.0] * 36,
        }
        prompt = _build_strategy_prompt(state)
        self.assertIn("ATTACK", prompt, "must include the strategy guide")
        self.assertIn("120.0", prompt, "must include speed")
        self.assertIn("18.0", prompt, "must include fuel")
        self.assertIn("strategy", prompt, "must include the JSON schema hint")

    def test_missing_optional_fields_use_defaults_without_crashing(self):
        prompt = _build_strategy_prompt({})
        self.assertIn("strategy", prompt)


if __name__ == "__main__":
    unittest.main()
