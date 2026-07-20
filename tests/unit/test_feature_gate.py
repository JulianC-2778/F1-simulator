import unittest

from midware.services.feature_gate import FeatureGate
from midware.shared.feature_registry import feature_specs


class FeatureGateTests(unittest.TestCase):
    def test_runtime_refresh_cannot_reenable_user_setting(self):
        gate = FeatureGate(feature_specs())
        gate.set_enabled(["commentary", "coach", "bot"])
        gate.update("engineer", active=True, healthy=True)
        engineer = next(item for item in gate.status() if item["name"] == "engineer")
        self.assertFalse(engineer["enabled"])
        self.assertFalse(engineer["active"])

    def test_empty_combination_is_valid(self):
        gate = FeatureGate(feature_specs())
        gate.set_enabled([])
        self.assertTrue(gate.combination()["supported"])
