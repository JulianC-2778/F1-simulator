import unittest

from midware.feature2_core import (
    build_dashboard_payload,
    build_lookahead_plan,
    build_pre_race_briefing,
    driver_style_profile,
    road_condition_profile,
)
from midware.shared.track_profiles import build_track_context, load_track_profile
from midware.telemetry import to_common_frame
from tests.fixtures.telemetry_frames import RAW_TORCS_FRAME


class CoachPrebriefTests(unittest.TestCase):
    def test_prebrief_uses_track_driver_style_and_road_condition(self):
        profile = load_track_profile("default-road")
        context = build_track_context(
            profile,
            dist_from_start=390.0,
            speed_kmh=215.0,
            road_condition="low_grip",
        )
        frames = [to_common_frame({**RAW_TORCS_FRAME, "distFromStart": str(390 + index * 8)}) for index in range(3)]
        driver = driver_style_profile(frames, "late_braker")
        road = road_condition_profile("low_grip")
        plan = build_lookahead_plan(context, driver, road)
        prebrief = build_pre_race_briefing(context, driver, road, plan)

        self.assertEqual(prebrief["status"], "ready")
        self.assertEqual(driver["id"], "late_braker")
        self.assertGreater(len(plan), 0)
        self.assertLess(plan[0]["target_speed_kmh"], context["next_corner"]["limit_kmh"])
        self.assertIn("lookahead_plan", prebrief)

    def test_dashboard_payload_carries_prebrief_and_lookahead(self):
        profile = load_track_profile("default-road")
        context = build_track_context(profile, dist_from_start=390.0, speed_kmh=215.0)
        payload = build_dashboard_payload(
            [RAW_TORCS_FRAME],
            track_context=context,
            driver_style="late_braker",
            road_condition="dry",
        )

        self.assertTrue(payload["status"]["has_telemetry"])
        self.assertEqual(payload["pre_race_briefing"]["status"], "ready")
        self.assertGreater(len(payload["lookahead_plan"]), 0)
        self.assertGreater(len(payload["guidance"]["lookahead_plan"]), 0)
