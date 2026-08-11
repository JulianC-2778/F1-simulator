"""Low-level control: gear shifting, ABS/TCL, brake distance, compute_control
core driving behaviour (straights, cornering, cruise).

Ports the assertions from ai_bot.py's built-in ``_run_tests()`` (the
"---- compute_control ----" / "_gear_shift" / "_brake_dist" sections, up to
but not including the off-track/recovery and map-lookahead sections, which
live in test_recovery.py and are out of scope respectively — see
docs/bot_test_plan.md section 4.2). ``compute_control`` and the recovery
helpers share module-level mutable state (``ai_bot._reset_driver_state()``),
so every test that drives the control loop resets it first to stay
independent of execution order.
"""

import unittest

import ai_bot
from ai_bot import (
    ATTACK,
    NORMAL,
    PIT,
    SAVE_FUEL,
    _apply_abs,
    _apply_tcl,
    _auto_gear,
    _brake_dist,
    _gear_from_speed,
    _gear_shift,
    _simple_autopilot,
    compute_control,
)


def _wheel_vel(speed_kmh: float) -> list:
    return [speed_kmh / 3.6 / ai_bot._WHEEL_RADIUS] * 4


class BotTestCase(unittest.TestCase):
    """Common setUp: fresh module-level driver state, no track model."""

    def setUp(self):
        ai_bot.set_track_model(None)
        ai_bot._reset_driver_state()

    def tearDown(self):
        ai_bot._reset_driver_state()


class AutoGearTests(unittest.TestCase):
    def test_upshifts_past_up_rpm(self):
        self.assertEqual(_auto_gear(3, 8000.0), 4)

    def test_downshifts_below_down_rpm(self):
        self.assertEqual(_auto_gear(3, 2500.0), 2)

    def test_holds_gear_in_the_middle_band(self):
        self.assertEqual(_auto_gear(3, 5000.0), 3)

    def test_neutral_or_reverse_forces_first_gear(self):
        self.assertEqual(_auto_gear(0, 5000.0), 1)
        self.assertEqual(_auto_gear(-1, 5000.0), 1)


class GearFromSpeedTests(unittest.TestCase):
    def test_top_gear_does_not_index_error(self):
        # Regression: _DOWN_SPEED[6] was missing and raised IndexError once
        # the car reached 6th gear, killing the whole drive loop.
        self.assertEqual(_gear_from_speed(6, 250.0), 6)
        self.assertEqual(_gear_from_speed(6, 100.0), 5)

    def test_upshift_into_top_gear(self):
        self.assertEqual(_gear_from_speed(5, 200.0), 6)

    def test_out_of_range_gear_does_not_crash(self):
        self.assertEqual(_gear_from_speed(7, 250.0), 7)

    def test_neutral_forces_first_gear(self):
        self.assertEqual(_gear_from_speed(0, 100.0), 1)


class GearShiftTests(unittest.TestCase):
    """rpm-first shifting with a speed-guarded downshift (anti-hunting)."""

    def test_high_rpm_upshifts(self):
        self.assertEqual(_gear_shift(3, 9000.0, 100.0), 4)

    def test_low_rpm_at_low_speed_downshifts(self):
        self.assertEqual(_gear_shift(3, 3000.0, 60.0), 2)

    def test_mid_rpm_holds_gear(self):
        self.assertEqual(_gear_shift(3, 5000.0, 100.0), 3)

    def test_speed_guard_blocks_downshift_the_lower_gear_cannot_carry(self):
        # A stale low-rpm reading at a road speed the lower gear can't carry
        # must not downshift — this used to flap 1st<->2nd and strangle the
        # launch.
        self.assertEqual(_gear_shift(2, 3400.0, 51.0), 2)
        self.assertEqual(_gear_shift(3, 3000.0, 100.0), 3)

    def test_no_upshift_past_top_gear(self):
        self.assertEqual(_gear_shift(6, 9500.0, 300.0), 6)

    def test_zero_rpm_falls_back_to_speed_table(self):
        self.assertEqual(_gear_shift(2, 0.0, 100.0), 3)

    def test_rpm_up_boundary(self):
        self.assertEqual(_gear_shift(1, 8600.0, 66.0), 2, "past _RPM_UP must upshift")
        self.assertEqual(_gear_shift(1, 8200.0, 66.0), 1, "below _RPM_UP must hold")

    def test_post_upshift_sag_does_not_bounce_back(self):
        # Regression: right after a 1->2 upshift revs sag briefly to
        # ~4000-4800; this must hold 2nd, not immediately drop back to 1st.
        self.assertEqual(_gear_shift(2, 4045.0, 56.0), 2)
        self.assertEqual(_gear_shift(2, 3800.0, 56.0), 2)


