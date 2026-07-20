import csv
import io
import socket
import time
import unittest

from fastapi.testclient import TestClient

from midware.app import create_app
from midware.telemetry import MAIN_CSV_FIELDS


class ProtocolTelemetryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(create_app())
        cls.client = cls.context.__enter__()
        cls.client.post("/api/features/enabled", json={"enabled": ["coach", "bot", "engineer"]})

    @classmethod
    def tearDownClass(cls):
        cls.client.post(
            "/api/features/enabled",
            json={"enabled": ["commentary", "engineer", "coach", "bot"]},
        )
        cls.context.__exit__(None, None, None)

    def test_websocket_v1_and_legacy_fields(self):
        with self.client.websocket_connect("/ws") as websocket:
            connected = websocket.receive_json()
            self.assertEqual(connected["type"], "connected")
            self.assertEqual(connected["version"], 1)
            self.assertTrue(connected["request_id"])
            self.assertEqual(connected["sequence"], 0)
            websocket.send_text("ping")
            pong = websocket.receive_json()
            self.assertEqual(pong["type"], "pong")
            self.assertEqual(pong["version"], 1)

    def test_udp_packet_reaches_shared_store(self):
        values = {field: "0" for field in MAIN_CSV_FIELDS}
        values.update({"seq": "98765", "sim_time": "4321.5", "lap": "3", "speedX": "155.5"})
        stream = io.StringIO()
        csv.writer(stream).writerow([values[field] for field in MAIN_CSV_FIELDS])
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(stream.getvalue().encode("utf-8"), ("127.0.0.1", 3101))
        finally:
            sender.close()

        deadline = time.time() + 2.0
        telemetry = {}
        while time.time() < deadline:
            telemetry = self.client.get("/api/telemetry").json().get("telemetry") or {}
            if int(float(telemetry.get("seq", 0))) == 98765:
                break
            time.sleep(0.02)
        self.assertEqual(int(float(telemetry.get("seq", 0))), 98765)
        self.assertEqual(float(telemetry["speedX"]), 155.5)
        health = self.client.get("/api/health").json()
        self.assertGreaterEqual(health["telemetry"]["ingestor"]["received_frames"], 1)

    def test_coach_reads_shared_telemetry(self):
        response = self.client.get("/api/coach/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), dict)
