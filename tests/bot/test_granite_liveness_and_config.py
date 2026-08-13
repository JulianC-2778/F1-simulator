"""New since the 2026-08-12 real-experiment/fault-injection work (PR #38-40,
commits "update ai_bot with race4 experiments and dashboard improvements"
and follow-ups): GraniteStrategist gained dashboard-facing liveness/timing
signals, and its polling pace/timeout became prompt-mode- and
environment-driven instead of a single hardcoded constant.

Two of these are a direct, cited response to gaps this test suite's own
real-data reports found:

- ``GraniteStrategist.last_round_trip_s`` / ``answer_seq`` — added because
  ``docs/bot_real_experiment_20260812.md`` section 4 had to caveat its own
  Granite decision-cadence numbers: the old ``TraceRecorder.decision()``
  only logged when the answer *text* changed, silently merging genuinely
  separate answers that happened to be worded the same. ``answer_seq``
  increments on every completed round trip (success or error) so a reader
  can tell a repeat from a dropped record, and ``last_round_trip_s`` is a
  real per-request measurement instead of the old cadence proxy.
- ``_STRATEGY_INTERVAL``/``_GRANITE_TIMEOUT`` becoming mode-dependent — the
  code comment above their definition explicitly says the old flat 5s/30s
  pairing is what our own 2026-08-12 session ran the reasoning prompt
  under, which is too fast for a ~7.6s-median reasoning answer and
  saturated the model for the whole race. See
  ``docs/bot_real_experiment_20260812.md``'s own updated caveat.

None of this changed the driving/safety-net behaviour tested elsewhere in
tests/bot/ (confirmed: the full suite passed unchanged after this merge) —
this file closes the coverage gap on the new surface itself.
"""

import unittest
from unittest.mock import patch

import ai_bot
from ai_bot import ATTACK, GraniteStrategist, _interval_from_env
from telemetry_common import WorkerResult


