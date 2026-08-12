"""New in the 2026-08-12 "Granite strategy integration" merge (PR #37,
commit f4ff6ed): the rule-free prompt variants need the raw sensor numbers
translated into self-explanatory phrases (``build_situation()``,
``_describe_gap()``), lap-time bookkeeping for the "previous lap" comparison
line (``GraniteStrategist._note_lap_time``), and a reasoning trace
("considered"/"rejected" options) surfaced onto the dashboard
(``GraniteStrategist.last_considered``/``last_rejected``).

None of this changed the driving/safety-net behaviour covered elsewhere in
tests/bot/ — the full suite still passed unchanged after this merge except
for one incidental port-fixture collision (fixed separately in
test_scr_client_network.py) — but this new surface itself had zero test
coverage, which is what this file closes.
"""

import unittest

from ai_bot import ATTACK, GraniteStrategist, _describe_gap, build_situation
from telemetry_common import WorkerResult


class DescribeGapTests(unittest.TestCase):
    def test_sensor_saturated_reads_as_no_car_in_range(self):
        self.assertEqual(_describe_gap(200.0, 100.0, "in front"), "no car in front within sensor range")
        self.assertEqual(_describe_gap(190.0, 100.0, "in front"), "no car in front within sensor range")

    def test_just_under_saturation_still_reports_a_real_gap(self):
        text = _describe_gap(189.0, 100.0, "in front")
        self.assertIn("189", text)
        self.assertNotIn("no car", text)

    def test_low_speed_omits_the_misleading_seconds_estimate(self):
        # Regression: an 11 m gap on the starting grid (near-zero speed)
        # rendered as "~11.0 s", which the model quoted back as a real time
        # gap -- metres alone are honest at walking pace or below.
        text = _describe_gap(11.0, 5.0, "behind")
        self.assertEqual(text, "11 m behind")
        self.assertNotIn("s)", text)

    def test_normal_speed_includes_a_seconds_estimate(self):
        text = _describe_gap(50.0, 100.0, "behind")
        self.assertIn("50 m behind", text)
        self.assertIn("s at current speed", text)


class BuildSituationTests(unittest.TestCase):
    def test_missing_lap_count_is_explicit_not_a_misleading_zero(self):
        situation = build_situation({})
        self.assertEqual(situation["lap"], "lap count unknown")

    def test_laps_left_without_total_reports_remaining_only(self):
        situation = build_situation({"laps_left": 3})
        self.assertEqual(situation["lap"], "3 lap(s) remaining")

    def test_laps_left_with_total_reports_done_and_remaining(self):
        situation = build_situation({"laps_left": 3, "remaining_laps": 10})
        self.assertEqual(situation["lap"], "7 lap(s) done, 3 remaining")

    def test_missing_race_pos_is_explicit(self):
        self.assertEqual(build_situation({})["position"], "position unknown")

    def test_race_pos_present(self):
        self.assertEqual(build_situation({"race_pos": 4})["position"], "P4")

    def test_fuel_with_no_burn_rate_measured_yet(self):
        situation = build_situation({"fuel": 42.0})
        self.assertIn("42.0 L left", situation["fuel"])
        self.assertIn("not measured yet", situation["fuel"])

    def test_fuel_burn_rate_known_but_race_length_unknown(self):
        situation = build_situation({"fuel": 42.0, "fuel_per_lap": 3.0})
        self.assertIn("burning 3.0 L/lap", situation["fuel"])
        self.assertIn("race length unknown", situation["fuel"])

    def test_fuel_margin_reports_spare_when_enough_to_finish(self):
        # 5 laps * 3.0 L/lap = 15.0 L needed; 42.0 L on board -> spare.
        situation = build_situation({"fuel": 42.0, "fuel_per_lap": 3.0, "laps_left": 5})
        self.assertIn("needed to finish", situation["fuel"])
        self.assertIn("spare", situation["fuel"])

    def test_fuel_margin_reports_short_when_not_enough_to_finish(self):
        # 5 laps * 3.0 L/lap = 15.0 L needed; 10.0 L on board -> short.
        situation = build_situation({"fuel": 10.0, "fuel_per_lap": 3.0, "laps_left": 5})
        self.assertIn("SHORT of finishing", situation["fuel"])

    def test_damage_phrasing_below_and_above_heavily_damaged_threshold(self):
        self.assertIn("drivable", build_situation({"damage": 100.0})["damage"])
        self.assertIn("heavily damaged", build_situation({"damage": 8000.0})["damage"])

    def test_speed_is_rendered_in_kmh(self):
        self.assertEqual(build_situation({"speed_x": 123.4})["speed"], "123 km/h")

    def test_gap_ahead_and_behind_use_describe_gap(self):
        situation = build_situation({"opponents": [200.0] * 36, "speed_x": 50.0})
        self.assertIn("no car in front within sensor range", situation["gap_ahead"])
        self.assertIn("no car behind within sensor range", situation["gap_behind"])

    def test_no_completed_lap_yet(self):
        self.assertEqual(build_situation({})["last_lap"], "no completed lap yet")

    def test_completed_lap_with_no_previous_to_compare(self):
        situation = build_situation({"last_lap_time": 92.5})
        self.assertIn("92.5 s", situation["last_lap"])
        self.assertIn("no previous lap to compare", situation["last_lap"])

    def test_completed_lap_faster_than_previous(self):
        situation = build_situation({"last_lap_time": 90.0}, prev_lap_time=92.0)
        self.assertIn("faster", situation["last_lap"])

    def test_completed_lap_slower_than_previous(self):
        situation = build_situation({"last_lap_time": 94.0}, prev_lap_time=92.0)
        self.assertIn("slower", situation["last_lap"])


