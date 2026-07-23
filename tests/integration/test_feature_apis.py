import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from midware import runtime
from midware.app import create_app
from tests.fixtures.telemetry_frames import RAW_TORCS_FRAME


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

    def test_coach_prebrief_and_dashboard_expose_lookahead(self):
        profiles = self.client.get("/api/coach/track-profiles")
        self.assertEqual(profiles.status_code, 200)
        self.assertGreater(len(profiles.json()["profiles"]), 0)

        prebrief = self.client.post(
            "/api/coach/prebrief",
            json={
                "track_id": "default-road",
                "driver_style": "late_braker",
                "road_condition": "low_grip",
                "use_model": False,
            },
        )
        self.assertEqual(prebrief.status_code, 200)
        self.assertEqual(prebrief.json()["pre_race_briefing"]["status"], "ready")
        self.assertGreater(len(prebrief.json()["lookahead_plan"]), 0)

        self.client.post("/api/telemetry/push", json={"telemetry": RAW_TORCS_FRAME})
        dashboard = self.client.get(
            "/api/coach/dashboard?track_id=default-road&driver_style=late_braker&road_condition=low_grip"
        )
        self.assertEqual(dashboard.status_code, 200)
        payload = dashboard.json()
        self.assertTrue(payload["status"]["has_telemetry"])
        self.assertGreater(len(payload["lookahead_plan"]), 0)

    def test_coach_prebrief_parses_fenced_model_json(self):
        with patch.object(
            runtime,
            "call_model_for_feature",
            AsyncMock(
                return_value='```json\n{"brief":"Brake earlier into sector one.","focus":["entry"],"risk":"entry overspeed"}\n```'
            ),
        ):
            response = self.client.post(
                "/api/coach/prebrief",
                json={
                    "track_id": "default-road",
                    "driver_style": "late_braker",
                    "road_condition": "low_grip",
                    "use_model": True,
                },
            )
        self.assertEqual(response.status_code, 200)
        supplement = response.json()["pre_race_briefing"]["model_supplement"]
        self.assertEqual(supplement["status"], "ready")
        self.assertEqual(supplement["text"], "Brake earlier into sector one.")
        self.assertEqual(supplement["focus"], ["entry"])
