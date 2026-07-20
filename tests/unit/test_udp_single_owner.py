import socket
import unittest

from midware.adapters.torcs_udp import TorcsUdpAdapter
from midware.telemetry import TelemetryStore


class UdpSingleOwnerTests(unittest.TestCase):
    def test_second_listener_fails_explicitly(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        first = TorcsUdpAdapter(TelemetryStore(), host="127.0.0.1", port=port)
        second = TorcsUdpAdapter(TelemetryStore(), host="127.0.0.1", port=port)
        first.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "Unable to bind telemetry UDP"):
                second.start()
        finally:
            first.stop()
