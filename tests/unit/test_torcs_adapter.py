import unittest

from midware.adapters.torcs_udp import TorcsTelemetryAdapter
from tests.fixtures.telemetry_frames import RAW_TORCS_FRAME


class TorcsAdapterTests(unittest.TestCase):
    def test_converts_boundary_names_once(self):
        frame = TorcsTelemetryAdapter.convert(RAW_TORCS_FRAME)
        self.assertEqual(frame.sim_time_s, 3.5)
        self.assertEqual(frame.speed_x_kmh, 201.25)
        self.assertEqual(frame.current_lap_time_s, 44.1)
        self.assertEqual(len(frame.track_sensors_m), 19)
        self.assertEqual(len(frame.opponent_sensors_m), 36)
