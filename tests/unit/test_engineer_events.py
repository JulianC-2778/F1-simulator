#!/usr/bin/env python3
"""Unit tests for engineer_events.py (Direction 1 addition: incident memory)."""

import unittest

from engineer_events import DAMAGE_JUMP_THRESHOLD, MAX_RECENT_EVENTS, IncidentTracker


class ControlledClock:
    """Fake monotonic clock: advance() moves it forward by a fixed amount."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self) -> float:
        return self._now


def _car_state(**overrides) -> dict:
    base = {"track_pos": 0.0, "damage": 0.0}
    base.update(overrides)
    return base


class IncidentTrackerTests(unittest.TestCase):
    def test_starts_with_no_events(self):
        tracker = IncidentTracker(clock=ControlledClock())
        self.assertEqual(tracker.recent_events, [])

    def test_first_update_seeds_state_without_recording_an_event(self):
        # Whatever the car is doing when questions start isn't a "new"
        # incident -- even if it's already off track or already damaged.
        tracker = IncidentTracker(clock=ControlledClock())
        tracker.update(_car_state(track_pos=1.5, damage=500.0))
        self.assertEqual(tracker.recent_events, [])

    def test_going_off_track_records_one_event(self):
        clock = ControlledClock()
        tracker = IncidentTracker(clock=clock)
        tracker.update(_car_state(track_pos=0.0))
        clock.advance(1.0)
        tracker.update(_car_state(track_pos=1.2))

        events = tracker.recent_events
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "off_track")

    def test_staying_off_track_does_not_record_repeated_events(self):
        clock = ControlledClock()
        tracker = IncidentTracker(clock=clock)
        tracker.update(_car_state(track_pos=0.0))
        clock.advance(1.0)
        tracker.update(_car_state(track_pos=1.2))
        clock.advance(1.0)
        tracker.update(_car_state(track_pos=1.3))
        clock.advance(1.0)
        tracker.update(_car_state(track_pos=1.4))

        self.assertEqual(len(tracker.recent_events), 1)

    def test_going_off_track_again_after_returning_records_a_new_event(self):
        clock = ControlledClock()
        tracker = IncidentTracker(clock=clock)
        tracker.update(_car_state(track_pos=0.0))
        clock.advance(1.0)
        tracker.update(_car_state(track_pos=1.2))  # off track: event 1
        clock.advance(1.0)
        tracker.update(_car_state(track_pos=0.0))  # back on track
        clock.advance(1.0)
        tracker.update(_car_state(track_pos=-1.1))  # off track again: event 2

        events = tracker.recent_events
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["type"] == "off_track" for e in events))

    def test_damage_increase_at_or_above_threshold_records_an_event(self):
        clock = ControlledClock()
        tracker = IncidentTracker(clock=clock)
        tracker.update(_car_state(damage=0.0))
        clock.advance(1.0)
        tracker.update(_car_state(damage=DAMAGE_JUMP_THRESHOLD))

        events = tracker.recent_events
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "damage")

    def test_small_damage_increase_below_threshold_is_not_recorded(self):
        clock = ControlledClock()
        tracker = IncidentTracker(clock=clock)
        tracker.update(_car_state(damage=0.0))
        clock.advance(1.0)
        tracker.update(_car_state(damage=DAMAGE_JUMP_THRESHOLD - 1))

        self.assertEqual(tracker.recent_events, [])

    def test_damage_decrease_is_not_recorded_as_an_incident(self):
        # e.g. a repair between sessions/laps -- not something to report.
        clock = ControlledClock()
        tracker = IncidentTracker(clock=clock)
        tracker.update(_car_state(damage=1000.0))
        clock.advance(1.0)
        tracker.update(_car_state(damage=0.0))

        self.assertEqual(tracker.recent_events, [])

    def test_seconds_ago_reflects_elapsed_clock_time(self):
        clock = ControlledClock()
        tracker = IncidentTracker(clock=clock)
        tracker.update(_car_state(track_pos=0.0))
        clock.advance(1.0)
        tracker.update(_car_state(track_pos=1.2))
        clock.advance(30.0)

        events = tracker.recent_events
        self.assertAlmostEqual(events[0]["seconds_ago"], 30.0, places=1)

    def test_recent_events_are_returned_newest_first(self):
        clock = ControlledClock()
        tracker = IncidentTracker(clock=clock)
        tracker.update(_car_state(track_pos=0.0, damage=0.0))
        clock.advance(1.0)
        tracker.update(_car_state(track_pos=1.2, damage=0.0))  # off_track first
        clock.advance(1.0)
        tracker.update(_car_state(track_pos=1.2, damage=DAMAGE_JUMP_THRESHOLD))  # damage second

        events = tracker.recent_events
        self.assertEqual([e["type"] for e in events], ["damage", "off_track"])

    def test_event_list_is_capped_at_max_recent_events(self):
        clock = ControlledClock()
        tracker = IncidentTracker(clock=clock)
        tracker.update(_car_state(track_pos=0.0, damage=0.0))

        # Alternate on/off track to generate more than the cap in edge-triggered events.
        for i in range(MAX_RECENT_EVENTS + 5):
            clock.advance(1.0)
            tracker.update(_car_state(track_pos=1.2 if i % 2 == 0 else 0.0, damage=0.0))

        self.assertLessEqual(len(tracker.recent_events), MAX_RECENT_EVENTS)


if __name__ == "__main__":
    unittest.main()
