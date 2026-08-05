"""Event-trigger boundary tests (T-epsilon / T / T+epsilon) for
`midware/commentary_engine.py::detect_event`.

Every threshold value and its inclusive/exclusive-ness below is read
directly from the real comparison operators in `detect_event()`
(commentary_engine.py:266-331) -- see docs/commentary_test_matrix.md
section 2 for the full table. `lap_complete` and `position_change` are
integer/inequality triggers with no continuous epsilon boundary, so they
get an on/off pair instead of a three-point boundary.

Each case uses a *fresh* CommentaryEngine seeded with one frame, then feeds
exactly one more frame two-frame window ([seed_frame, test_frame]) so
`detect_event` sees `previous=seed_frame`, `latest=test_frame`. Every
non-target condition is deliberately held constant/neutral so the priority
tie-break in detect_event's final `max()` can't accidentally swap in a
different event -- priority tie-break itself is covered in
tests/unit/test_commentary_modes.py.
"""

import unittest
from unittest.mock import patch

from midware import commentary_engine as ce_module
from midware.commentary_engine import CommentaryConfig, CommentaryEngine

EPS = 1e-6


class ControlledClock:
    """Deterministic stand-in for time.time(): advances by a fixed step per
    call, so the global wall-clock cooldown in _can_emit_event doesn't block
    a second event fired microseconds later in real time (see
    test_commentary_modes.py for the same pattern)."""

    def __init__(self, start: float = 1_000_000.0, step: float = 5.0):
        self.value = start
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def base_frame(sim_time: float, **overrides) -> dict:
    frame = {
        "sim_time": sim_time,
        "lap": 1,
        "speed_x": 40.0,   # below battle's speed>60 gate by default
        "gear": 3,
        "track_pos": 0.0,
        "damage": 0.0,
        "race_pos": 5,
        "fuel": 50.0,
        "throttle": 0.2,   # below pace_surge's throttle>0.8 gate by default
        "brake": 0.0,
        "steer": 0.0,
        "angle": 0.0,
        "rpm": 5000.0,
        "dist_from_start": sim_time * 50.0,
        "cur_lap_time": sim_time,
        "last_lap_time": 0.0,
    }
    frame.update(overrides)
    return frame


def front_gap_frame(sim_time: float, gap: float, **overrides) -> dict:
    frame = base_frame(sim_time, **overrides)
    frame["opponent_18"] = gap  # index 16-20 range feeds front_gap
    return frame


def detect(mode: str, seed: dict, test: dict, window_seconds: float = 6.0):
    engine = CommentaryEngine(CommentaryConfig(mode=mode, window_seconds=window_seconds))
    engine.next_decision([seed])
    return engine.next_decision([seed, test])


class TestContactBoundary(unittest.TestCase):
    """contact: damage_delta >= 5.0 (inclusive)."""

    def test_below_threshold_does_not_trigger(self):
        decision = detect("event", base_frame(0.0, damage=0.0), base_frame(0.5, damage=4.99))
        self.assertIsNone(decision)

    def test_at_threshold_triggers(self):
        decision = detect("event", base_frame(0.0, damage=0.0), base_frame(0.5, damage=5.0))
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "contact")

    def test_above_threshold_triggers(self):
        decision = detect("event", base_frame(0.0, damage=0.0), base_frame(0.5, damage=5.01))
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "contact")


