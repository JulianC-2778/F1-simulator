"""GraniteStrategist: the async Granite caller that must never block the
drive loop and must degrade safely when the network/model call fails.

tests/unit/test_bot_clients.py already covers "tick() does not wait for a
slow network call" — this file covers what test_bot_clients.py doesn't:
what tick() actually returns/records once a result (success or error)
completes, and what _call_granite() sends/parses at the HTTP layer. See
docs/bot_test_plan.md section 4.5.

Rather than relying on real background-thread timing, most tests here push
a WorkerResult directly into GraniteStrategist's LatestTaskRunner queue
(the same queue tick() drains via pop_completed()) — this keeps the tests
deterministic instead of racing a real worker thread.

_call_granite()'s output is (strategy, reason, trace).  The trace holds the
model's "considered"/"rejected" reasoning fields and is empty for every
prompt variant that does not ask for them; it rides along with the decision
rather than being stashed on the strategist by the worker thread so that a
displayed trace can never belong to a request whose result was discarded as
stale.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from ai_bot import ATTACK, DEFEND, NORMAL, GraniteStrategist
from telemetry_common import WorkerResult


class GraniteStrategistTickTests(unittest.TestCase):
    def test_initial_strategy_before_any_result_is_normal(self):
        strategist = GraniteStrategist(interval=999.0)
        strategy, reason = strategist.tick({})
        self.assertEqual(strategy, NORMAL)
        self.assertEqual(reason, "startup")

    def test_successful_result_switches_strategy_and_clears_fallback(self):
        strategist = GraniteStrategist(interval=999.0)
        strategist.fallback = True
        strategist.last_error = "stale error from a previous failure"
        strategist._runner._results.put(
            WorkerResult(task={}, output=(ATTACK, "clear track", {}))
        )
        strategy, reason = strategist.tick({})
        self.assertEqual(strategy, ATTACK)
        self.assertEqual(reason, "clear track")
        self.assertFalse(strategist.fallback)
        self.assertEqual(strategist.last_error, "")

    def test_error_result_sets_fallback_and_holds_last_known_strategy(self):
        # This is the core safety property: if the Granite round-trip fails
        # (timeout, connection error, exception), the bot must keep driving
        # under whatever strategy was last confirmed, not crash or freeze.
        strategist = GraniteStrategist(interval=999.0)
        strategist._runner._results.put(
            WorkerResult(task={}, output=(DEFEND, "opponent close", {}))
        )
        strategist.tick({})
        self.assertEqual(strategist.last_strategy(), DEFEND)

        strategist._runner._results.put(WorkerResult(task={}, error="Connection refused"))
        strategy, reason = strategist.tick({})
        self.assertEqual(strategy, DEFEND, "must keep the last confirmed strategy, not reset to NORMAL")
        self.assertEqual(reason, "opponent close")
        self.assertTrue(strategist.fallback)
        self.assertEqual(strategist.last_error, "Connection refused")

    def test_recovers_from_fallback_once_a_new_result_succeeds(self):
        strategist = GraniteStrategist(interval=999.0)
        strategist._runner._results.put(WorkerResult(task={}, error="timeout"))
        strategist.tick({})
        self.assertTrue(strategist.fallback)

        strategist._runner._results.put(WorkerResult(task={}, output=(ATTACK, "recovered", {})))
        strategist.tick({})
        self.assertFalse(strategist.fallback)
        self.assertEqual(strategist.last_error, "")

    def test_tick_does_not_resubmit_before_the_interval_elapses(self):
        strategist = GraniteStrategist(interval=999.0)
        strategist._runner.submit = MagicMock(wraps=strategist._runner.submit)
        strategist.tick({})
        strategist.tick({})
        strategist.tick({})
        self.assertEqual(strategist._runner.submit.call_count, 1)

    def test_debounce_confirm_1_switches_on_first_completed_result(self):
        # _STRATEGY_CONFIRM is 1 in the current code, so a single completed
        # proposal that differs from the active strategy switches
        # immediately with no smoothing delay.
        strategist = GraniteStrategist(interval=999.0)
        strategist._runner._results.put(WorkerResult(task={}, output=(ATTACK, "go", {})))
        strategy, _ = strategist.tick({})
        self.assertEqual(strategy, ATTACK)


class CallGraniteHttpLayerTests(unittest.TestCase):
    class _FakeUrlopenResponse:
        def __init__(self, payload: bytes):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._payload

    def test_posts_current_strategy_and_sensor_state_to_bot_strategy_endpoint(self):
        strategist = GraniteStrategist(base_url="http://127.0.0.1:9999", interval=999.0)
        response_body = json.dumps({"decision": {"strategy": "ATTACK", "reason": "clear"}}).encode()
        with patch("urllib.request.urlopen", return_value=self._FakeUrlopenResponse(response_body)) as mock_urlopen:
            strategy, reason, trace = strategist._call_granite({"state": {"fuel": 40.0}})
        self.assertEqual(strategy, ATTACK)
        self.assertEqual(reason, "clear")
        # No considered/rejected in this reply, so the trace is empty rather
        # than absent — downstream reads it unconditionally.
        self.assertEqual(trace, {"considered": [], "rejected": {}})
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "http://127.0.0.1:9999/api/bot/strategy")
        body = json.loads(request.data.decode("utf-8"))
        # The raw state is forwarded as-is, alongside two derived fields the
        # rule-free prompt variants need: `situation` (the same numbers
        # rendered as self-explanatory phrases) and `allowed_strategies` (so
        # the prompt never offers an option this bot would discard).
        self.assertEqual(body["sensor_state"]["fuel"], 40.0)
        self.assertIn("situation", body["sensor_state"])
        self.assertIn("allowed_strategies", body["sensor_state"])
        self.assertIn("current_strategy", body)

    def test_malformed_decision_falls_back_to_normal(self):
        strategist = GraniteStrategist(base_url="http://127.0.0.1:9999", interval=999.0)
        response_body = json.dumps({"decision": {}}).encode()
        with patch("urllib.request.urlopen", return_value=self._FakeUrlopenResponse(response_body)):
            strategy, _, _ = strategist._call_granite({"state": {}})
        self.assertEqual(strategy, NORMAL)

    def test_network_failure_propagates_as_an_exception_for_the_runner_to_catch(self):
        # _call_granite itself does not swallow errors — LatestTaskRunner's
        # worker loop is what converts the exception into a WorkerResult
        # with .error set (see telemetry_common.py::LatestTaskRunner._run).
        strategist = GraniteStrategist(base_url="http://127.0.0.1:9999", interval=999.0)
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            with self.assertRaises(OSError):
                strategist._call_granite({"state": {}})


if __name__ == "__main__":
    unittest.main()
