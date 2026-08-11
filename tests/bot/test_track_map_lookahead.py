"""Pre-race track-map lookahead: compute_control's optional integration with
track_model.TrackModel (braking cap ahead of a mapped corner, A+ entry-line
bias, brake-point mode, and the 5-gate "trust" system that lets the map
cancel a false sensor-only brake reaction).

Ports the "---- P1: pre-race map lookahead ----" section of ai_bot.py's
built-in _run_tests(). This is the part of docs/bot_test_plan.md section 6
("P1 pre-race map lookahead ... not done") that was still open — see
docs/bot_test_matrix.md section 6.

Skipped entirely (matching the self-test's own behaviour) if track_model.py
isn't importable in this environment.
"""

import re
import unittest

import ai_bot
from ai_bot import NORMAL, _TRACK_MODEL_AVAILABLE, compute_control

if _TRACK_MODEL_AVAILABLE:
    from track_model import Segment, TrackModel


def _clear_straight_state(**overrides):
    track = [150.0] * 9 + [180.0] + [150.0] * 9
    state = {"speed_x": 80.0, "rpm": 5000.0, "gear": 3, "angle": 0.0, "track_pos": 0.0, "track": track}
    state.update(overrides)
    return state


@unittest.skipUnless(_TRACK_MODEL_AVAILABLE, "track_model.py not importable in this environment")
class TrackMapLookaheadTests(unittest.TestCase):
    def setUp(self):
        self.tm = TrackModel(
            [Segment("str", 600.0, 0.0, 0.0),
             Segment("rgt", ai_bot.math.pi * 30.0, 30.0, 30.0),
             Segment("str", 400.0, 0.0, 0.0)],
            width=12.0, name="unit-map",
        )
        ai_bot.set_track_model(self.tm)
        ai_bot._reset_driver_state()

    def tearDown(self):
        self.tm.real_lap = None
        ai_bot.set_track_model(None)
        ai_bot._reset_driver_state()

    def test_map_cap_brakes_ahead_of_a_hairpin_the_sensors_cannot_see_yet(self):
        # Sensors say clear straight, but the map knows a hairpin starts in
        # 20 m -- the map cap must beat the reactive (sensor-only) target and
        # lift/brake the car even though nothing looks wrong to the sensors.
        state = _clear_straight_state(speed_x=200.0, gear=6, dist_from_start=580.0, track=[200.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertIn("(accel 0.000)", out)
        self.assertNotIn("(brake 0.000)", out)

    def test_map_does_not_interfere_far_from_any_corner(self):
        state = _clear_straight_state(speed_x=80.0, dist_from_start=100.0, track=[200.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertIn("(accel 1.000)", out)

    def test_missing_dist_from_start_silently_skips_the_map(self):
        state = _clear_straight_state(speed_x=80.0)
        out = compute_control(state, NORMAL)
        self.assertIn("(accel 1.000)", out)

    def test_entry_line_bias_steers_toward_the_outside_before_a_mapped_corner(self):
        # A right-hander 150 m ahead (inside the entry zone) -> drift LEFT
        # (positive steer) to take the entry from the outside. The setpoint
        # is slew-limited, so it needs repeated frames to build up.
        state = _clear_straight_state(speed_x=80.0, dist_from_start=450.0, track=[200.0] * 19)
        out = ""
        for _ in range(40):
            out = compute_control(state, NORMAL)
        m = re.search(r"\(steer ([-0-9.]+)\)", out)
        self.assertIsNotNone(m)
        self.assertGreater(float(m.group(1)), 0.05)

    def test_brake_point_mode_holds_full_throttle_up_to_the_curve(self):
        state = _clear_straight_state(speed_x=200.0, gear=5, dist_from_start=450.0, track=[200.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertIn("(accel 1.000)", out)

    def test_brake_point_mode_has_a_small_neutral_gap_just_below_the_curve(self):
        # No sawtooth: just below the braking curve sits a small neutral gap
        # (neither throttle nor brake), rather than flip-flopping every frame.
        state = _clear_straight_state(speed_x=250.0, gear=5, dist_from_start=450.0, track=[200.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertIn("(accel 0.000)", out)
        self.assertIn("(brake 0.000)", out)

    def test_trust_gate_1_uncalibrated_map_is_not_trusted(self):
        state = _clear_straight_state(speed_x=200.0, gear=5, dist_from_start=100.0, track=[150.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertEqual(ai_bot._dbg["trust"], 0.0)
        self.assertNotIn("(accel 1.000)", out, "an uncalibrated map must not cancel the sensor-based lift")

    def test_trust_cancels_a_false_sensor_lift_once_all_gates_pass(self):
        state = _clear_straight_state(speed_x=200.0, gear=5, dist_from_start=100.0, track=[150.0] * 19)
        self.tm.calibrate(self.tm.lap_length)  # simulate a completed practice lap
        ai_bot._reset_driver_state()
        out = compute_control(state, NORMAL)
        self.assertEqual(ai_bot._dbg["trust"], 1.0)
        self.assertIn("(accel 1.000)", out)

    def test_trust_gate_3_nearby_traffic_vetoes_trust(self):
        self.tm.calibrate(self.tm.lap_length)
        opponents_ahead = [200.0] * 36
        opponents_ahead[17] = 50.0  # a car 50 m ahead: the map can't see traffic
        state = _clear_straight_state(speed_x=200.0, gear=5, dist_from_start=100.0,
                                       track=[150.0] * 19, opponents=opponents_ahead)
        compute_control(state, NORMAL)
        self.assertEqual(ai_bot._dbg["trust"], 0.0)

    def test_trust_gate_5_slow_corners_are_never_trusted(self):
        # The map must still bind min() and brake for a genuine slow corner
        # even where the other trust gates would otherwise pass.
        self.tm.calibrate(self.tm.lap_length)
        state = _clear_straight_state(speed_x=200.0, gear=5, dist_from_start=580.0, track=[200.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertEqual(ai_bot._dbg["trust"], 0.0)
        self.assertNotIn("(brake 0.000)", out)


if __name__ == "__main__":
    unittest.main()
