"""ScrClient's real UDP protocol behavior: handshake, receive/send over an
actual socket, backlog draining, and the timeout/shutdown/restart signals.

Everything in tests/bot/test_scr_protocol.py exercises ScrClient before a
handshake (instantiate/close/not-connected errors) — this file is the
counterpart that actually opens sockets and talks the real SCR wire
protocol, against a small local UDP stand-in for scr_server
(``_FakeTorcsServer``) instead of real TORCS. See
docs/bot_test_matrix.md section 6 ("ScrClient's real UDP handshake and
receive/send path" — previously an explicit gap).

No real TORCS is used or required; everything runs over 127.0.0.1 on an
OS-assigned ephemeral port, so tests can run in any order/host without port
collisions.
"""

import socket
import threading
import time
import unittest
from unittest.mock import patch

import ai_bot
from ai_bot import ScrClient, format_scr_control


def _sample_state_packet(**overrides):
    fields = {
        "angle": "0.0", "curLapTime": "10.0", "damage": "0", "distFromStart": "50.0",
        "distRaced": "50.0", "fuel": "40.0", "gear": "3", "lastLapTime": "0.0",
        "opponents": " ".join(["200.0"] * 36), "racePos": "1", "rpm": "5000.0",
        "speedX": "100.0", "speedY": "0.0", "speedZ": "0.0",
        "track": " ".join(["150.0"] * 19), "trackPos": "0.0",
        "wheelSpinVel": "10.0 10.0 10.0 10.0", "z": "0.3",
    }
    fields.update(overrides)
    return "".join(f"({k} {v})" for k, v in fields.items())


class _FakeTorcsServer:
    """Minimal UDP stand-in for scr_server: answers the SCR(init...)
    handshake, then just exposes plain send/recv so a test can script
    exactly what the real ScrClient sees and sends."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._client_addr = None

    def wait_for_handshake(self, timeout=2.0):
        self._sock.settimeout(timeout)
        data, addr = self._sock.recvfrom(2048)
        assert data.decode().startswith("SCR(init"), data
        self._client_addr = addr
        self._sock.sendto(b"***identified***", addr)

    def send_to_client(self, text: str):
        assert self._client_addr is not None, "handshake not completed yet"
        self._sock.sendto(text.encode(), self._client_addr)

    def recv_from_client(self, timeout=1.0):
        self._sock.settimeout(timeout)
        try:
            data, _ = self._sock.recvfrom(2048)
            return data.decode()
        except socket.timeout:
            return None

    def close(self):
        self._sock.close()


def _handshake(client: ScrClient, server: _FakeTorcsServer):
    """Runs client.connect() on a background thread (it blocks waiting for
    the reply) while the test thread plays the server side synchronously."""
    error = {}

    def _connect():
        try:
            client.connect()
        except Exception as exc:  # noqa: BLE001 - re-raised on the test thread
            error["exc"] = exc

    thread = threading.Thread(target=_connect, daemon=True)
    thread.start()
    server.wait_for_handshake()
    thread.join(timeout=2.0)
    if "exc" in error:
        raise error["exc"]


class ScrClientHandshakeTests(unittest.TestCase):
    def test_handshake_succeeds_against_a_responding_server(self):
        server = _FakeTorcsServer()
        client = ScrClient("127.0.0.1", server.port)
        try:
            _handshake(client, server)  # must not raise
        finally:
            client.close()
            server.close()

    def test_handshake_fails_cleanly_when_nothing_responds(self):
        # No server at all — patch the retry/timeout constants down so this
        # test takes a fraction of a second instead of the real 5s x 5
        # retries the production defaults use.
        with patch.object(ai_bot, "_HANDSHAKE_TIMEOUT", 0.05), \
             patch.object(ai_bot, "_HANDSHAKE_RETRIES", 2):
            client = ScrClient("127.0.0.1", 1)  # nothing listens on port 1
            with self.assertRaises(ConnectionError):
                client.connect()
            client.close()

    def test_guard_port_conflict_gives_a_specific_actionable_error(self):
        # Reserve the exact local guard port ai_bot.py's own comment derives
        # for this target port, then confirm a second "instance" fails fast
        # with a message that names the problem instead of hanging or
        # silently splitting the packet stream with the first client.
        target_port = 40001
        guard_port = ai_bot._LOCAL_PORT_BASE + (target_port % 100)
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("", guard_port))
        try:
            client = ScrClient("127.0.0.1", target_port)
            with self.assertRaises(ConnectionError) as ctx:
                client.connect()
            self.assertIn("Another bot instance", str(ctx.exception))
            client.close()
        finally:
            blocker.close()


class ScrClientReceiveSendTests(unittest.TestCase):
    def setUp(self):
        self.server = _FakeTorcsServer()
        self.client = ScrClient("127.0.0.1", self.server.port)
        _handshake(self.client, self.server)

    def tearDown(self):
        self.client.close()
        self.server.close()

    def test_receive_state_parses_a_real_packet_off_the_wire(self):
        self.server.send_to_client(_sample_state_packet(speedX="123.4", gear="5"))
        state = self.client.receive_state()
        self.assertIsNotNone(state)
        self.assertAlmostEqual(state["speed_x"], 123.4)
        self.assertEqual(state["gear"], 5)

    def test_receive_state_times_out_to_an_empty_dict_not_none(self):
        # {} (timeout, keep waiting) must be distinguishable from None (race
        # ended) -- see ai_bot.py's own comment on why resending on a
        # timeout is a real prior bug (it made scr_server's control queue
        # run permanently behind).
        state = self.client.receive_state()
        self.assertEqual(state, {})

    def test_receive_state_drains_backlog_and_returns_only_the_newest(self):
        self.server.send_to_client(_sample_state_packet(gear="1"))
        self.server.send_to_client(_sample_state_packet(gear="2"))
        self.server.send_to_client(_sample_state_packet(gear="3"))
        time.sleep(0.05)  # let all three land in the OS receive buffer first
        state = self.client.receive_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["gear"], 3, "must act on the newest queued frame, not the oldest")

    def test_receive_state_returns_none_and_marks_shutdown_on_shutdown_signal(self):
        self.server.send_to_client("***shutdown***")
        state = self.client.receive_state()
        self.assertIsNone(state)
        self.assertTrue(self.client.is_shutdown)

    def test_receive_state_returns_none_without_marking_shutdown_on_restart_signal(self):
        self.server.send_to_client("***restart***")
        state = self.client.receive_state()
        self.assertIsNone(state)
        self.assertFalse(self.client.is_shutdown, "a restart is not a shutdown")

    def test_send_control_reaches_the_server_verbatim(self):
        ctrl = format_scr_control(accel=0.75, brake=0.0, gear=4, steer=0.1)
        self.client.send_control(ctrl)
        received = self.server.recv_from_client()
        self.assertEqual(received, ctrl)


if __name__ == "__main__":
    unittest.main()