class TestOffTrackBoundary(unittest.TestCase):
    """off_track: abs(track_pos) > 1.0 (strict) and not already off."""

    def test_below_threshold_does_not_trigger(self):
        decision = detect("event", base_frame(0.0, track_pos=0.0), base_frame(0.5, track_pos=0.999))
        self.assertIsNone(decision)

    def test_exactly_at_threshold_does_not_trigger_strict_inequality(self):
        decision = detect("event", base_frame(0.0, track_pos=0.0), base_frame(0.5, track_pos=1.0))
        self.assertIsNone(decision)

    def test_above_threshold_triggers(self):
        decision = detect("event", base_frame(0.0, track_pos=0.0), base_frame(0.5, track_pos=1.001))
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "off_track")

    def test_negative_side_reports_left(self):
        decision = detect("event", base_frame(0.0, track_pos=0.0), base_frame(0.5, track_pos=-1.5))
        self.assertIsNotNone(decision)
        self.assertIn("left", decision.event["reason"])

    def test_positive_side_reports_right(self):
        decision = detect("event", base_frame(0.0, track_pos=0.0), base_frame(0.5, track_pos=1.5))
        self.assertIsNotNone(decision)
        self.assertIn("right", decision.event["reason"])

    def test_staying_off_track_does_not_refire(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        engine.next_decision([base_frame(0.0, track_pos=0.0)])
        first = engine.next_decision([base_frame(0.0, track_pos=0.0), base_frame(0.5, track_pos=1.5)])
        self.assertIsNotNone(first)
        self.assertEqual(first.event["event_type"], "off_track")
        # Still off track on the next frame -- must not re-trigger.
        second = engine.next_decision([
            base_frame(0.5, track_pos=1.5),
            base_frame(1.0, track_pos=1.6),
        ])
        if second is not None:
            self.assertNotEqual(second.event["event_type"], "off_track")


class TestLapCompleteBoundary(unittest.TestCase):
    """lap_complete: integer lap counter increment -- on/off, no epsilon."""

    def test_same_lap_does_not_trigger(self):
        decision = detect("event", base_frame(0.0, lap=1), base_frame(0.5, lap=1))
        self.assertIsNone(decision)

    def test_lap_increment_triggers(self):
        decision = detect("event", base_frame(0.0, lap=1), base_frame(0.5, lap=2))
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "lap_complete")
        self.assertEqual(decision.event["completed_lap"], 1)


class TestPositionChangeBoundary(unittest.TestCase):
    """position_change: any race_pos inequality -- on/off, no epsilon."""

    def test_same_position_does_not_trigger(self):
        decision = detect("event", base_frame(0.0, race_pos=5), base_frame(0.5, race_pos=5))
        self.assertIsNone(decision)

    def test_position_improves_reports_up(self):
        decision = detect("event", base_frame(0.0, race_pos=5), base_frame(0.5, race_pos=4))
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "position_change")
        self.assertIn("up", decision.event["reason"])

    def test_position_worsens_reports_down(self):
        decision = detect("event", base_frame(0.0, race_pos=5), base_frame(0.5, race_pos=6))
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "position_change")
        self.assertIn("down", decision.event["reason"])


class TestBattleGapBoundary(unittest.TestCase):
    """battle: front_gap < 10.0 (strict) AND speed_x > 60.0 (strict).
    Gap sub-boundary holds speed_x fixed safely above 60."""

    def test_gap_above_threshold_does_not_trigger(self):
        decision = detect(
            "event",
            front_gap_frame(0.0, 10.01, speed_x=100.0),
            front_gap_frame(0.5, 10.01, speed_x=100.0),
        )
        self.assertIsNone(decision)

    def test_gap_exactly_at_threshold_does_not_trigger_strict_inequality(self):
        decision = detect(
            "event",
            front_gap_frame(0.0, 10.0, speed_x=100.0),
            front_gap_frame(0.5, 10.0, speed_x=100.0),
        )
        self.assertIsNone(decision)

    def test_gap_below_threshold_triggers(self):
        decision = detect(
            "event",
            front_gap_frame(0.0, 9.99, speed_x=100.0),
            front_gap_frame(0.5, 9.99, speed_x=100.0),
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "battle")


class TestBattleSpeedBoundary(unittest.TestCase):
    """battle speed sub-boundary: holds front_gap fixed safely below 10."""

    def test_speed_below_threshold_does_not_trigger(self):
        decision = detect(
            "event",
            front_gap_frame(0.0, 5.0, speed_x=59.99),
            front_gap_frame(0.5, 5.0, speed_x=59.99),
        )
        self.assertIsNone(decision)

    def test_speed_exactly_at_threshold_does_not_trigger_strict_inequality(self):
        decision = detect(
            "event",
            front_gap_frame(0.0, 5.0, speed_x=60.0),
            front_gap_frame(0.5, 5.0, speed_x=60.0),
        )
        self.assertIsNone(decision)

    def test_speed_above_threshold_triggers(self):
        decision = detect(
            "event",
            front_gap_frame(0.0, 5.0, speed_x=60.01),
            front_gap_frame(0.5, 5.0, speed_x=60.01),
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "battle")


