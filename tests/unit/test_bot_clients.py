import time
import unittest
from unittest.mock import patch

from ai_bot import BotStatusReporter, GraniteStrategist


class _Response:
    def __enter__(self):
        time.sleep(0.15)
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"ok":true}'


class BotClientTests(unittest.TestCase):
    def test_status_tick_does_not_wait_for_network(self):
        with patch("urllib.request.urlopen", return_value=_Response()):
            reporter = BotStatusReporter(interval=0.01)
            started = time.monotonic()
            reporter.tick(connected=True, strategy="NORMAL")
            self.assertLess(time.monotonic() - started, 0.05)
            reporter.close()

    def test_strategy_tick_does_not_wait_for_network(self):
        strategist = GraniteStrategist(interval=0.0)

        def slow_call(task):
            time.sleep(0.15)
            return "ATTACK", "clear"

        strategist._runner._worker = slow_call
        started = time.monotonic()
        self.assertEqual(strategist.tick({})[0], "NORMAL")
        self.assertLess(time.monotonic() - started, 0.05)
