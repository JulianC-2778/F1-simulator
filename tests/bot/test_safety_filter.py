"""safety_filter: the last line of defence between a Granite/LLM strategy
pick and the actual car — see docs/bot_test_plan.md section 4.4 (marked P0:
this is the one piece of ai_bot.py whose correctness stops the car from
driving itself into a wall on bad model output, and it had zero pytest
coverage before this file).

safety_filter() is a pure function (no I/O, no module state), so every case
here is a plain input -> output assertion. Priorities are checked in
descending order and the first match wins (see the function's own
docstring) — the boundary tests below probe T-eps/T/T+eps around every
threshold constant in ai_bot.py, plus the priority-interaction cases the
built-in ``_run_tests()`` self-test already covered (ported here so they run
under pytest/CI instead of only via ``python ai_bot.py``).

Updated 2026-08-12 after merging in a same-day upstream pit-system rework
(commits "avoid"/"avoid speed"/"avoid reverse frame-by-frame calculation"/
"pit"/"fix pit-lane docking") that changed real behaviour here:
``_FUEL_PIT`` moved 5.0 -> 10.0, PIT now only actually engages once
``_near_pit_lane()`` says the car is close to a real pit lane (otherwise a
fuel-critical/PIT-requesting car is downgraded to ATTACK and keeps racing —
see ``_PIT_APPROACH_DIST``'s comment), and two feature flags
(``_SAVE_FUEL_ENABLED``, ``_FUEL_CAUTION_ENABLED``) were added, both
currently ``False`` per an explicit "TEMP (pit-system testing, 2026-08-09)
... flip back to True to restore normal behaviour" comment in ai_bot.py.
Tests that depend on those two flags read the live value from ``ai_bot``
instead of hardcoding an assumption, so they stay correct either way the
flags end up set.
"""

import unittest

import ai_bot
from ai_bot import ATTACK, BLOCK, DEFEND, NORMAL, PIT, SAVE_FUEL, safety_filter

_EPS = 1e-6


def _rear_close_opponents():
    # index 1 sits in the rear sensor cone read by _rear_gap (see ai_bot.py's
    # own self-test comment: "index 0-3/32-35 = rear cone").
    opponents = [200.0] * 36
    opponents[1] = 15.0
    return opponents


class PriorityOneUnknownStrategyTests(unittest.TestCase):
    """Priority 1: anything not in _GRANITE_STRATEGIES -> NORMAL."""

    base = {"fuel": 50.0, "damage": 0.0}

    def test_none_becomes_normal(self):
        self.assertEqual(safety_filter(None, self.base), NORMAL)

    def test_empty_string_becomes_normal(self):
        self.assertEqual(safety_filter("", self.base), NORMAL)

    def test_unknown_word_becomes_normal(self):
        self.assertEqual(safety_filter("TURBO", self.base), NORMAL)

    def test_system_only_block_is_rejected_even_when_healthy(self):
        # Granite must never be able to self-select BLOCK just by saying the
        # word — it is a deterministic per-frame reflex driven by
        # safety_filter itself (priority 6), not a strategic choice.
        self.assertEqual(safety_filter(BLOCK, self.base), NORMAL)

    def test_known_healthy_strategies_pass_through_unchanged(self):
        # PIT and SAVE_FUEL deliberately excluded here: PIT's pass-through is
        # now conditional on pit-lane proximity (see PriorityTwoFuelPitTests)
        # and SAVE_FUEL's availability is gated by a feature flag (see
        # SaveFuelStrategyAvailabilityTests) — neither is a plain "healthy ->
        # unchanged" case anymore.
        self.assertEqual(safety_filter(ATTACK, self.base), ATTACK)
        self.assertEqual(safety_filter(NORMAL, self.base), NORMAL)
        self.assertEqual(safety_filter(DEFEND, self.base), DEFEND)


class SaveFuelStrategyAvailabilityTests(unittest.TestCase):
    """SAVE_FUEL's availability is gated by ``_SAVE_FUEL_ENABLED`` (2026-08-09
    pit-system rework: disabled by default so low fuel resolves straight to
    PIT instead of Granite hedging with economical driving first — see the
    "TEMP (pit-system testing...)" comment above SAVE_FUEL's definition).
    Reads the live flag so this test stays correct either way it's set."""

    def test_save_fuel_matches_the_current_flag_state(self):
        result = safety_filter(SAVE_FUEL, {"fuel": 50.0, "damage": 0.0})
        if ai_bot._SAVE_FUEL_ENABLED:
            self.assertEqual(result, SAVE_FUEL)
        else:
            self.assertEqual(result, NORMAL, "disabled -> not in _GRANITE_STRATEGIES -> priority 1 -> NORMAL")


