import unittest

from midware.schemas.race import CarState
from midware.schemas.telemetry import TelemetryFrame


class TelemetrySchemaTests(unittest.TestCase):
    def test_defaults_and_units_contract(self):
        frame = TelemetryFrame(seq=1, speed_x_kmh=123.4)
        self.assertEqual(frame.seq, 1)
        self.assertEqual(frame.speed_x_kmh, 123.4)
        self.assertEqual(frame.track_sensors_m, [])

    def test_missing_tire_wear_is_none(self):
        self.assertIsNone(CarState().tire_wear_percent)
