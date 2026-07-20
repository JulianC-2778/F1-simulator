import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from midware import runtime
from midware.app import create_app


ALL_FEATURES = ["commentary", "engineer", "coach", "bot"]


class FeatureApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(create_app())
        cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.client.post("/api/features/enabled", json={"enabled": ALL_FEATURES})

    def test_engineer_api_and_history_use_model_path(self):
        with patch.object(runtime, "call_model_for_feature", AsyncMock(return_value="Brake earlier.")) as model:
            response = self.client.post(
                "/api/engineer/ask",
                json={"question": "What should I change?", "car_state": {"speed": 120}},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "Brake earlier.")
        model.assert_awaited_once()
        self.assertGreater(len(self.client.get("/api/engineer/history").json()["messages"]), 0)
        self.assertEqual(self.client.post("/api/engineer/clear").status_code, 200)

    def test_each_disabled_feature_changes_real_api_behavior(self):
        cases = {
            "commentary": ("post", "/api/commentary/manual", {"prompt": "test"}),
            "engineer": ("post", "/api/engineer/ask", {"question": "test"}),
            "coach": ("get", "/api/coach/dashboard", None),
            "bot": ("post", "/api/bot/strategy", {"sensor_state": {}}),
        }
        for feature, (method, path, body) in cases.items():
            with self.subTest(feature=feature):
                enabled = [name for name in ALL_FEATURES if name != feature]
                self.client.post("/api/features/enabled", json={"enabled": enabled})
                response = getattr(self.client, method)(path, json=body) if body is not None else getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 409)
                state = next(
                    item for item in self.client.get("/api/features/status").json()["features"]
                    if item["name"] == feature
                )
                self.assertFalse(state["enabled"])
                self.assertFalse(state["active"])
                self.client.post("/api/features/enabled", json={"enabled": ALL_FEATURES})

    def test_bot_strategy_api_validates_broker_result(self):
        with patch.object(
            runtime,
            "call_model_for_feature",
            AsyncMock(return_value='{"strategy":"SAVE_FUEL","reason":"low fuel"}'),
        ):
            response = self.client.post("/api/bot/strategy", json={"sensor_state": {"fuel": 12}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"]["strategy"], "SAVE_FUEL")

    def test_manual_commentary_queues_when_enabled(self):
        with patch.object(runtime, "generate_commentary", AsyncMock(return_value="ok")) as generate:
            response = self.client.post("/api/commentary/manual", json={"prompt": "manual"})
            self.assertEqual(response.status_code, 200)
            asyncio.run(asyncio.sleep(0))
        self.assertTrue(generate.called)
