#!/usr/bin/env python3
"""Unit tests for car_state_source.py (Direction 1 telemetry data sources)."""

import json
import unittest
from unittest.mock import MagicMock, patch

from car_state_source import FakeCarStateSource, HttpCarStateSource, wait_for_live_state
from race_analyzer import CAR_STATE_KEYS, empty_car_state


class FakeCarStateSourceTests(unittest.TestCase):
    def test_get_state_returns_full_contract_shape(self):
        state = FakeCarStateSource().get_state()
        self.assertEqual(set(state.keys()), set(CAR_STATE_KEYS))

    def test_get_state_cycles_through_demo_scenarios_with_matching_problems(self):
        source = FakeCarStateSource()
        first = source.get_state()
        second = source.get_state()
        third = source.get_state()
        fourth = source.get_state()

        self.assertEqual(first["speed"], 210.0)
        self.assertEqual(first["problems"], ["rpm too high"])

        self.assertEqual(second["speed"], 65.0)
        self.assertEqual(second["problems"], ["gear too high"])

        self.assertEqual(third["speed"], 180.0)
        self.assertEqual(third["problems"], ["car damage high", "near track edge"])

        # The demo cycle wraps back to the first scenario on the 4th call.
        self.assertEqual(fourth, first)


class HttpCarStateSourceTests(unittest.TestCase):
    @staticmethod
    def _mock_response(body: bytes):
        response = MagicMock()
        response.read.return_value = body
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    def test_get_state_parses_live_telemetry_payload(self):
        payload = json.dumps(
            {
                "telemetry": {
                    "speedX": 150.0,
                    "rpm": 7000.0,
                    "gear": 4,
                    "trackPos": 0.1,
                    "damage": 200.0,
                    "fuel": 40.0,
                    "curLapTime": 55.5,
                },
                "rankings": [],
            }
        ).encode("utf-8")

        with patch("car_state_source.request.urlopen", return_value=self._mock_response(payload)):
            source = HttpCarStateSource(base_url="http://fake-midware:9999")
            self.assertTrue(source.is_ready())
            state = source.get_state()

        self.assertEqual(state["speed"], 150.0)
        self.assertEqual(state["gear"], 4)
        self.assertEqual(state["problems"], ["normal"])

    def test_get_state_falls_back_to_empty_state_when_midware_is_unreachable(self):
        with patch("car_state_source.request.urlopen", side_effect=OSError("connection refused")):
            source = HttpCarStateSource(base_url="http://fake-midware:9999")
            self.assertFalse(source.is_ready())
            self.assertEqual(source.get_state(), empty_car_state())

    def test_get_state_falls_back_to_empty_state_on_malformed_json(self):
        with patch("car_state_source.request.urlopen", return_value=self._mock_response(b"not json")):
            source = HttpCarStateSource(base_url="http://fake-midware:9999")
            self.assertFalse(source.is_ready())
            self.assertEqual(source.get_state(), empty_car_state())

    def test_get_state_falls_back_to_empty_state_when_telemetry_key_missing(self):
        payload = json.dumps({"rankings": []}).encode("utf-8")
        with patch("car_state_source.request.urlopen", return_value=self._mock_response(payload)):
            source = HttpCarStateSource(base_url="http://fake-midware:9999")
            self.assertEqual(source.get_state(), empty_car_state())


class _StubSource:
    """A fake CarStateSource-like object that becomes ready after N polls."""

    def __init__(self, ready_after: int):
        self._calls = 0
        self._ready_after = ready_after

    def is_ready(self) -> bool:
        self._calls += 1
        return self._calls >= self._ready_after


class WaitForLiveStateTests(unittest.TestCase):
    def test_returns_true_once_source_becomes_ready(self):
        self.assertTrue(wait_for_live_state(_StubSource(ready_after=3), timeout=2.0))

    def test_returns_false_when_source_never_becomes_ready(self):
        self.assertFalse(wait_for_live_state(_StubSource(ready_after=10_000), timeout=0.2))


if __name__ == "__main__":
    unittest.main()
