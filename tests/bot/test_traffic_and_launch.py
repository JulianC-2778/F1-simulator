"""Side-traffic avoidance, standing-start launch handling, front-opponent
following/overtake line bias, and BLOCK's position-defence steering bias.

Ports the "---- side-traffic avoidance ----" / "---- start-of-race caution
----" / "---- front-opponent following/overtake ----" / "---- BLOCK:
position-defence ----" sections of ai_bot.py's built-in _run_tests() — the
largest remaining gap noted in docs/bot_test_matrix.md section 6. These are
tuning-heavy regressions (each one documents a specific live-driving
incident in ai_bot.py's own comments), so test names/docstrings here quote
the original incident rather than re-deriving the numbers independently.
"""

import re
import unittest

import ai_bot
from ai_bot import BLOCK, NORMAL, _TRACK_MODEL_AVAILABLE, compute_control


def _cs(**overrides):
    track = [150.0] * 9 + [180.0] + [150.0] * 9
    state = {"speed_x": 80.0, "rpm": 5000.0, "gear": 3, "angle": 0.0, "track_pos": 0.0, "track": track}
    state.update(overrides)
    return state


def _steer_of(ctrl: str) -> float:
    m = re.search(r"\(steer ([-0-9.]+)\)", ctrl)
    assert m, ctrl
    return float(m.group(1))


def _brake_of(ctrl: str) -> float:
    m = re.search(r"\(brake ([-0-9.]+)\)", ctrl)
    assert m, ctrl
    return float(m.group(1))


class TrafficTestCase(unittest.TestCase):
    def setUp(self):
        ai_bot.set_track_model(None)
        ai_bot._reset_driver_state()

    def tearDown(self):
        ai_bot._reset_driver_state()


class SideTrafficAvoidanceTests(TrafficTestCase):
    def test_tight_car_on_the_right_steers_left_to_clear_it(self):
        opponents = [200.0] * 36
        opponents[22] = 3.0  # well inside _AVOID_RIGHT
        out = compute_control(_cs(speed_x=80.0, opponents=opponents), NORMAL)
        self.assertGreater(_steer_of(out), 0.0)

    def test_tight_car_on_the_left_steers_right_to_clear_it(self):
        opponents = [200.0] * 36
        opponents[13] = 3.0  # well inside _AVOID_LEFT
        out = compute_control(_cs(speed_x=80.0, opponents=opponents), NORMAL)
        self.assertLess(_steer_of(out), 0.0)

    def test_dead_ahead_opponent_does_not_fall_into_a_blind_gap(self):
        # Regression: index 18 (0 deg, dead ahead) used to fall between the
        # left/right avoidance windows and read as clear on both sides.
        opponents = [200.0] * 36
        opponents[18] = 3.0
        out = compute_control(_cs(speed_x=80.0, opponents=opponents), NORMAL)
        self.assertGreater(abs(_steer_of(out)), 0.02)

    def test_convergence_gate_a_genuinely_closing_gap_keeps_avoid_near_full_strength(self):
        opponents = [200.0] * 36
        for i in range(60):
            opponents[13] = 14.0 - i * 0.1  # 14m -> ~8m, steadily closing
            compute_control(_cs(speed_x=100.0, track_pos=0.0, track=[200.0] * 19, opponents=opponents), NORMAL)
        self.assertGreater(abs(ai_bot._avoid_lp), 0.06)

    def test_convergence_gate_a_stable_non_closing_gap_is_pulled_toward_a_reduced_floor(self):
        opponents_converging = [200.0] * 36
        for i in range(60):
            opponents_converging[13] = 14.0 - i * 0.1
            compute_control(_cs(speed_x=100.0, track_pos=0.0, track=[200.0] * 19, opponents=opponents_converging), NORMAL)
        avoid_converging = ai_bot._avoid_lp
        ai_bot._reset_driver_state()

        opponents_stable = [200.0] * 36
        opponents_stable[13] = 8.0
        for _ in range(60):
            compute_control(_cs(speed_x=100.0, track_pos=0.0, track=[200.0] * 19, opponents=opponents_stable), NORMAL)
        avoid_stable = ai_bot._avoid_lp

        self.assertLess(abs(avoid_stable), abs(avoid_converging) * 0.6)

    def test_room_taper_pushes_less_once_already_near_the_edge(self):
        # Live incident: a persistent 6-9 m side gap that never closed or
        # opened let avoid+barrier settle into a rub AT the track edge
        # instead of resolving.
        opponents = [200.0] * 36
        opponents[13] = 6.0  # left_gap = 6 m, well inside _AVOID_DIST
        for _ in range(60):
            compute_control(_cs(speed_x=100.0, track_pos=0.0, track=[200.0] * 19, opponents=opponents), NORMAL)
        avoid_centre = ai_bot._avoid_lp
        ai_bot._reset_driver_state()

        for _ in range(60):
            compute_control(_cs(speed_x=100.0, track_pos=-0.95, track=[200.0] * 19, opponents=opponents), NORMAL)
        avoid_edge = ai_bot._avoid_lp

        self.assertLess(avoid_centre, -0.05, "avoid should push meaningfully with room to spare")
        self.assertLess(abs(avoid_edge), abs(avoid_centre) * 0.5)

    def test_standoff_breaker_escalates_after_sustained_closeness(self):
        opponents = [200.0] * 36
        opponents[13] = 8.0  # steady 8m gap, inside _AVOID_DIST
        state = _cs(speed_x=150.0, dist_raced=1000.0, opponents=opponents)
        for _ in range(int(ai_bot._STANDOFF_TIME / ai_bot._TICK_S) - 10):
            compute_control(state, NORMAL)
        self.assertEqual(ai_bot._dbg["why"], "side-close", "should still be the passive ease before the timer expires")
        for _ in range(20):
            compute_control(state, NORMAL)
        self.assertEqual(ai_bot._dbg["why"], "standoff-yield")


