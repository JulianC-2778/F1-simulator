"""display_label(): dashboard wording only, never a strategy.

The point of this module is that a demo can show a situation-aware name
("Chasing · full throttle") that changes as the race changes, without adding
strategies. Adding one would move every measured result — safety_filter's
branches, bot_replay.py's diversity/sensitivity metrics, the strategy
distributions in docs/bot_prompt_comparison_race3.md — so the tests below
pin the separation rather than the exact wording.
"""

import unittest

from ai_bot import (
    ATTACK, BLOCK, DEFEND, NORMAL, PIT,
    _ALL_STRATEGIES, _DMG_NO_ATTACK,
    display_label, safety_filter,
)


def _state(**over):
    base = {"fuel": 50.0, "damage": 0.0, "race_pos": 5,
            "dist_raced": 5000.0, "opponents": [200.0] * 36}
    base.update(over)
    return base


def _with_car_ahead(metres):
    opps = [200.0] * 36
    for i in range(15, 22):          # _FRONT_CONE
        opps[i] = metres
    return opps


def _with_car_behind(metres):
    opps = [200.0] * 36
    for i in list(range(0, 4)) + list(range(32, 36)):
        opps[i] = metres
    return opps


class LabelIsPresentationOnlyTests(unittest.TestCase):
    def test_label_never_changes_the_strategy_safety_filter_picks(self):
        # The whole guarantee in one test: computing a label must not perturb
        # the decision, whatever the situation.
        situations = [
            _state(),
            _state(race_pos=1),
            _state(opponents=_with_car_ahead(20.0)),
            _state(opponents=_with_car_behind(8.0)),
            _state(damage=9999.0),
            _state(fuel=1.0),
        ]
        for st in situations:
            with self.subTest(state=st):
                before = safety_filter(ATTACK, dict(st))
                display_label(before, dict(st))
                after = safety_filter(ATTACK, dict(st))
                self.assertEqual(before, after)

    def test_label_never_returns_a_strategy_name_for_known_strategies(self):
        # If a label were ever equal to a strategy name, a reader (or a future
        # careless commit) could mistake the display string for the decision.
        for strategy in _ALL_STRATEGIES:
            with self.subTest(strategy=strategy):
                label = display_label(strategy, _state())
                self.assertNotIn(label, _ALL_STRATEGIES)

    def test_unknown_strategy_falls_back_to_its_own_name(self):
        self.assertEqual(display_label("SOMETHING_NEW", _state()), "SOMETHING_NEW")


class LabelTracksTheSituationTests(unittest.TestCase):
    """ATTACK is one strategy but three different race situations — this is
    what makes the badge move between model answers."""

    def test_attack_reads_differently_when_leading_chasing_or_neither(self):
        leading = display_label(ATTACK, _state(race_pos=1))
        chasing = display_label(ATTACK, _state(race_pos=3, opponents=_with_car_ahead(20.0)))
        plain = display_label(ATTACK, _state(race_pos=3))
        self.assertEqual(len({leading, chasing, plain}), 3)

    def test_chasing_wins_over_leading_when_a_car_is_actually_in_range(self):
        # P1 with a car 20 m up the road is being lapped or is mid-overtake;
        # "Leading" would be actively misleading there.
        self.assertEqual(
            display_label(ATTACK, _state(race_pos=1, opponents=_with_car_ahead(20.0))),
            display_label(ATTACK, _state(race_pos=3, opponents=_with_car_ahead(20.0))),
        )

    def test_normal_distinguishes_being_pressed_from_cruising(self):
        pressed = display_label(NORMAL, _state(opponents=_with_car_behind(8.0)))
        cruising = display_label(NORMAL, _state())
        self.assertNotEqual(pressed, cruising)

    def test_defend_distinguishes_damaged_from_merely_cautious(self):
        hurt = display_label(DEFEND, _state(damage=_DMG_NO_ATTACK + 1))
        cautious = display_label(DEFEND, _state(damage=0.0))
        self.assertNotEqual(hurt, cautious)

    def test_every_strategy_has_a_non_empty_label(self):
        for strategy in (ATTACK, NORMAL, DEFEND, PIT, BLOCK):
            with self.subTest(strategy=strategy):
                self.assertTrue(display_label(strategy, _state()).strip())

    def test_missing_opponent_data_does_not_raise(self):
        # Early frames and the telemetry-unavailable fallback both hand over a
        # state with no opponents array at all.
        self.assertTrue(display_label(ATTACK, {"race_pos": 1}))
        self.assertTrue(display_label(NORMAL, {}))


if __name__ == "__main__":
    unittest.main()
