"""Input-handling tests for the Commentary event-detection pipeline.

Targets `midware/commentary_engine.py` (pure functions + `CommentaryEngine`,
no FastAPI/network involved) per docs/commentary_test_plan.md work package A
section 5.1, and `CommentaryEngine.update_config` for the illegal-config case
in section 5.4. See docs/commentary_test_matrix.md for how each requirement
here maps to real code.
"""

import math
import unittest

from midware.commentary_engine import (
    CommentaryConfig,
    CommentaryEngine,
    normalize_frame,
)
from tests.fixtures.telemetry_frames import RAW_TORCS_FRAME


def make_frame(sim_time: float, **overrides) -> dict:
    """A minimal valid frame dict using the engine's own (snake_case) field
    names -- see normalize_frame() for the full accepted key set."""
    frame = {
        "sim_time": sim_time,
        "lap": 1,
        "speed_x": 100.0,
        "gear": 3,
        "track_pos": 0.0,
        "damage": 0.0,
        "race_pos": 5,
        "fuel": 50.0,
        "throttle": 0.5,
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


class TestEmptyAndMissingInput(unittest.TestCase):
    def test_empty_frame_list_returns_none_without_raising(self):
        engine = CommentaryEngine()
        self.assertIsNone(engine.next_decision([]))

    def test_single_frame_only_seeds_state_and_returns_none(self):
        engine = CommentaryEngine()
        result = engine.next_decision([make_frame(0.0)])
        self.assertIsNone(result)
        self.assertEqual(engine.last_lap, 1)

    def test_completely_empty_dict_frame_does_not_raise(self):
        # No keys at all -- normalize_frame must fall back to defaults for
        # every field rather than raising KeyError/TypeError.
        engine = CommentaryEngine()
        try:
            engine.next_decision([{}, {}])
        except Exception as exc:  # pragma: no cover - failure path
            self.fail(f"empty telemetry dict raised {type(exc).__name__}: {exc}")

    def test_missing_optional_fields_fall_back_to_documented_defaults(self):
        # normalize_frame() has no concept of "required" vs "optional" --
        # every field is read through number()/int() with a hard-coded
        # default (see commentary_engine.py:180-202). This test pins the
        # actual default values rather than assuming a "missing required
        # field -> explicit error" behaviour that the code does not have.
        normalized = normalize_frame({"sim_time": 5.0})
        self.assertEqual(normalized["sim_time"], 5.0)
        self.assertEqual(normalized["lap"], 0)
        self.assertEqual(normalized["race_pos"], 99)  # explicit default=99.0
        self.assertEqual(normalized["damage"], 0.0)
        self.assertEqual(len(normalized["opponents"]), 36)
        self.assertTrue(all(value == 200.0 for value in normalized["opponents"]))
        self.assertEqual(len(normalized["track"]), 19)
        self.assertTrue(all(value == -1.0 for value in normalized["track"]))

    def test_raw_torcs_frame_shape_is_accepted(self):
        # RAW_TORCS_FRAME (tests/fixtures/telemetry_frames.py) uses the
        # camelCase UDP field names (speedX/trackPos/...) that the real UDP
        # adapter produces; normalize_frame must accept that shape too.
        normalized = normalize_frame(RAW_TORCS_FRAME)
        self.assertEqual(normalized["lap"], 2)
        self.assertEqual(normalized["race_pos"], 3)
        self.assertAlmostEqual(normalized["speed_x"], 201.25)
        self.assertAlmostEqual(normalized["track_pos"], 0.2)


class TestMalformedNumericInput(unittest.TestCase):
    def test_non_numeric_string_field_falls_back_to_default_not_raise(self):
        normalized = normalize_frame({"sim_time": 1.0, "damage": "not-a-number"})
        self.assertEqual(normalized["damage"], 0.0)

    def test_wrong_type_field_falls_back_to_default_not_raise(self):
        normalized = normalize_frame({"sim_time": 1.0, "lap": {"nested": "dict"}})
        self.assertEqual(normalized["lap"], 0)

    def test_nan_speed_does_not_raise_and_does_not_trigger_pace_surge(self):
        # float("nan") is a legal float, so number() lets it through; NaN
        # comparisons are always False, so this must not crash and must not
        # spuriously trigger the speed-delta pace_surge rule.
        engine = CommentaryEngine()
        engine.next_decision([make_frame(0.0)])
        frames = [make_frame(0.0), make_frame(0.5, speed_x=math.nan, throttle=0.9)]
        decision = engine.next_decision(frames)
        if decision is not None:
            self.assertNotEqual(decision.event["event_type"], "pace_surge")

    def test_infinity_damage_does_not_raise(self):
        engine = CommentaryEngine()
        engine.next_decision([make_frame(0.0)])
        frames = [make_frame(0.0), make_frame(0.5, damage=math.inf)]
        try:
            engine.next_decision(frames)
        except Exception as exc:  # pragma: no cover - failure path
            self.fail(f"Infinity damage raised {type(exc).__name__}: {exc}")

    def test_out_of_range_values_do_not_raise(self):
        engine = CommentaryEngine()
        engine.next_decision([make_frame(0.0)])
        frames = [
            make_frame(0.0),
            make_frame(0.5, race_pos=-7, track_pos=999.0, damage=-500.0),
        ]
        try:
            engine.next_decision(frames)
        except Exception as exc:  # pragma: no cover - failure path
            self.fail(f"out-of-range telemetry raised {type(exc).__name__}: {exc}")


class TestFullValidTelemetry(unittest.TestCase):
    def test_full_legal_telemetry_processes_normally(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        engine.next_decision([make_frame(0.0)])
        # A clean lap increment with nothing else unusual should be detected
        # as lap_complete and nothing else.
        frames = [make_frame(0.0), make_frame(0.5, lap=2)]
        decision = engine.next_decision(frames)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "lap_complete")


class TestContinuousDuplicateFrames(unittest.TestCase):
    def test_repeated_identical_frame_does_not_repeatedly_fire(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        frame = make_frame(0.0)
        # Seed, then feed the *same* frame content many times at
        # incrementing sim_time (as a stalled/duplicate telemetry source
        # might do) -- nothing about the car state changes, so no event
        # should ever fire, and no exception should be raised.
        engine.next_decision([frame])
        decisions = []
        for i in range(1, 15):
            t = i * 0.1
            frames = [frame, {**frame, "sim_time": t}]
            decisions.append(engine.next_decision(frames))
        self.assertTrue(all(d is None for d in decisions))


class TestConfigValidation(unittest.TestCase):
    """5.4 'illegal config produces an explicit error' -- this pins the
    ACTUAL current behaviour of CommentaryEngine.update_config, which is a
    mix of the two: numeric fields raise (uncaught) on non-numeric input,
    but the `mode` field accepts any string with no validation at all. Per
    task-book rule (matrix.md do not fabricate a pass), both are recorded
    as-is rather than assumed."""

    def test_non_numeric_value_for_float_field_raises_value_error(self):
        engine = CommentaryEngine()
        with self.assertRaises(ValueError):
            engine.update_config({"baseline_interval": "not-a-number"})

    def test_unknown_mode_string_is_accepted_without_validation(self):
        # Documented gap (see commentary_test_matrix.md section on illegal
        # config): there is no allow-list check on `mode`, so this
        # currently succeeds and silently sets an invalid mode rather than
        # raising or being rejected.
        engine = CommentaryEngine()
        engine.update_config({"mode": "not_a_real_mode"})
        self.assertEqual(engine.config.mode, "not_a_real_mode")

    def test_valid_config_update_applies_and_type_coerces(self):
        engine = CommentaryEngine()
        engine.update_config({"baseline_interval": "12.5", "max_words": "30"})
        self.assertEqual(engine.config.baseline_interval, 12.5)
        self.assertEqual(engine.config.max_words, 30)
        self.assertIsInstance(engine.config.max_words, int)


if __name__ == "__main__":
    unittest.main()