class GraniteStrategistLapTimeTrackingTests(unittest.TestCase):
    def test_first_completed_lap_is_noted_with_no_previous(self):
        strategist = GraniteStrategist(interval=999.0)
        strategist._note_lap_time({"last_lap_time": 90.0})
        self.assertEqual(strategist._prev_lap_time, 0.0)
        self.assertEqual(strategist._last_lap_seen, 90.0)

    def test_second_completed_lap_shifts_the_previous_forward(self):
        strategist = GraniteStrategist(interval=999.0)
        strategist._note_lap_time({"last_lap_time": 90.0})
        strategist._note_lap_time({"last_lap_time": 91.5})
        self.assertEqual(strategist._prev_lap_time, 90.0)
        self.assertEqual(strategist._last_lap_seen, 91.5)

    def test_unchanged_last_lap_time_does_not_shift_anything(self):
        # Called every tick, not just on lap completion -- most calls see
        # the same last_lap_time repeated and must be a no-op.
        strategist = GraniteStrategist(interval=999.0)
        strategist._note_lap_time({"last_lap_time": 90.0})
        strategist._note_lap_time({"last_lap_time": 90.0})
        strategist._note_lap_time({"last_lap_time": 90.0})
        self.assertEqual(strategist._prev_lap_time, 0.0)
        self.assertEqual(strategist._last_lap_seen, 90.0)

    def test_missing_or_zero_last_lap_time_is_ignored(self):
        strategist = GraniteStrategist(interval=999.0)
        strategist._note_lap_time({})
        strategist._note_lap_time({"last_lap_time": 0.0})
        self.assertEqual(strategist._last_lap_seen, 0.0)


class GraniteStrategistReasoningTraceTests(unittest.TestCase):
    def test_tick_populates_last_considered_and_last_rejected_from_the_trace(self):
        strategist = GraniteStrategist(interval=999.0)
        trace = {
            "considered": [{"strategy": "ATTACK", "why": "clear track"},
                           {"strategy": "DEFEND", "why": "no threat"}],
            "rejected": {"SAVE_FUEL": "fuel is not a concern yet"},
        }
        strategist._runner._results.put(WorkerResult(task={}, output=(ATTACK, "clear track", trace)))
        strategist.tick({})
        self.assertEqual(strategist.last_considered, trace["considered"])
        self.assertEqual(strategist.last_rejected, trace["rejected"])

    def test_trace_updates_even_when_the_active_strategy_does_not_switch(self):
        # tick() assigns last_considered/last_rejected unconditionally on
        # every successful result, before _debounce runs -- a re-confirming
        # answer must still refresh the displayed reasoning.
        strategist = GraniteStrategist(interval=999.0)
        strategist._runner._results.put(
            WorkerResult(task={}, output=(ATTACK, "first", {"considered": [], "rejected": {}}))
        )
        strategist.tick({})

        new_trace = {"considered": [{"strategy": "ATTACK", "why": "still clear"}], "rejected": {}}
        strategist._runner._results.put(WorkerResult(task={}, output=(ATTACK, "still clear", new_trace)))
        strategy, _ = strategist.tick({})
        self.assertEqual(strategy, ATTACK, "re-confirming the active strategy is a no-op switch")
        self.assertEqual(strategist.last_considered, new_trace["considered"])

    def test_default_trace_fields_are_empty_not_missing(self):
        strategist = GraniteStrategist(interval=999.0)
        self.assertEqual(strategist.last_considered, [])
        self.assertEqual(strategist.last_rejected, {})

    def test_error_result_does_not_touch_the_previous_trace(self):
        strategist = GraniteStrategist(interval=999.0)
        trace = {"considered": [{"strategy": "ATTACK", "why": "clear"}], "rejected": {}}
        strategist._runner._results.put(WorkerResult(task={}, output=(ATTACK, "clear", trace)))
        strategist.tick({})

        strategist._runner._results.put(WorkerResult(task={}, error="timeout"))
        strategist.tick({})
        self.assertEqual(strategist.last_considered, trace["considered"], "a failed call must not wipe the last good trace")


if __name__ == "__main__":
    unittest.main()