class PriorityTwoFuelPitTests(unittest.TestCase):
    """Priority 2: fuel < _FUEL_PIT -> PIT, but ONLY once the car is
    actually near a real pit lane (``_near_pit_lane()``); far from one, a
    fuel-critical car (or one where Granite itself asked for PIT) is
    downgraded to ATTACK and keeps racing instead of crawling a lap early at
    PIT's ~50 km/h cap for nothing — see ai_bot.py's 2026-08-09
    _PIT_APPROACH_DIST/_near_pit_lane comments."""

    @staticmethod
    def _near_pit_state(**overrides):
        # 10 m before a pit entry on a 2000 m track -- inside
        # _PIT_APPROACH_DIST (150 m), so _near_pit_lane() is True.
        state = {"fuel": 50.0, "damage": 0.0, "dist_from_start": 990.0,
                 "track_length": 2000.0, "pit_entry": 1000.0, "pit_exit": 1050.0}
        state.update(overrides)
        return state

    def test_below_threshold_and_near_pit_lane_forces_pit(self):
        state = self._near_pit_state(fuel=ai_bot._FUEL_PIT - _EPS)
        self.assertEqual(safety_filter(ATTACK, state), PIT)
        self.assertEqual(safety_filter(NORMAL, state), PIT)

    def test_exactly_at_threshold_does_not_trigger_even_near_pit(self):
        # `fuel < _FUEL_PIT` is a strict less-than.
        state = self._near_pit_state(fuel=ai_bot._FUEL_PIT)
        self.assertEqual(safety_filter(NORMAL, state), NORMAL)

    def test_just_above_threshold_does_not_trigger(self):
        state = self._near_pit_state(fuel=ai_bot._FUEL_PIT + _EPS)
        self.assertEqual(safety_filter(NORMAL, state), NORMAL)

    def test_granite_requesting_pit_near_the_lane_is_honoured_regardless_of_fuel(self):
        state = self._near_pit_state(fuel=50.0)
        self.assertEqual(safety_filter(PIT, state), PIT)

    def test_below_threshold_but_far_from_pit_lane_keeps_racing(self):
        # No pit-lane telemetry at all -- _near_pit_lane() is unconditionally
        # False -- so neither a fuel-critical car nor Granite's own PIT
        # request actually engages PIT; both fall back to ATTACK.
        state = {"fuel": ai_bot._FUEL_PIT - 1.0, "damage": 0.0}
        self.assertEqual(safety_filter(PIT, state), ATTACK)
        self.assertEqual(safety_filter(NORMAL, state), NORMAL)


class PriorityThreeCriticalDamageDefendTests(unittest.TestCase):
    """Priority 3: damage >= _DMG_DEFEND (9500) -> DEFEND."""

    def test_above_threshold_forces_defend(self):
        state = {"fuel": 50.0, "damage": ai_bot._DMG_DEFEND + _EPS}
        self.assertEqual(safety_filter(ATTACK, state), DEFEND)
        self.assertEqual(safety_filter(NORMAL, state), DEFEND)

    def test_exactly_at_threshold_triggers(self):
        # `damage >= _DMG_DEFEND` is inclusive.
        state = {"fuel": 50.0, "damage": ai_bot._DMG_DEFEND}
        self.assertEqual(safety_filter(ATTACK, state), DEFEND)

    def test_just_below_threshold_does_not_trigger_defend(self):
        state = {"fuel": 50.0, "damage": ai_bot._DMG_DEFEND - _EPS}
        self.assertNotEqual(safety_filter(ATTACK, state), DEFEND)