class IntervalFromEnvTests(unittest.TestCase):
    def test_unset_env_var_uses_the_given_default(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("TORCS_BOT_INTERVAL", None)
            self.assertEqual(_interval_from_env(15.0), 15.0)

    def test_valid_override_is_used(self):
        with patch.dict("os.environ", {"TORCS_BOT_INTERVAL": "12.5"}):
            self.assertEqual(_interval_from_env(15.0), 12.5)

    def test_non_numeric_value_falls_back_to_default(self):
        with patch.dict("os.environ", {"TORCS_BOT_INTERVAL": "not-a-number"}):
            self.assertEqual(_interval_from_env(15.0), 15.0)

    def test_value_below_one_second_falls_back_to_default(self):
        # Must not silently let the bot hammer the model broker at 0s/negative pacing.
        with patch.dict("os.environ", {"TORCS_BOT_INTERVAL": "0.5"}):
            self.assertEqual(_interval_from_env(15.0), 15.0)
        with patch.dict("os.environ", {"TORCS_BOT_INTERVAL": "-3"}):
            self.assertEqual(_interval_from_env(15.0), 15.0)

    def test_exactly_one_second_is_accepted(self):
        with patch.dict("os.environ", {"TORCS_BOT_INTERVAL": "1.0"}):
            self.assertEqual(_interval_from_env(15.0), 1.0)


class GraniteStrategistThinkingStateTests(unittest.TestCase):
    def test_starts_not_thinking_with_no_answer_yet(self):
        strategist = GraniteStrategist(interval=999.0)
        self.assertFalse(strategist.thinking)
        self.assertIsNone(strategist._answered_at)
        self.assertEqual(strategist.answer_seq, 0)

    def test_submitting_a_request_sets_thinking_true(self):
        strategist = GraniteStrategist(interval=0.0)
        strategist.tick({})
        self.assertTrue(strategist.thinking)

    def test_a_completed_success_clears_thinking_and_advances_seq(self):
        strategist = GraniteStrategist(interval=0.0)
        strategist.tick({})  # submits, thinking=True
        strategist._runner._results.put(WorkerResult(task={}, output=(ATTACK, "go", {})))
        strategist.tick({})
        self.assertFalse(strategist.thinking)
        self.assertIsNotNone(strategist._answered_at)
        self.assertEqual(strategist.answer_seq, 1)

    def test_a_completed_error_also_clears_thinking_and_advances_seq(self):
        # A single timeout must not leave the dashboard saying "thinking" forever.
        strategist = GraniteStrategist(interval=0.0)
        strategist.tick({})
        strategist._runner._results.put(WorkerResult(task={}, error="timeout"))
        strategist.tick({})
        self.assertFalse(strategist.thinking)
        self.assertEqual(strategist.answer_seq, 1)

    def test_seq_advances_once_per_completed_round_trip_even_with_identical_text(self):
        # This is the exact gap the real-experiment report flagged: two
        # answers with the same wording must both be counted, not merged.
        strategist = GraniteStrategist(interval=999.0)
        strategist._runner._results.put(WorkerResult(task={}, output=(ATTACK, "same text", {})))
        strategist.tick({})
        self.assertEqual(strategist.answer_seq, 1)
        strategist._runner._results.put(WorkerResult(task={}, output=(ATTACK, "same text", {})))
        strategist.tick({})
        self.assertEqual(strategist.answer_seq, 2)


class GraniteStrategistLivenessTests(unittest.TestCase):
    def test_liveness_before_any_answer(self):
        strategist = GraniteStrategist(interval=15.0)
        live = strategist.liveness()
        self.assertFalse(live["thinking"])
        self.assertIsNone(live["age_s"])
        self.assertEqual(live["interval_s"], 15.0)
        self.assertIsNone(live["round_trip_s"])
        self.assertGreaterEqual(live["next_in_s"], 0.0)

    def test_liveness_reflects_thinking_state(self):
        strategist = GraniteStrategist(interval=0.0)
        strategist.tick({})
        self.assertTrue(strategist.liveness()["thinking"])

    def test_liveness_age_s_is_non_negative_after_an_answer(self):
        strategist = GraniteStrategist(interval=999.0)
        strategist._runner._results.put(WorkerResult(task={}, output=(ATTACK, "go", {})))
        strategist.tick({})
        live = strategist.liveness()
        self.assertIsNotNone(live["age_s"])
        self.assertGreaterEqual(live["age_s"], 0.0)

    def test_liveness_next_in_s_never_goes_negative(self):
        # interval already elapsed (0.0) -> must clamp at 0, not report a
        # confusing negative "seconds until next request".
        strategist = GraniteStrategist(interval=0.0)
        self.assertEqual(strategist.liveness()["next_in_s"], 0.0)

    def test_liveness_reports_the_measured_round_trip(self):
        strategist = GraniteStrategist(interval=999.0)
        strategist.last_round_trip_s = 7.612
        self.assertEqual(strategist.liveness()["round_trip_s"], 7.612)


class CallGraniteRoundTripTimingTests(unittest.TestCase):
    class _DelayedFakeResponse:
        def __init__(self, payload: bytes, delay: float):
            self._payload = payload
            self._delay = delay

        def __enter__(self):
            import time
            time.sleep(self._delay)
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._payload

    def test_last_round_trip_s_measures_the_real_call_duration(self):
        import json
        strategist = GraniteStrategist(base_url="http://127.0.0.1:9999", interval=999.0)
        response_body = json.dumps({"decision": {"strategy": "ATTACK", "reason": "clear"}}).encode()
        with patch(
            "urllib.request.urlopen",
            return_value=self._DelayedFakeResponse(response_body, delay=0.05),
        ):
            strategist._call_granite({"state": {}})
        self.assertIsNotNone(strategist.last_round_trip_s)
        self.assertGreaterEqual(strategist.last_round_trip_s, 0.05)
        self.assertLess(strategist.last_round_trip_s, 1.0, "should not include unrelated overhead")


if __name__ == "__main__":
    unittest.main()