class StartOfRaceCautionTests(TrafficTestCase):
    def test_15m_gap_is_fine_once_racing_normally(self):
        opponents = [200.0] * 36
        opponents[22] = 15.0
        out = compute_control(_cs(speed_x=80.0, opponents=opponents, dist_raced=1000.0), NORMAL)
        self.assertEqual(_steer_of(out), 0.0)

    def test_same_15m_gap_triggers_avoidance_during_the_launch(self):
        # The whole grid launches together, closer than any point in open
        # racing -- the wider _START_AVOID_DIST must catch what normal
        # racing would ignore.
        opponents = [200.0] * 36
        opponents[22] = 15.0
        out = compute_control(_cs(speed_x=80.0, opponents=opponents, dist_raced=50.0), NORMAL)
        self.assertGreater(_steer_of(out), 0.0)

    def test_launch_throttle_is_not_capped(self):
        out = compute_control(_cs(speed_x=80.0, dist_raced=50.0), NORMAL)
        self.assertIn("(accel 1.000)", out)

    def test_clutch_starts_near_full_slip_the_instant_first_gear_connects_during_launch(self):
        state = _cs(speed_x=0.0, gear=1, rpm=9500.0, dist_raced=10.0)
        out = compute_control(state, NORMAL)
        m = re.search(r"\(clutch ([-0-9.]+)\)", out)
        self.assertIsNotNone(m)
        self.assertGreater(float(m.group(1)), 0.9)

    def test_clutch_is_fully_engaged_once_the_ramp_finishes(self):
        state = _cs(speed_x=0.0, gear=1, rpm=9500.0, dist_raced=10.0)
        out = ""
        for _ in range(int(ai_bot._CLUTCH_RAMP_TIME / ai_bot._TICK_S) + 5):
            out = compute_control(state, NORMAL)
        self.assertIn("(clutch 0.000)", out)

    def test_clutch_ramp_does_not_apply_past_first_gear(self):
        out = compute_control(_cs(speed_x=40.0, gear=2, dist_raced=10.0), NORMAL)
        self.assertIn("(clutch 0.000)", out)

    def test_clutch_ramp_is_scoped_to_the_launch_window(self):
        # A mid-race 1st-gear hairpin exit must not feather the clutch.
        out = compute_control(_cs(speed_x=0.0, gear=1, dist_raced=1000.0), NORMAL)
        self.assertIn("(clutch 0.000)", out)