class PriorityFourNoAttackOnDamageTests(unittest.TestCase):
    """Priority 4: damage >= _DMG_NO_ATTACK (8000) disallows ATTACK -> NORMAL."""

    def test_above_threshold_downgrades_attack_to_normal(self):
        state = {"fuel": 50.0, "damage": ai_bot._DMG_NO_ATTACK + _EPS}
        self.assertEqual(safety_filter(ATTACK, state), NORMAL)

    def test_exactly_at_threshold_downgrades(self):
        state = {"fuel": 50.0, "damage": ai_bot._DMG_NO_ATTACK}
        self.assertEqual(safety_filter(ATTACK, state), NORMAL)

    def test_just_below_threshold_leaves_attack_alone(self):
        state = {"fuel": 50.0, "damage": ai_bot._DMG_NO_ATTACK - _EPS}
        self.assertEqual(safety_filter(ATTACK, state), ATTACK)

    def test_non_attack_strategies_unaffected_in_this_damage_band(self):
        # Only ATTACK is downgraded here; DEFEND/NORMAL/SAVE_FUEL pass
        # through untouched by this specific rule.
        state = {"fuel": 50.0, "damage": ai_bot._DMG_NO_ATTACK + 100.0}
        self.assertEqual(safety_filter(DEFEND, state), DEFEND)
        self.assertEqual(safety_filter(NORMAL, state), NORMAL)


class PriorityFiveNoAttackOnLowFuelTests(unittest.TestCase):
    """Priority 5: fuel < _FUEL_CAUTION (15.0) disallows ATTACK -> NORMAL,
    but ONLY while ``_FUEL_CAUTION_ENABLED`` is True. As of the 2026-08-09
    pit-system rework this flag defaults to False ("the car now commits to
    ATTACK everywhere except right at the pit lane") — these tests read the
    live flag value so they stay correct whichever way it's set."""

    def test_below_threshold_matches_the_current_flag_state(self):
        state = {"fuel": ai_bot._FUEL_CAUTION - _EPS, "damage": 0.0}
        result = safety_filter(ATTACK, state)
        if ai_bot._FUEL_CAUTION_ENABLED:
            self.assertEqual(result, NORMAL)
        else:
            self.assertEqual(result, ATTACK)

    def test_exactly_at_threshold_never_triggers_regardless_of_flag(self):
        state = {"fuel": ai_bot._FUEL_CAUTION, "damage": 0.0}
        self.assertEqual(safety_filter(ATTACK, state), ATTACK)

    def test_just_above_threshold_never_triggers_regardless_of_flag(self):
        state = {"fuel": ai_bot._FUEL_CAUTION + _EPS, "damage": 0.0}
        self.assertEqual(safety_filter(ATTACK, state), ATTACK)


class PrioritySixBlockOnRearThreatTests(unittest.TestCase):
    """Priority 6: bgap < _BLOCK_TRIGGER_GAP (20.0) AND healthy AND not
    launching (dist_raced >= _START_CAUTION_DIST, 150.0) -> BLOCK."""

    def test_close_rear_car_and_healthy_triggers_block(self):
        state = {"fuel": 50.0, "damage": 0.0, "opponents": _rear_close_opponents(),
                  "dist_raced": 1000.0}
        self.assertEqual(safety_filter(ATTACK, state), BLOCK)
        self.assertEqual(safety_filter(NORMAL, state), BLOCK)

    def test_distant_rear_car_does_not_trigger_block(self):
        state = {"fuel": 50.0, "damage": 0.0, "opponents": [200.0] * 36, "dist_raced": 1000.0}
        self.assertEqual(safety_filter(ATTACK, state), ATTACK)

    def test_damaged_car_does_not_attempt_block_even_with_rear_threat(self):
        # The existing damage priority (4) wins: a damaged car should get
        # home safe, not try to defend a position.
        state = {"fuel": 50.0, "damage": ai_bot._DMG_NO_ATTACK,
                  "opponents": _rear_close_opponents(), "dist_raced": 1000.0}
        self.assertEqual(safety_filter(ATTACK, state), NORMAL)

    def test_low_fuel_car_does_not_attempt_block_even_with_rear_threat(self):
        # Priority 6's own gate (`fuel >= _FUEL_CAUTION`) blocks BLOCK for a
        # low-fuel car regardless of _FUEL_CAUTION_ENABLED; whether ATTACK
        # itself gets downgraded to NORMAL first (priority 5) still depends
        # on that flag.
        state = {"fuel": ai_bot._FUEL_CAUTION - 1.0, "damage": 0.0,
                  "opponents": _rear_close_opponents(), "dist_raced": 1000.0}
        result = safety_filter(ATTACK, state)
        expected = NORMAL if ai_bot._FUEL_CAUTION_ENABLED else ATTACK
        self.assertEqual(result, expected)
        self.assertNotEqual(result, BLOCK, "a low-fuel car must never attempt BLOCK")

    def test_launch_window_suppresses_block_even_at_close_rear_gap(self):
        # Regression (live on-track, 2026-08-07): a standing two-row grid
        # puts every neighbour within the trigger gap by construction, so
        # BLOCK must not fire during the launch window.
        state = {"fuel": 50.0, "damage": 0.0, "opponents": _rear_close_opponents(),
                  "dist_raced": ai_bot._START_CAUTION_DIST - 1.0}
        self.assertEqual(safety_filter(ATTACK, state), ATTACK)

    def test_same_rear_gap_past_the_launch_window_still_triggers_block(self):
        state = {"fuel": 50.0, "damage": 0.0, "opponents": _rear_close_opponents(),
                  "dist_raced": ai_bot._START_CAUTION_DIST + 1.0}
        self.assertEqual(safety_filter(ATTACK, state), BLOCK)

    def test_bgap_exactly_at_threshold_does_not_trigger(self):
        opponents = [200.0] * 36
        opponents[1] = ai_bot._BLOCK_TRIGGER_GAP
        state = {"fuel": 50.0, "damage": 0.0, "opponents": opponents, "dist_raced": 1000.0}
        self.assertEqual(safety_filter(ATTACK, state), ATTACK)

    def test_bgap_just_under_threshold_triggers(self):
        opponents = [200.0] * 36
        opponents[1] = ai_bot._BLOCK_TRIGGER_GAP - _EPS
        state = {"fuel": 50.0, "damage": 0.0, "opponents": opponents, "dist_raced": 1000.0}
        self.assertEqual(safety_filter(ATTACK, state), BLOCK)

    def test_dist_raced_missing_defaults_to_not_launching(self):
        # compute_control's own default for a missing dist_raced field is
        # "never launching" (1e9) — safety_filter must match that default so
        # the two don't disagree about whether the car is still on the grid.
        state = {"fuel": 50.0, "damage": 0.0, "opponents": _rear_close_opponents()}
        self.assertEqual(safety_filter(ATTACK, state), BLOCK)