def run_ticks(engine, frames):
    """Feed frames as consecutive (frames[i-1], frames[i]) two-frame windows,
    in order, using the SAME engine instance so pace_surge's burst-tracking
    state (pace_surge_active/_start_speed/_peak_speed) persists across
    calls the way it does against a real, continuously-polled telemetry
    stream. frames[0] only seeds state. Returns one decision (or None) per
    subsequent frame."""
    engine.next_decision([frames[0]])
    decisions = []
    for i in range(1, len(frames)):
        decisions.append(engine.next_decision([frames[i - 1], frames[i]]))
    return decisions


class TestPaceSurgeBurstAccumulation(unittest.TestCase):
    """pace_surge is tracked as a continuous burst, not a per-tick delta
    check: PACE_SURGE_MIN_DELTA_KMH (20.0, strict) applies to the TOTAL
    gain across the whole burst (start speed to peak speed), and exactly
    one event is reported once the burst ends (throttle drops or speed
    stops climbing) -- not once per detection tick. Found via a real
    driving session where a single ~20s start-line acceleration produced
    14 separate pace_surge events; see docs/commentary_test_matrix.md."""

    def test_no_event_while_still_accelerating(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frames = [
            base_frame(0.0, speed_x=0.0, throttle=0.9),
            base_frame(0.5, speed_x=10.0, throttle=0.9),
            base_frame(1.0, speed_x=20.0, throttle=0.9),
            base_frame(1.5, speed_x=30.0, throttle=0.9),
        ]
        decisions = run_ticks(engine, frames)
        self.assertTrue(all(d is None for d in decisions))
        self.assertTrue(engine.pace_surge_active)

    def test_burst_reports_once_when_it_ends_with_full_start_to_peak_range(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frames = [
            base_frame(0.0, speed_x=0.0, throttle=0.9),
            base_frame(0.5, speed_x=10.0, throttle=0.9),
            base_frame(1.0, speed_x=20.0, throttle=0.9),
            base_frame(1.5, speed_x=30.0, throttle=0.9),
            base_frame(2.0, speed_x=30.0, throttle=0.0),  # lifts off -- burst ends
        ]
        decisions = run_ticks(engine, frames)
        self.assertEqual(decisions[:-1], [None, None, None])
        decision = decisions[-1]
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "pace_surge")
        self.assertIn("0.0", decision.event["reason"])
        self.assertIn("30.0", decision.event["reason"])
        self.assertFalse(engine.pace_surge_active)

    def test_total_gain_at_or_below_threshold_does_not_report(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frames = [
            base_frame(0.0, speed_x=0.0, throttle=0.9),
            base_frame(0.5, speed_x=20.0, throttle=0.9),  # total gain == 20.0, not > 20.0
            base_frame(1.0, speed_x=20.0, throttle=0.0),  # burst ends
        ]
        decisions = run_ticks(engine, frames)
        self.assertTrue(all(d is None for d in decisions))

    def test_total_gain_above_threshold_reports(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frames = [
            base_frame(0.0, speed_x=0.0, throttle=0.9),
            base_frame(0.5, speed_x=20.01, throttle=0.9),
            base_frame(1.0, speed_x=20.01, throttle=0.0),
        ]
        decisions = run_ticks(engine, frames)
        self.assertIsNotNone(decisions[-1])
        self.assertEqual(decisions[-1].event["event_type"], "pace_surge")

    def test_speed_plateauing_without_throttle_drop_also_ends_the_burst(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frames = [
            base_frame(0.0, speed_x=0.0, throttle=0.9),
            base_frame(0.5, speed_x=30.0, throttle=0.9),
            base_frame(1.0, speed_x=30.0, throttle=0.9),  # speed stops climbing, throttle still down
        ]
        decisions = run_ticks(engine, frames)
        self.assertIsNotNone(decisions[-1])
        self.assertEqual(decisions[-1].event["event_type"], "pace_surge")

    def test_state_resets_so_a_second_independent_burst_can_report(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frames = [
            base_frame(0.0, speed_x=0.0, throttle=0.9),
            base_frame(0.5, speed_x=30.0, throttle=0.9),
            base_frame(1.0, speed_x=30.0, throttle=0.0),   # first burst ends: 0 -> 30
            base_frame(1.5, speed_x=30.0, throttle=0.9),
            base_frame(2.0, speed_x=60.0, throttle=0.9),
            base_frame(2.5, speed_x=60.0, throttle=0.0),   # second burst ends: 30 -> 60
        ]
        # The two burst-end ticks happen microseconds apart in real
        # wall-clock time within this test; without a controlled clock the
        # global 1s wall-clock cooldown in _can_emit_event would swallow
        # the second, genuinely-distinct event.
        with patch.object(ce_module.time, "time", ControlledClock()):
            decisions = run_ticks(engine, frames)
        surges = [d for d in decisions if d is not None]
        self.assertEqual(len(surges), 2)
        self.assertIn("0.0", surges[0].event["reason"])
        self.assertIn("30.0", surges[0].event["reason"])
        self.assertIn("30.0", surges[1].event["reason"])
        self.assertIn("60.0", surges[1].event["reason"])


class TestPaceSurgeThrottleGate(unittest.TestCase):
    """A tick only counts as 'still accelerating' with throttle > 0.8 (strict)."""

    def test_throttle_at_or_below_threshold_never_starts_a_burst(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frames = [
            base_frame(0.0, speed_x=0.0, throttle=0.8),
            base_frame(0.5, speed_x=30.0, throttle=0.8),
        ]
        run_ticks(engine, frames)
        self.assertFalse(engine.pace_surge_active)

    def test_throttle_above_threshold_starts_a_burst(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frames = [
            base_frame(0.0, speed_x=0.0, throttle=0.81),
            base_frame(0.5, speed_x=30.0, throttle=0.81),
        ]
        run_ticks(engine, frames)
        self.assertTrue(engine.pace_surge_active)


class TestPaceSurgeRequiresNonNegativeSpeed(unittest.TestCase):
    """Regression test for a real bug found during work-package-B testing:
    a car flung backward by a hard collision could swing from e.g. -102.2
    to -46.4 km/h -- a real 55.8 km/h numeric increase with throttle
    pinned, but not an intentional acceleration. Both endpoints of every
    tick must be non-negative (genuine forward speed) for that tick to
    count toward a burst at all."""

    def test_negative_to_negative_never_starts_a_burst_even_with_large_delta(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frames = [
            base_frame(0.0, speed_x=-102.2, throttle=0.9),
            base_frame(0.5, speed_x=-46.4, throttle=0.9),
            base_frame(1.0, speed_x=-46.4, throttle=0.0),
        ]
        decisions = run_ticks(engine, frames)
        self.assertTrue(all(d is None for d in decisions))
        self.assertFalse(engine.pace_surge_active)

    def test_negative_to_positive_does_not_start_a_burst(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frames = [
            base_frame(0.0, speed_x=-10.0, throttle=0.9),
            base_frame(0.5, speed_x=30.0, throttle=0.9),
        ]
        run_ticks(engine, frames)
        self.assertFalse(engine.pace_surge_active)

    def test_positive_to_positive_still_works(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frames = [
            base_frame(0.0, speed_x=0.0, throttle=0.9),
            base_frame(0.5, speed_x=25.0, throttle=0.9),
            base_frame(1.0, speed_x=25.0, throttle=0.0),
        ]
        decisions = run_ticks(engine, frames)
        self.assertIsNotNone(decisions[-1])
        self.assertEqual(decisions[-1].event["event_type"], "pace_surge")


class TestPaceUpdateIntervalBoundary(unittest.TestCase):
    """pace_update: sim_time - last_commentary_sim_time >= baseline_interval
    (default 10.0, inclusive). Uses mode="hybrid" (mode="event" excludes
    pace_update entirely -- that gating is covered in
    test_commentary_modes.py) and a wide window_seconds so both the seed and
    test frame, ~10s apart, survive select_recent_frames' window filter."""

    def test_below_interval_does_not_trigger(self):
        decision = detect("hybrid", base_frame(0.0), base_frame(9.99), window_seconds=20.0)
        self.assertIsNone(decision)

    def test_at_interval_triggers(self):
        decision = detect("hybrid", base_frame(0.0), base_frame(10.0), window_seconds=20.0)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "pace_update")

    def test_above_interval_triggers(self):
        decision = detect("hybrid", base_frame(0.0), base_frame(10.01), window_seconds=20.0)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "pace_update")


if __name__ == "__main__":
    unittest.main()
