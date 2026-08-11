import unittest

from midware.feature2_core import build_dashboard_payload
from midware.shared.track_profiles import build_track_context, load_track_profile
from tests.fixtures.telemetry_frames import RAW_TORCS_FRAME


def telemetry_frame(**overrides):
    frame = dict(RAW_TORCS_FRAME)
    frame.update({key: str(value) for key, value in overrides.items()})
    return frame


def default_track_context(*, dist_from_start=390.0, speed_kmh=180.0, road_condition="dry"):
    profile = load_track_profile("default-road")
    return build_track_context(
        profile,
        dist_from_start=dist_from_start,
        speed_kmh=speed_kmh,
        road_condition=road_condition,
    )


class Feature2CoreTests(unittest.TestCase):
    def test_empty_dashboard_keeps_prebrief_available_with_track_profile(self):
        context = default_track_context(road_condition="wet")
        payload = build_dashboard_payload(
            [],
            track_context=context,
            driver_style="late_braker",
            road_condition="wet",
        )

        self.assertFalse(payload["status"]["has_telemetry"])
        self.assertEqual(payload["status"]["session_state"], "waiting")
        self.assertIsNone(payload["guidance"])
        self.assertEqual(payload["driver_profile"]["id"], "late_braker")
        self.assertEqual(payload["road_condition"]["id"], "wet")
        self.assertEqual(payload["pre_race_briefing"]["status"], "ready")
        self.assertGreater(len(payload["lookahead_plan"]), 0)

    def test_map_overspeed_drives_braking_guidance_and_priority_issue(self):
        context = default_track_context(dist_from_start=390.0, speed_kmh=180.0)
        payload = build_dashboard_payload(
            [telemetry_frame(speedX=180.0, brake=0.0, distFromStart=390.0)],
            track_context=context,
            driver_style="late_braker",
            road_condition="dry",
        )

        self.assertTrue(payload["status"]["has_telemetry"])
        self.assertEqual(payload["guidance"]["state_id"], "mapped_braking_setup")
        self.assertEqual(payload["guidance"]["focus_area"], "braking")
        self.assertEqual(payload["guidance"]["priority"], "high")
        self.assertEqual(payload["guidance"]["priority_issues"][0]["label"], "Mapped braking target")
        self.assertEqual(payload["guidance"]["priority_issues"][0]["severity"], "high")
        self.assertEqual(payload["_overlay_request"]["state_id"], payload["guidance"]["state_id"])
        self.assertTrue(payload["_overlay_cache_key"].startswith("mapped_braking_setup|braking|high|"))

        signals = {item["label"]: item for item in payload["signals"]}
        self.assertEqual(signals["Map Limit"]["tone"], "danger")

    def test_off_track_frame_takes_recovery_priority_without_map_context(self):
        payload = build_dashboard_payload(
            [telemetry_frame(trackPos=1.15, throttle=0.85, speedX=125.0)],
            driver_style="auto",
            road_condition="dry",
        )

        self.assertEqual(payload["guidance"]["state_id"], "off_track_recovery")
        self.assertEqual(payload["guidance"]["priority"], "high")
        self.assertEqual(payload["guidance"]["priority_issues"][0]["label"], "Track limits exceeded")

        signals = {item["label"]: item for item in payload["signals"]}
        self.assertEqual(signals["Track Limit"]["tone"], "danger")
        self.assertEqual(signals["Track Map"]["display"], "sensor-only")

    def test_history_series_respects_requested_history_window(self):
        frames = [
            telemetry_frame(seq=1, sim_time=0.0, speedX=100.0),
            telemetry_frame(seq=2, sim_time=5.0, speedX=140.0),
            telemetry_frame(seq=3, sim_time=12.0, speedX=180.0),
        ]

        payload = build_dashboard_payload(frames, window_seconds=6.0, history_seconds=8.0)

        self.assertEqual(payload["status"]["frame_count"], 2)
        self.assertEqual([point["value"] for point in payload["history"]["speed_x"]], [140.0, 180.0])
        self.assertEqual(payload["latest_state"]["seq"], 3)


if __name__ == "__main__":
    unittest.main()