class PriorityInteractionTests(unittest.TestCase):
    """Higher-priority rules must win when multiple conditions are true at once."""

    def test_pit_beats_defend_when_both_fuel_and_damage_are_critical_and_near_pit(self):
        # Priority 2's near-pit-lane PIT check runs before priority 3's
        # damage check, and is unconditional on damage -- so PIT still wins
        # over DEFEND even for a critically damaged car, as long as the car
        # is actually near a pit lane.
        state = {
            "fuel": ai_bot._FUEL_PIT - 1.0, "damage": ai_bot._DMG_DEFEND + 100.0,
            "dist_from_start": 990.0, "track_length": 2000.0,
            "pit_entry": 1000.0, "pit_exit": 1050.0,
        }
        self.assertEqual(safety_filter(ATTACK, state), PIT)

    def test_defend_wins_over_pit_downgrade_when_far_from_pit_lane(self):
        # Without pit-lane telemetry, PIT never actually engages (see
        # PriorityTwoFuelPitTests) -- a critically damaged, fuel-critical car
        # far from the pits falls through to DEFEND (protect what's left)
        # instead of PIT.
        state = {"fuel": ai_bot._FUEL_PIT - 1.0, "damage": ai_bot._DMG_DEFEND + 100.0}
        self.assertEqual(safety_filter(ATTACK, state), DEFEND)

    def test_defend_beats_no_attack_downgrade_when_damage_is_critical(self):
        state = {"fuel": 50.0, "damage": ai_bot._DMG_DEFEND + 100.0}
        # Both priority 3 (>= _DMG_DEFEND -> DEFEND) and priority 4
        # (>= _DMG_NO_ATTACK, ATTACK -> NORMAL) match; priority 3 must win.
        self.assertEqual(safety_filter(ATTACK, state), DEFEND)

    def test_no_attack_beats_block_when_car_is_damaged_and_threatened(self):
        state = {"fuel": 50.0, "damage": ai_bot._DMG_NO_ATTACK + 1.0,
                  "opponents": _rear_close_opponents(), "dist_raced": 1000.0}
        self.assertEqual(safety_filter(ATTACK, state), NORMAL)

    def test_defaults_apply_when_fuel_and_damage_are_absent(self):
        # safety_filter defaults fuel to 50.0 and damage to 0.0 when the
        # sensor dict doesn't carry them — must not crash and must behave
        # like a healthy car.
        self.assertEqual(safety_filter(ATTACK, {}), ATTACK)


if __name__ == "__main__":
    unittest.main()
