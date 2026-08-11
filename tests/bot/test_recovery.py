"""Off-track recovery, stabilize-after-impact, wrong-way turnaround, stuck
watchdogs, and pure-pursuit steering.

Ports the corresponding assertions from ai_bot.py's built-in ``_run_tests()``
(off-track / flung / wrong-way / no-progress / stuck sections). See
docs/bot_test_plan.md section 4.3. All of this state is module-global inside
ai_bot.py, so every test resets it via ``ai_bot._reset_driver_state()`` first.
"""

import unittest

import ai_bot
from ai_bot import ATTACK, NORMAL, _pursuit_target, compute_control


class RecoveryTestCase(unittest.TestCase):
    def setUp(self):
        ai_bot.set_track_model(None)
        ai_bot._reset_driver_state()

    def tearDown(self):
        ai_bot._reset_driver_state()

    def _state(self, **overrides):
        track = [150.0] * 9 + [180.0] + [150.0] * 9
        state = {"speed_x": 80.0, "rpm": 5000.0, "gear": 3, "angle": 0.0, "track_pos": 0.0, "track": track}
        state.update(overrides)
        return state


class OffTrackReentryTests(RecoveryTestCase):
    def test_off_track_still_moving_sheds_speed_and_steers_back_shallow(self):
        out = compute_control(self._state(track_pos=1.5), ATTACK)
        self.assertIn("(accel 0.000)", out)
        self.assertIn("(brake 0.500)", out)
        self.assertIn("(steer -0.750)", out)  # angle - 0.5*track_pos

    def test_hysteresis_holds_recovery_pace_just_back_over_the_edge(self):
        # Hysteresis is stateful across frames (module-level ``_recovering``
        # flag): entering recovery off-track, then coming back to |tpos|<1
        # without yet being "well inside", must still drive at recovery pace
        # rather than snapping straight back to full racing throttle.
        compute_control(self._state(track_pos=1.5, speed_x=80.0), ATTACK)
        out_edge = compute_control(self._state(track_pos=0.95, speed_x=40.0), ATTACK)
        self.assertIn("(accel 0.500)", out_edge, "must stay in gentle recovery pace")

    def test_hysteresis_exits_once_well_inside_and_aligned(self):
        compute_control(self._state(track_pos=1.5, speed_x=80.0), ATTACK)
        compute_control(self._state(track_pos=0.95, speed_x=40.0), ATTACK)
        out_back = compute_control(self._state(track_pos=0.2, speed_x=40.0), ATTACK)
        self.assertIn("(accel 1.000)", out_back, "must resume full racing once clearly back on track")

    def test_apex_kerb_ride_does_not_trigger_recovery(self):
        # A legitimate kerb clip (|track_pos| just past 1) must not be treated
        # as an excursion — grabbing for the centre mid-corner threw the car
        # off the outside in a prior regression.
        out = compute_control(self._state(track_pos=1.05, speed_x=120.0), NORMAL)
        self.assertNotIn("(gear -1)", out)
        self.assertIn("(accel 1.000)", out)

    def test_crawl_forward_when_off_track_slow_and_facing_forward(self):
        out = compute_control(self._state(track_pos=1.5, speed_x=2.0, angle=0.0), ATTACK)
        self.assertIn("(gear 1)", out)
        self.assertIn("(accel 0.500)", out)
        self.assertIn("(brake 0.000)", out)
        self.assertIn("(steer -0.750)", out)

    def test_turn_around_when_off_track_stopped_and_facing_away(self):
        out = compute_control(self._state(track_pos=1.5, speed_x=0.0, angle=3.0), ATTACK)
        self.assertIn("(gear -1)", out)
        self.assertIn("(accel 0.500)", out)
        self.assertIn("(steer -1.000)", out)


class StabilizeAfterImpactTests(RecoveryTestCase):
    def test_flung_far_off_track_at_speed_brakes_straight_with_no_steer(self):
        out = compute_control(self._state(track_pos=3.0, speed_x=50.0, angle=0.2), ATTACK)
        self.assertIn("(accel 0.000)", out)
        self.assertIn("(brake 0.900)", out)
        self.assertIn("(steer 0.000)", out)

    def test_once_settled_and_facing_right_way_creeps_forward_not_reverse(self):
        out = compute_control(self._state(track_pos=3.0, speed_x=5.0, angle=0.2), ATTACK)
        self.assertNotIn("(gear -1)", out)
        self.assertNotIn("(brake 0.900)", out, "must not keep braking forever once settled")

    def test_once_settled_but_facing_badly_wrong_still_reverses(self):
        out = compute_control(self._state(track_pos=3.0, speed_x=5.0, angle=3.0), ATTACK)
        self.assertIn("(gear -1)", out)