class AbsTclTests(unittest.TestCase):
    def test_abs_leaves_brake_alone_below_slip_threshold(self):
        # wheels turning at exactly road speed -> no slip -> no reduction
        wheels = _wheel_vel(100.0)
        self.assertEqual(_apply_abs(0.8, 100.0, wheels), 0.8)

    def test_abs_reduces_brake_when_wheels_locking(self):
        locked = [0.0, 0.0, 0.0, 0.0]
        reduced = _apply_abs(0.8, 100.0, locked)
        self.assertLess(reduced, 0.8)

    def test_abs_ignores_low_speed(self):
        # speed_ms < 3.0 m/s (~10.8 km/h): ABS must not touch the brake.
        self.assertEqual(_apply_abs(0.8, 5.0, [0.0, 0.0, 0.0, 0.0]), 0.8)

    def test_tcl_reduces_throttle_on_wheelspin(self):
        spinning = [200.0, 200.0, 200.0, 200.0]  # far faster than road speed
        reduced = _apply_tcl(1.0, 50.0, spinning)
        self.assertLess(reduced, 1.0)

    def test_tcl_needs_four_wheel_readings(self):
        self.assertEqual(_apply_tcl(1.0, 50.0, [0.0, 0.0]), 1.0)


class BrakeDistTests(unittest.TestCase):
    def test_no_distance_needed_when_not_decelerating(self):
        self.assertEqual(_brake_dist(100.0, 150.0, 1200.0), 0.0)
        self.assertEqual(_brake_dist(100.0, 100.0, 1200.0), 0.0)

    def test_sanity_range_for_a_known_deceleration(self):
        # 200 -> 80 km/h at 1200 kg (car1-trb1 + 50 L fuel): hand-computed
        # from the same closed-form formula, roughly 80-100 m at _BRAKE_MU=1.0.
        d = _brake_dist(200.0, 80.0, 1200.0)
        self.assertTrue(80.0 < d < 100.0, f"got {d}")

    def test_distance_grows_with_the_speed_drop(self):
        self.assertGreater(_brake_dist(200.0, 40.0, 1200.0), _brake_dist(200.0, 80.0, 1200.0))


class SimpleAutopilotTests(unittest.TestCase):
    def test_full_throttle_on_a_clear_track(self):
        track = [150.0] * 9 + [180.0] + [150.0] * 9
        state = {"speed_x": 80.0, "rpm": 5000.0, "gear": 3, "angle": 0.1, "track_pos": 0.2, "track": track}
        out = _simple_autopilot(state)
        self.assertIn("(accel 1.000)", out)
        self.assertIn("(gear 3)", out)


