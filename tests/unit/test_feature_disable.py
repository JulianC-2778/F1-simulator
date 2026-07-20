import asyncio
import unittest

from midware import commentary


class FeatureDisableTests(unittest.TestCase):
    def tearDown(self):
        commentary.runtime_manager.set_enabled(["commentary", "engineer", "coach", "bot"])

    def test_engineer_disabled_returns_409_before_model_call(self):
        commentary.runtime_manager.set_enabled(["commentary", "coach", "bot"])
        response = asyncio.run(commentary.ask_engineer({"question": "status?"}))
        self.assertEqual(response.status_code, 409)

    def test_commentary_and_coach_disabled_return_409(self):
        commentary.runtime_manager.set_enabled(["engineer", "bot"])
        commentary_response = asyncio.run(commentary.manual_commentary({"prompt": "test"}))
        coach_response = asyncio.run(commentary.get_coach_dashboard())
        self.assertEqual(commentary_response.status_code, 409)
        self.assertEqual(coach_response.status_code, 409)