class WrongWayTurnaroundTests(RecoveryTestCase):
    def test_wrong_way_on_track_triggers_reverse_turn_not_normal_driving(self):
        state = self._state(track_pos=0.0, speed_x=0.0, angle=3.0, track=[-1.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertIn("(gear -1)", out)

    def test_wrong_way_at_speed_brakes_before_manoeuvring(self):
        state = self._state(track_pos=0.0, speed_x=80.0, angle=3.0, track=[-1.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertIn("(brake 0.800)", out)
        self.assertIn("(accel 0.000)", out)

    def test_reverse_leg_is_capped_and_forces_a_forward_leg(self):
        state = self._state(track_pos=0.0, speed_x=-10.0, angle=3.0, track=[-1.0] * 19)
        out = ""
        for _ in range(ai_bot._TA_REV_MAX_FRAMES + 1):
            out = compute_control(state, NORMAL)
        self.assertIn("(gear 1)", out)
        self.assertIn("(accel 0.400)", out)


class NoProgressWatchdogTests(RecoveryTestCase):
    def test_escalates_to_stabilize_only_after_the_full_window(self):
        state = self._state(track_pos=2.3, speed_x=0.0, angle=3.0, dist_from_start=500.0)
        for _ in range(ai_bot._NO_PROGRESS_FRAMES - 10):
            compute_control(state, NORMAL)
        self.assertNotEqual(ai_bot._dbg["mode"], "stabilize", "fired too early")
        for _ in range(20):
            compute_control(state, NORMAL)
        self.assertEqual(ai_bot._dbg["mode"], "stabilize")


class BlindSensorFallbackTests(RecoveryTestCase):
    def test_all_beams_unusable_never_floors_it_blind(self):
        out = compute_control(self._state(track=[-1.0] * 19), ATTACK)
        self.assertNotIn("(accel 1.000)", out)


class StuckReverseTests(RecoveryTestCase):
    def test_sustained_jam_on_track_triggers_reverse_burst(self):
        wall = [150.0] * 9 + [2.0] + [150.0] * 9  # nose 2m from a wall
        state = self._state(speed_x=1.0, gear=1, angle=0.1, track_pos=0.2, track=wall)
        out = ""
        for _ in range(ai_bot._STUCK_FRAMES + 1):
            out = compute_control(state, NORMAL)
        self.assertIn("(gear -1)", out)
        self.assertIn("(accel 0.500)", out)

    def test_clear_standing_start_does_not_falsely_reverse(self):
        state = self._state(speed_x=0.0, gear=1, track_pos=0.0)  # track[9]=180, clear
        out = ""
        for _ in range(ai_bot._STUCK_FRAMES + 5):
            out = compute_control(state, NORMAL)
        self.assertNotIn("(gear -1)", out)


class PurePursuitTests(unittest.TestCase):
    def test_symmetric_track_aims_straight_ahead(self):
        target = _pursuit_target([150.0] * 19)
        self.assertIsNotNone(target)
        self.assertAlmostEqual(target, 0.0, places=6, msg="a symmetric beam profile must aim dead ahead")

    def test_returns_none_when_no_beams_are_usable(self):
        self.assertIsNone(_pursuit_target([-1.0] * 19), "all-blocked beams have no usable target")
        self.assertIsNone(_pursuit_target([]))

    def test_left_leaning_track_aims_left(self):
        left_corner = [200.0] * 10 + [40.0] * 9
        target = _pursuit_target(left_corner)
        self.assertIsNotNone(target)


class CompoundCorneringTests(RecoveryTestCase):
    def test_top_gear_corner_does_not_crash_and_steers_in(self):
        left_corner = [200.0] * 10 + [40.0] * 9
        state = self._state(speed_x=250.0, gear=6, track=left_corner)
        out = compute_control(state, ATTACK)  # must not raise
        self.assertIn("(gear 6)", out)
        self.assertNotIn("(steer 0.000)", out)

    def test_symmetric_track_pure_pursuit_goes_straight(self):
        state = self._state(speed_x=200.0, gear=6, track=[200.0] * 19, track_pos=0.0)
        out = compute_control(state, NORMAL)
        self.assertIn("(steer 0.000)", out)


if __name__ == "__main__":
    unittest.main()
