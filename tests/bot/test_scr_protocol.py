"""SCR wire-protocol layer: parse_scr_state / format_scr_control / ScrClient.

Ports the assertions from ai_bot.py's built-in ``_run_tests()`` (section
"---- parse_scr_state ----" / "---- format_scr_control ----" / "---- ScrClient
API ----") into pytest so they run under `tools/run_tests.sh` / CI instead of
only via a manual `python ai_bot.py` invocation. See docs/bot_test_plan.md
section 4.1.
"""

import unittest

from ai_bot import ScrClient, format_scr_control, parse_scr_state


def _sample_packet() -> str:
    opponents = " ".join(["200.0"] * 36)
    track = " ".join(["150.0"] * 9 + ["180.0"] + ["150.0"] * 9)
    wheels = "12.5 12.5 13.0 13.0"
    focus = "-1.0 -1.0 -1.0 -1.0 -1.0"
    return (
        f"(angle 0.015)(curLapTime 42.3)(damage 0)(distFromStart 312.7)"
        f"(distRaced 312.7)(fuel 38.5)(gear 4)(lastLapTime 91.2)"
        f"(opponents {opponents})(racePos 2)(rpm 7800)"
        f"(speedX 148.3)(speedY -0.4)(speedZ 0.0)"
        f"(track {track})(trackPos 0.12)(wheelSpinVel {wheels})"
        f"(z 0.33)(focus {focus})(x 241.0)(y 88.0)"
        f"(roll 0.0)(pitch 0.01)(yaw 1.57)"
        f"(speedGlobalX 120.1)(speedGlobalY 88.3)"
    )


class ParseScrStateTests(unittest.TestCase):
    def test_full_valid_packet(self):
        state = parse_scr_state(_sample_packet())
        self.assertIsNotNone(state)
        self.assertEqual(state["gear"], 4)
        self.assertEqual(state["race_pos"], 2)
        self.assertAlmostEqual(state["speed_x"], 148.3)
        self.assertAlmostEqual(state["fuel"], 38.5)
        self.assertEqual(len(state["opponents"]), 36)
        self.assertEqual(len(state["track"]), 19)
        self.assertEqual(len(state["wheel_spin_vel"]), 4)
        self.assertEqual(len(state["focus"]), 5)
        self.assertEqual(state["opponents"][0], 200.0)
        self.assertEqual(state["focus"][0], -1.0)

    def test_empty_string_returns_none(self):
        self.assertIsNone(parse_scr_state(""))

    def test_incomplete_packet_missing_required_keys_returns_none(self):
        self.assertIsNone(parse_scr_state("(angle 0.1)"))

    def test_short_opponents_array_is_padded_to_36(self):
        track = " ".join(["150.0"] * 9 + ["180.0"] + ["150.0"] * 9)
        wheels = "12.5 12.5 13.0 13.0"
        short_opp = " ".join(["50.0"] * 10)
        partial = (
            f"(angle 0)(curLapTime 0)(damage 0)(distFromStart 0)(distRaced 0)"
            f"(fuel 30)(gear 1)(lastLapTime 0)(opponents {short_opp})"
            f"(racePos 1)(rpm 0)(speedX 0)(speedY 0)(speedZ 0)"
            f"(track {track})(trackPos 0)(wheelSpinVel {wheels})(z 0)"
        )
        state = parse_scr_state(partial)
        self.assertIsNotNone(state)
        self.assertEqual(len(state["opponents"]), 36)
        self.assertEqual(state["opponents"][35], 200.0, "padding value must be the array default")

    def test_non_numeric_field_falls_back_to_default(self):
        track = " ".join(["150.0"] * 19)
        wheels = "0 0 0 0"
        packet = (
            f"(angle 0)(curLapTime 0)(damage NaN)(distFromStart 0)(distRaced 0)"
            f"(fuel 30)(gear notanumber)(lastLapTime 0)(opponents )"
            f"(racePos 1)(rpm 0)(speedX 100.0)(speedY 0)(speedZ 0)"
            f"(track {track})(trackPos 0)(wheelSpinVel {wheels})(z 0)"
        )
        state = parse_scr_state(packet)
        self.assertIsNotNone(state)
        # gear is an _INT_FIELDS entry parsed via parse_int(..., default=0)
        self.assertEqual(state["gear"], 0)
        # damage falls through parse_float(..., default=0.0); "NaN" IS valid
        # float() input in Python, so this documents the real behaviour
        # rather than assuming it was rejected.
        self.assertTrue(state["damage"] != state["damage"] or state["damage"] == 0.0)


class FormatScrControlTests(unittest.TestCase):
    def test_normal_range_values(self):
        ctrl = format_scr_control(accel=0.8, brake=0.0, gear=3, steer=-0.12)
        self.assertIn("(accel 0.800)", ctrl)
        self.assertIn("(brake 0.000)", ctrl)
        self.assertIn("(gear 3)", ctrl)
        self.assertIn("(steer -0.120)", ctrl)
        self.assertIn("(clutch 0.000)", ctrl)
        self.assertIn("(focus 0)", ctrl)
        self.assertIn("(meta 0)", ctrl)

    def test_out_of_range_values_are_clamped(self):
        ctrl = format_scr_control(accel=2.0, brake=-1.0, steer=5.0, focus=200)
        self.assertIn("(accel 1.000)", ctrl)
        self.assertIn("(brake 0.000)", ctrl)
        self.assertIn("(steer 1.000)", ctrl)
        self.assertIn("(focus 90)", ctrl)

    def test_negative_steer_and_focus_clamp_symmetrically(self):
        ctrl = format_scr_control(steer=-5.0, focus=-200)
        self.assertIn("(steer -1.000)", ctrl)
        self.assertIn("(focus -90)", ctrl)

    def test_meta_is_normalised_to_0_or_1(self):
        self.assertIn("(meta 1)", format_scr_control(meta=1))
        self.assertIn("(meta 1)", format_scr_control(meta=5))
        self.assertIn("(meta 0)", format_scr_control(meta=0))


class ScrClientTests(unittest.TestCase):
    def test_instantiation_records_address(self):
        client = ScrClient("localhost", 3001)
        self.assertEqual(client._addr, ("localhost", 3001))
        self.assertFalse(client.is_shutdown)
        client.close()

    def test_close_without_connect_is_a_no_op(self):
        client = ScrClient("localhost", 3001)
        client.close()  # must not raise
        client.close()  # idempotent

    def test_receive_state_without_connect_raises(self):
        client = ScrClient("localhost", 3001)
        with self.assertRaises(RuntimeError):
            client.receive_state()

    def test_send_control_without_connect_raises(self):
        client = ScrClient("localhost", 3001)
        with self.assertRaises(RuntimeError):
            client.send_control("(accel 1.000)")


if __name__ == "__main__":
    unittest.main()
