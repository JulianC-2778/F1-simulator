"""BotStatusReporter: the client-side heartbeat thread that must never let a
slow or broken network stall the drive loop. tests/unit/test_bot_clients.py
already covers "tick() does not wait for the network" — this file covers
what happens once the background send actually runs: failures must be
swallowed, not raised into the drive loop, and close() must send a final
"disconnected" update. See docs/bot_test_plan.md section 4.6.
"""

import time
import unittest
from unittest.mock import patch

from ai_bot import BotStatusReporter, NORMAL


class _RaisingResponse:
    def __enter__(self):
        raise ConnectionRefusedError("nobody is listening")

    def __exit__(self, *args):
        return False


class _OkResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"ok":true}'


class BotStatusReporterTests(unittest.TestCase):
    def test_initial_state_is_disconnected_normal(self):
        with patch("urllib.request.urlopen", return_value=_OkResponse()):
            reporter = BotStatusReporter(interval=999.0)
            with reporter._lock:
                self.assertEqual(reporter._latest, {"connected": False, "strategy": NORMAL})
            reporter.close()

    def test_network_failure_is_swallowed_not_raised(self):
        # The background thread must survive a broken connection indefinitely
        # (it runs for the whole race) — an unhandled exception here would
        # silently kill heartbeat reporting for the rest of the session.
        with patch("urllib.request.urlopen", return_value=_RaisingResponse()):
            reporter = BotStatusReporter(interval=0.01)
            reporter.update(connected=True, strategy="ATTACK", immediate=True)
            time.sleep(0.1)  # let the background thread attempt (and fail) the send
            self.assertTrue(reporter._thread.is_alive(), "reporter thread must survive a failed send")
            reporter.close()

    def test_update_merges_fields_without_dropping_previous_ones(self):
        with patch("urllib.request.urlopen", return_value=_OkResponse()):
            reporter = BotStatusReporter(interval=999.0)
            reporter.update(connected=True, strategy="ATTACK")
            reporter.update(speed_kmh=180.0)
            with reporter._lock:
                self.assertEqual(reporter._latest["connected"], True)
                self.assertEqual(reporter._latest["strategy"], "ATTACK")
                self.assertEqual(reporter._latest["speed_kmh"], 180.0)
            reporter.close()

    def test_close_sends_a_final_disconnected_update(self):
        sent_payloads = []

        class _CapturingResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                return b"{}"

        def fake_urlopen(request, timeout=None):
            import json
            sent_payloads.append(json.loads(request.data.decode("utf-8")))
            return _CapturingResponse()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            reporter = BotStatusReporter(interval=999.0)
            reporter.update(connected=True, strategy="ATTACK", immediate=True)
            time.sleep(0.05)
            reporter.close()
            time.sleep(0.05)

        self.assertTrue(sent_payloads, "close() must trigger at least one send")
        self.assertEqual(sent_payloads[-1]["connected"], False)

    def test_close_is_idempotent_and_bounded(self):
        with patch("urllib.request.urlopen", return_value=_OkResponse()):
            reporter = BotStatusReporter(interval=999.0)
            started = time.monotonic()
            reporter.close()
            reporter.close()
            self.assertLess(time.monotonic() - started, 2.0, "close() must not hang")


if __name__ == "__main__":
    unittest.main()
