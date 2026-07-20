import asyncio
import unittest
from unittest.mock import patch

from midware import runtime
from midware.schemas.bot import Strategy, StrategyDecision


class StrategyDecisionTests(unittest.TestCase):
    def test_strategy_is_closed_enum(self):
        self.assertEqual(StrategyDecision(strategy="ATTACK").strategy, Strategy.ATTACK)
        with self.assertRaises(Exception):
            StrategyDecision(strategy="TURBO")

    def test_bot_strategy_endpoint_validates_model_result(self):
        async def fake_call(*args, **kwargs):
            return '{"strategy":"DEFEND","reason":"damage"}'

        runtime.runtime_manager.set_enabled(["commentary", "engineer", "coach", "bot"])
        with patch.object(runtime, "call_model_for_feature", fake_call):
            result = asyncio.run(runtime.request_bot_strategy({"sensor_state": {"damage": 5000}}))
        self.assertEqual(result["decision"]["strategy"], "DEFEND")