class FrontOpponentOvertakeTests(TrafficTestCase):
    def test_closing_on_a_slower_car_with_the_left_open_eases_left(self):
        out = ""
        for i in range(60):
            opponents = [200.0] * 36
            opponents[18] = max(25.0, 40.0 - i * 0.25)  # 40m -> 25m, closing
            opponents[22] = 15.0  # right congested (not tight enough for _AVOID_DIST)
            out = compute_control(_cs(speed_x=80.0, opponents=opponents), NORMAL)
        self.assertGreater(_steer_of(out), 0.05)

    def test_closing_on_a_slower_car_with_the_right_open_eases_right(self):
        out = ""
        for i in range(60):
            opponents = [200.0] * 36
            opponents[18] = max(25.0, 40.0 - i * 0.25)
            opponents[13] = 15.0  # left congested
            out = compute_control(_cs(speed_x=80.0, opponents=opponents), NORMAL)
        self.assertLess(_steer_of(out), -0.05)

    def test_matched_pace_non_closing_gap_does_not_trigger_the_overtake_bias(self):
        opponents = [200.0] * 36
        opponents[18] = 25.0
        opponents[22] = 15.0
        out = ""
        for _ in range(60):
            out = compute_control(_cs(speed_x=80.0, opponents=opponents), NORMAL)
        self.assertLess(abs(_steer_of(out)), 0.05)

    def test_cone_boundary_jump_does_not_read_as_a_real_closing_spike(self):
        # Live capture: a car's bearing crossing the _FRONT_CONE boundary
        # makes front_gap jump discontinuously even though true distance
        # barely changed (23.7m -> 7.5m in one tick while a different
        # sensor window on the same car only moved 7.3m -> 7.5m).
        opponents = [200.0] * 36
        opponents[18] = 24.0  # first tick: nothing yet inside the front cone
        compute_control(_cs(speed_x=80.0, opponents=opponents), NORMAL)
        opponents[18] = 7.5  # next tick: same nearby car now inside it
        compute_control(_cs(speed_x=80.0, opponents=opponents), NORMAL)
        self.assertLess(abs(ai_bot._dbg["close_rate"]), 5.0)

    @unittest.skipUnless(_TRACK_MODEL_AVAILABLE, "track_model.py not importable in this environment")
    def test_ambiguous_room_biases_toward_the_next_known_corner_direction(self):
        # Borrowed from TORCS's built-in "bt" robot: when neither side reads
        # clearly roomier, commit to the inside of the next known corner
        # instead of sitting neutral.
        from track_model import Segment, TrackModel

        tm = TrackModel(
            [Segment("str", 350.0, 0.0, 0.0), Segment("lft", 60.0, 40.0, 40.0), Segment("str", 400.0, 0.0, 0.0)],
            width=12.0, name="tiebreak-map",
        )
        ai_bot.set_track_model(tm)
        ai_bot._reset_driver_state()
        try:
            out = ""
            for i in range(60):
                opponents = [200.0] * 36
                opponents[18] = max(25.0, 40.0 - i * 0.25)  # closing, dead ahead
                out = compute_control(_cs(speed_x=80.0, dist_from_start=0.0, opponents=opponents), NORMAL)
            self.assertGreater(_steer_of(out), 0.05, "ambiguous room + left-hander ahead must bias left")
        finally:
            ai_bot.set_track_model(None)
            ai_bot._reset_driver_state()


class BlockPositionDefenceTests(TrafficTestCase):
    def test_threat_on_the_left_eases_left_to_hold_the_line(self):
        opponents = [200.0] * 36
        opponents[13] = 18.0  # outside _AVOID_DIST so collision-avoidance doesn't cancel the bias
        state = _cs(speed_x=80.0, opponents=opponents)
        out = ""
        for _ in range(40):
            out = compute_control(state, BLOCK)
        self.assertGreater(_steer_of(out), 0.02)

    def test_threat_on_the_right_eases_right_to_hold_the_line(self):
        opponents = [200.0] * 36
        opponents[22] = 18.0
        state = _cs(speed_x=80.0, opponents=opponents)
        out = ""
        for _ in range(40):
            out = compute_control(state, BLOCK)
        self.assertLess(_steer_of(out), -0.02)

    def test_block_bias_does_not_leak_into_other_strategies(self):
        opponents = [200.0] * 36
        opponents[13] = 18.0
        state = _cs(speed_x=80.0, opponents=opponents)
        out = ""
        for _ in range(40):
            out = compute_control(state, NORMAL)
        self.assertLess(abs(_steer_of(out)), 0.05)


class FollowCapTests(TrafficTestCase):
    def test_boxed_in_with_a_tight_gap_ahead_brakes_hard(self):
        opponents = [200.0] * 36
        opponents[18] = 5.0
        opponents[13] = 8.0
        opponents[22] = 8.0
        out = compute_control(_cs(speed_x=200.0, opponents=opponents), NORMAL)
        self.assertEqual(ai_bot._dbg["opp_bound"], 1.0)
        self.assertGreater(_brake_of(out), 0.5)

    def test_same_tight_gap_with_an_open_side_does_not_brake(self):
        # Regression that shipped first: braking here cost whole seconds a
        # lap in ordinary traffic because a side was almost always open.
        opponents = [200.0] * 36
        opponents[18] = 5.0
        out = compute_control(_cs(speed_x=200.0, opponents=opponents), NORMAL)
        self.assertEqual(ai_bot._dbg["opp_bound"], 0.0)
        self.assertLess(_brake_of(out), 0.1)

    def test_25m_gap_does_not_trigger_the_follow_cap(self):
        opponents = [200.0] * 36
        opponents[18] = 25.0
        compute_control(_cs(speed_x=200.0, opponents=opponents), NORMAL)
        self.assertEqual(ai_bot._dbg["opp_bound"], 0.0)


if __name__ == "__main__":
    unittest.main()
