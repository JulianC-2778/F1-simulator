#!/usr/bin/env python3
"""Unit tests for tire_strategy.py (Direction 1 addition: tire wear + pit window)."""

import unittest

from tire_strategy import (
    LOW_FUEL_LAPS_CRITICAL,
    LOW_FUEL_LAPS_WARNING,
    TIRE_CRITICAL_PCT,
    TIRE_WARNING_PCT,
    RaceStrategyTracker,
    estimate_pit_window,
)


class ControlledClock:
    """Fake monotonic clock: advance() moves it forward by a fixed amount."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self) -> float:
        return self._now


def _car_state(**overrides) -> dict:
    base = {"speed": 150.0, "rpm": 6000.0, "gear": 4, "track_pos": 0.0, "damage": 0.0, "fuel": 30.0, "lap_time": 10.0}
    base.update(overrides)
    return base


class RaceStrategyTrackerWearTests(unittest.TestCase):
    def test_wear_starts_at_zero(self):
        tracker = RaceStrategyTracker(clock=ControlledClock())
        self.assertEqual(tracker.wear_pct, 0.0)

    def test_first_update_does_not_accumulate_wear_no_prior_timestamp(self):
        clock = ControlledClock()
        tracker = RaceStrategyTracker(clock=clock)
        tracker.update(_car_state())
        self.assertEqual(tracker.wear_pct, 0.0)

    def test_wear_increases_with_elapsed_time(self):
        clock = ControlledClock()
        tracker = RaceStrategyTracker(clock=clock)
        tracker.update(_car_state())
        clock.advance(100.0)
        tracker.update(_car_state())
        self.assertGreater(tracker.wear_pct, 0.0)

    def test_higher_speed_wears_tires_faster(self):
        slow = RaceStrategyTracker(clock=(slow_clock := ControlledClock()))
        fast = RaceStrategyTracker(clock=(fast_clock := ControlledClock()))
        slow.update(_car_state(speed=50.0))
        fast.update(_car_state(speed=250.0))
        slow_clock.advance(60.0)
        fast_clock.advance(60.0)
        slow.update(_car_state(speed=50.0))
        fast.update(_car_state(speed=250.0))
        self.assertGreater(fast.wear_pct, slow.wear_pct)

    def test_cornering_movement_wears_tires_faster_than_a_straight_line(self):
        straight = RaceStrategyTracker(clock=(sc := ControlledClock()))
        cornering = RaceStrategyTracker(clock=(cc := ControlledClock()))
        straight.update(_car_state(track_pos=0.0))
        cornering.update(_car_state(track_pos=0.0))
        sc.advance(10.0)
        cc.advance(10.0)
        straight.update(_car_state(track_pos=0.0))
        cornering.update(_car_state(track_pos=0.6))  # big lateral jump = hard cornering proxy
        self.assertGreater(cornering.wear_pct, straight.wear_pct)

    def test_wear_is_capped_at_100_percent(self):
        clock = ControlledClock()
        tracker = RaceStrategyTracker(clock=clock)
        tracker.update(_car_state(speed=300.0))
        clock.advance(1_000_000.0)  # absurdly long stint, must still clamp
        tracker.update(_car_state(speed=300.0))
        self.assertEqual(tracker.wear_pct, 100.0)

    def test_a_fuel_jump_resets_wear_to_zero_pit_stop_heuristic(self):
        clock = ControlledClock()
        tracker = RaceStrategyTracker(clock=clock)
        tracker.update(_car_state(fuel=5.0))
        clock.advance(500.0)
        tracker.update(_car_state(fuel=5.0))
        self.assertGreater(tracker.wear_pct, 0.0)  # some wear accumulated first

        clock.advance(1.0)
        tracker.update(_car_state(fuel=40.0))  # refuelled -- implies a pit stop
        self.assertEqual(tracker.wear_pct, 0.0)

    def test_a_small_fuel_increase_within_noise_does_not_reset_wear(self):
        # e.g. a fuel reading correction, not a real refuel.
        clock = ControlledClock()
        tracker = RaceStrategyTracker(clock=clock)
        tracker.update(_car_state(fuel=20.0))
        clock.advance(500.0)
        tracker.update(_car_state(fuel=20.0))
        wear_before = tracker.wear_pct
        self.assertGreater(wear_before, 0.0)

        # No clock advance here on purpose -- isolates "did a small fuel bump
        # reset wear" from "did more time also pass", which is covered by
        # test_wear_increases_with_elapsed_time separately.
        tracker.update(_car_state(fuel=20.5))  # +0.5L, well under the reset threshold
        self.assertEqual(tracker.wear_pct, wear_before)

    def test_manual_reset_zeroes_wear(self):
        clock = ControlledClock()
        tracker = RaceStrategyTracker(clock=clock)
        tracker.update(_car_state())
        clock.advance(500.0)
        tracker.update(_car_state())
        self.assertGreater(tracker.wear_pct, 0.0)
        tracker.reset()
        self.assertEqual(tracker.wear_pct, 0.0)


class RaceStrategyTrackerFuelPerLapTests(unittest.TestCase):
    def test_fuel_per_lap_is_unknown_until_a_lap_boundary_is_seen(self):
        clock = ControlledClock()
        tracker = RaceStrategyTracker(clock=clock)
        tracker.update(_car_state(lap_time=10.0, fuel=30.0))
        clock.advance(10.0)
        tracker.update(_car_state(lap_time=20.0, fuel=28.0))
        self.assertIsNone(tracker.fuel_per_lap)

    def test_fuel_per_lap_is_computed_when_lap_time_resets(self):
        clock = ControlledClock()
        tracker = RaceStrategyTracker(clock=clock)
        tracker.update(_car_state(lap_time=5.0, fuel=30.0))
        clock.advance(70.0)
        tracker.update(_car_state(lap_time=75.0, fuel=28.0))  # still same lap, later in it
        clock.advance(1.0)
        tracker.update(_car_state(lap_time=1.0, fuel=27.2))   # lap_time dropped -- new lap started
        self.assertAlmostEqual(tracker.fuel_per_lap, 30.0 - 27.2, places=2)

    def test_small_lap_time_jitter_does_not_look_like_a_new_lap(self):
        clock = ControlledClock()
        tracker = RaceStrategyTracker(clock=clock)
        tracker.update(_car_state(lap_time=10.0, fuel=30.0))
        clock.advance(1.0)
        tracker.update(_car_state(lap_time=9.5, fuel=29.9))  # tiny backward jitter, not a real lap reset
        self.assertIsNone(tracker.fuel_per_lap)


class EstimatePitWindowTests(unittest.TestCase):
    def test_low_wear_and_ample_fuel_does_not_recommend_a_pit(self):
        result = estimate_pit_window(_car_state(fuel=30.0), wear_pct=10.0, fuel_per_lap=2.0)
        self.assertFalse(result["recommend_pit"])
        self.assertEqual(result["urgency"], "low")
        self.assertEqual(result["reasons"], [])

    def test_tire_wear_at_warning_threshold_is_medium_urgency(self):
        result = estimate_pit_window(_car_state(fuel=30.0), wear_pct=TIRE_WARNING_PCT, fuel_per_lap=2.0)
        self.assertEqual(result["urgency"], "medium")
        self.assertIn("tires", result["reasons"])
        self.assertTrue(result["recommend_pit"])

    def test_tire_wear_at_critical_threshold_is_high_urgency(self):
        result = estimate_pit_window(_car_state(fuel=30.0), wear_pct=TIRE_CRITICAL_PCT, fuel_per_lap=2.0)
        self.assertEqual(result["urgency"], "high")
        self.assertIn("tires", result["reasons"])

    def test_unknown_fuel_per_lap_does_not_crash_or_recommend_on_fuel(self):
        result = estimate_pit_window(_car_state(fuel=1.0), wear_pct=0.0, fuel_per_lap=None)
        self.assertIsNone(result["laps_of_fuel_left"])
        self.assertEqual(result["reasons"], [])

    def test_fuel_at_critical_laps_remaining_is_high_urgency(self):
        result = estimate_pit_window(_car_state(fuel=2.0), wear_pct=0.0, fuel_per_lap=2.0)  # 1.0 lap left
        self.assertLessEqual(result["laps_of_fuel_left"], LOW_FUEL_LAPS_CRITICAL)
        self.assertEqual(result["urgency"], "high")
        self.assertIn("fuel", result["reasons"])

    def test_fuel_at_warning_laps_remaining_is_medium_urgency(self):
        result = estimate_pit_window(_car_state(fuel=5.0), wear_pct=0.0, fuel_per_lap=2.0)  # 2.5 laps left
        self.assertLessEqual(result["laps_of_fuel_left"], LOW_FUEL_LAPS_WARNING)
        self.assertEqual(result["urgency"], "medium")
        self.assertIn("fuel", result["reasons"])

    def test_critical_fuel_overrides_medium_tire_urgency_to_high(self):
        result = estimate_pit_window(_car_state(fuel=1.0), wear_pct=TIRE_WARNING_PCT, fuel_per_lap=2.0)
        self.assertEqual(result["urgency"], "high")
        self.assertIn("tires", result["reasons"])
        self.assertIn("fuel", result["reasons"])

    def test_medium_fuel_does_not_downgrade_an_already_high_tire_urgency(self):
        result = estimate_pit_window(_car_state(fuel=5.0), wear_pct=TIRE_CRITICAL_PCT, fuel_per_lap=2.0)
        self.assertEqual(result["urgency"], "high")
        self.assertIn("tires", result["reasons"])
        self.assertIn("fuel", result["reasons"])


if __name__ == "__main__":
    unittest.main()
