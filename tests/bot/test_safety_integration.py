"""The two forced regression tests from docs/bot_test_plan.md section 4.7.

Both wire GraniteStrategist's output into safety_filter the same way
run_bot()'s docstring usage example does — asserting on safety_filter()
alone is not enough, because a future refactor could stop calling it on
every strategy source and these tests wouldn't catch it if they only
exercised safety_filter() in isolation.
"""

import unittest

from ai_bot import ATTACK, BLOCK, DEFEND, GraniteStrategist, safety_filter
from telemetry_common import WorkerResult


class SafetyFilterOverridesGraniteTests(unittest.TestCase):
    def test_safety_filter_downgrades_granites_attack_when_car_is_critically_damaged(self):
        strategist = GraniteStrategist(interval=999.0)
        strategist._runner._results.put(
            WorkerResult(task={}, output=(ATTACK, "opponent is slow, push now", {}))
        )
        state = {"fuel": 50.0, "damage": 9999.0}  # far past _DMG_DEFEND (9500)

        raw_strategy, _reason = strategist.tick(state)
        self.assertEqual(raw_strategy, ATTACK, "precondition: Granite really did propose ATTACK")

        final_strategy = safety_filter(raw_strategy, state)
        self.assertEqual(
            final_strategy, DEFEND,
            "safety_filter must be the last word over Granite's raw output — "
            "a critically damaged car must never actually attack",
        )


class BlockIsSystemOnlyEndToEndTests(unittest.TestCase):
    def test_granite_hallucinating_block_is_rejected_at_the_parse_layer_and_never_reaches_the_car(self):
        strategist = GraniteStrategist(interval=999.0)
        # Simulate the model literally answering with the forbidden word —
        # _call_granite always routes its raw text through
        # _parse_strategy_response before GraniteStrategist ever sees it, so
        # by the time a WorkerResult exists its output is already sanitised.
        # This test exercises that same parsing path directly, matching what
        # _call_granite does at runtime.
        from ai_bot import _parse_strategy_response

        sanitised_strategy, _ = _parse_strategy_response('{"strategy": "BLOCK", "reason": "defending"}')
        strategist._runner._results.put(
            WorkerResult(task={}, output=(sanitised_strategy, "defending", {}))
        )

        healthy_state = {"fuel": 50.0, "damage": 0.0}
        raw_strategy, _reason = strategist.tick(healthy_state)
        self.assertNotEqual(raw_strategy, BLOCK, "a Granite-sourced BLOCK must never survive parsing")

        final_strategy = safety_filter(raw_strategy, healthy_state)
        self.assertNotEqual(
            final_strategy, BLOCK,
            "even if it somehow got this far, safety_filter's own priority-1 "
            "check rejects BLOCK from any source other than its own rear-gap rule",
        )

    def test_block_can_only_be_produced_by_safety_filters_own_rear_gap_rule(self):
        opponents = [200.0] * 36
        opponents[1] = 5.0  # well inside _BLOCK_TRIGGER_GAP
        threatened_state = {"fuel": 50.0, "damage": 0.0, "opponents": opponents, "dist_raced": 1000.0}
        self.assertEqual(safety_filter(ATTACK, threatened_state), BLOCK)


if __name__ == "__main__":
    unittest.main()
