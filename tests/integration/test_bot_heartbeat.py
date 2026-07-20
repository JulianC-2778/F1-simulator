import unittest

from fastapi.testclient import TestClient

from midware import runtime
from midware.app import create_app


class BotHeartbeatIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(create_app())
        cls.client = cls.context.__enter__()
        cls.client.post(
            "/api/features/enabled",
            json={"enabled": ["commentary", "engineer", "coach", "bot"]},
        )

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def test_server_receive_time_and_expiration(self):
        response = self.client.post(
            "/api/bot/status",
            json={
                "connected": True,
                "strategy": "NORMAL",
                "speed_kmh": 100,
                "gear": 3,
                "details": {"client_updated_at": -999999},
            },
        )
        self.assertEqual(response.status_code, 200)
        status = response.json()["status"]
        self.assertEqual(status["health"], "healthy")
        received_at = status["received_at"]
        expired = runtime.bot_status_service.snapshot(now=received_at + 6.0)
        self.assertEqual(expired.health, "disconnected")
        self.assertFalse(expired.active)

    def test_disconnected_update_is_accepted_while_bot_disabled(self):
        self.client.post(
            "/api/features/enabled",
            json={"enabled": ["commentary", "engineer", "coach"]},
        )
        disconnected = self.client.post(
            "/api/bot/status",
            json={"connected": False, "strategy": "NORMAL"},
        )
        self.assertEqual(disconnected.status_code, 200)
        connected = self.client.post(
            "/api/bot/status",
            json={"connected": True, "strategy": "NORMAL"},
        )
        self.assertEqual(connected.status_code, 409)
        self.client.post(
            "/api/features/enabled",
            json={"enabled": ["commentary", "engineer", "coach", "bot"]},
        )