class ComputeControlStraightLineTests(BotTestCase):
    def _clear_straight_state(self, **overrides):
        track = [150.0] * 9 + [180.0] + [150.0] * 9
        state = {"speed_x": 80.0, "rpm": 5000.0, "gear": 3, "angle": 0.0, "track_pos": 0.0, "track": track}
        state.update(overrides)
        return state

    def test_each_strategy_pushes_full_throttle_on_a_clear_straight(self):
        state = self._clear_straight_state()
        self.assertIn("(accel 1.000)", compute_control(state, ATTACK))
        self.assertIn("(accel 1.000)", compute_control(state, NORMAL))

    def test_save_fuel_caps_throttle_below_full(self):
        state = self._clear_straight_state()
        out = compute_control(state, SAVE_FUEL)
        self.assertIn("(accel 0.650)", out)

    def test_arriving_hot_at_a_corner_lifts_and_brakes(self):
        state = self._clear_straight_state(speed_x=250.0, track=[60.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertIn("(accel 0.000)", out)
        self.assertNotIn("(brake 0.000)", out)

    def test_cruising_at_the_cap_uses_steady_partial_throttle(self):
        # Regression: the old bang-bang controller (full below target, zero
        # above) tapped the pedal the whole straight instead of holding a
        # steady partial throttle at the speed cap.
        state = self._clear_straight_state(speed_x=245.0, gear=6, track=[200.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertIn("(brake 0.000)", out)
        self.assertIn("(accel 0.833)", out)

    def test_mid_straight_pace_keeps_pushing(self):
        # Regression: an earlier zero-endpoint braking curve capped this
        # exact case (~150 km/h, 100 m sight) at ~151 km/h and lifted half a
        # straight early.
        state = self._clear_straight_state(speed_x=150.0, gear=5, track=[100.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertIn("(accel 1.000)", out)
        self.assertIn("(brake 0.000)", out)

    def test_heading_offset_produces_corrective_steer(self):
        state = self._clear_straight_state(angle=0.10, track=[200.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertNotIn("(steer 0.000)", out)
        self.assertNotIn("(steer -", out)

    def test_ragged_straight_ignores_sensor_noise(self):
        # A grazing/ragged edge-reading straight must not steer sideways or
        # cost throttle — the small wandering aim inside the deadband must
        # be ignored.
        ragged = [15.0] * 7 + [90.0, 200.0, 200.0, 140.0, 52.0] + [15.0] * 7
        state = self._clear_straight_state(track=ragged, speed_x=180.0)
        out = compute_control(state, NORMAL)
        self.assertIn("(steer 0.000)", out)
        self.assertIn("(accel 1.000)", out)

    def test_off_centre_on_open_straight_drifts_toward_centre(self):
        state = self._clear_straight_state(track_pos=0.7, speed_x=200.0, track=[200.0] * 19)
        out = compute_control(state, NORMAL)
        self.assertIn("(steer -0.040)", out)
        self.assertIn("(accel 1.000)", out)

    def test_grazing_beam_does_not_falsely_cap_speed(self):
        # Regression: a ±5-10 deg beam grazing the edge of an otherwise clear
        # 90 m straight used to cap speed at ~100 km/h via a median-of-near-
        # beams miscalculation.
        graze = [150.0] * 9 + [90.0, 200.0, 200.0] + [150.0] * 7
        state = self._clear_straight_state(track=graze)
        out = compute_control(state, NORMAL)
        self.assertIn("(accel 1.000)", out)


class ComputeControlPhysicsOverrideTests(BotTestCase):
    def test_tight_corner_forces_full_brake_when_stopping_distance_exceeds_sight(self):
        track = [150.0] * 9 + [180.0] + [150.0] * 9
        tight = [40.0] * 7 + [25.0] * 5 + [40.0] * 7
        state = {"speed_x": 150.0, "rpm": 5000.0, "gear": 3, "angle": 0.0, "track_pos": 0.0,
                 "track": tight, "wheel_spin_vel": _wheel_vel(150.0)}
        out = compute_control(state, NORMAL)
        self.assertIn("(brake 1.000)", out)

    def test_ample_sight_leaves_partial_brake_alone(self):
        import re
        open_corner = [40.0] * 19
        state = {"speed_x": 160.0, "rpm": 5000.0, "gear": 3, "angle": 0.0, "track_pos": 0.0,
                 "track": open_corner, "wheel_spin_vel": _wheel_vel(160.0)}
        out = compute_control(state, NORMAL)
        m = re.search(r"\(brake ([-0-9.]+)\)", out)
        self.assertIsNotNone(m)
        self.assertTrue(0.0 < float(m.group(1)) < 1.0, out)


class ComputeControlPitTests(BotTestCase):
    def test_pit_never_sets_meta_even_at_low_speed(self):
        # Real bug fix (2026-08-09, see the self-test's own comment at this
        # exact scenario): `meta=1` means RACE RESTART to scr_server
        # (CarControl::META_RESTART), not "pit please". The old code reused
        # meta as a fake pit signal and would have restarted the race the
        # moment PIT strategy slowed the car below 10 km/h -- this asserts
        # the fix, not the old (buggy) behaviour.
        track = [150.0] * 9 + [180.0] + [150.0] * 9
        state = {"speed_x": 5.0, "rpm": 800.0, "gear": 1, "angle": 0.0, "track_pos": 0.0, "track": track}
        out = compute_control(state, PIT)
        self.assertNotIn("(meta 1)", out)


if __name__ == "__main__":
    unittest.main()
