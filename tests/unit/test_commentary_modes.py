"""Mode gating, same-cycle priority tie-break, cooldown, event dedup and
text dedup tests for `midware/commentary_engine.py`. See
docs/commentary_test_matrix.md rows: Modes, Priority/pre-emption (tie-break
half only -- actual asyncio task cancellation is integration-level, see
tests/integration/test_commentary_runtime.py), Cooldown, Event
deduplication, Text deduplication before display.
"""

import unittest
from unittest.mock import patch

from midware import commentary_engine as ce_module
from midware.commentary_engine import CommentaryConfig, CommentaryEngine


def base_frame(sim_time: float, **overrides) -> dict:
    frame = {
        "sim_time": sim_time,
        "lap": 1,
        "speed_x": 40.0,
        "gear": 3,
        "track_pos": 0.0,
        "damage": 0.0,
        "race_pos": 5,
        "fuel": 50.0,
        "throttle": 0.2,
        "brake": 0.0,
        "steer": 0.0,
        "angle": 0.0,
        "rpm": 5000.0,
        "dist_from_start": sim_time * 50.0,
        "cur_lap_time": sim_time,
        "last_lap_time": 0.0,
    }
    frame.update(overrides)
    return frame


class ControlledClock:
    """Deterministic stand-in for time.time(): each call advances by a fixed
    step, so wall-clock-gated logic (the global 1s cooldown in
    _can_emit_event) can be driven precisely without real sleeps."""

    def __init__(self, start: float = 1_000_000.0, step: float = 5.0):
        self.value = start
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class TestModeGating(unittest.TestCase):
    def test_off_mode_never_emits_even_with_a_clear_trigger(self):
        engine = CommentaryEngine(CommentaryConfig(mode="off"))
        engine.next_decision([base_frame(0.0, lap=1)])
        decision = engine.next_decision([base_frame(0.0, lap=1), base_frame(0.5, lap=2)])
        self.assertIsNone(decision)

    def test_interval_mode_blocks_event_triggers(self):
        engine = CommentaryEngine(CommentaryConfig(mode="interval"))
        engine.next_decision([base_frame(0.0, lap=1)])
        decision = engine.next_decision([base_frame(0.0, lap=1), base_frame(0.5, lap=2)])
        self.assertIsNone(decision)

    def test_interval_mode_allows_pace_update(self):
        engine = CommentaryEngine(CommentaryConfig(mode="interval", window_seconds=20.0))
        engine.next_decision([base_frame(0.0)])
        decision = engine.next_decision([base_frame(0.0), base_frame(10.0)])
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "pace_update")

    def test_event_mode_blocks_pace_update(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event", window_seconds=20.0))
        engine.next_decision([base_frame(0.0)])
        decision = engine.next_decision([base_frame(0.0), base_frame(10.0)])
        self.assertIsNone(decision)

    def test_event_mode_allows_event_triggers(self):
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        engine.next_decision([base_frame(0.0, lap=1)])
        decision = engine.next_decision([base_frame(0.0, lap=1), base_frame(0.5, lap=2)])
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "lap_complete")

    def test_hybrid_mode_allows_both(self):
        engine = CommentaryEngine(CommentaryConfig(mode="hybrid", window_seconds=20.0))
        engine.next_decision([base_frame(0.0, lap=1)])
        event_decision = engine.next_decision([base_frame(0.0, lap=1), base_frame(0.5, lap=2)])
        self.assertIsNotNone(event_decision)
        self.assertEqual(event_decision.event["event_type"], "lap_complete")

        engine2 = CommentaryEngine(CommentaryConfig(mode="hybrid", window_seconds=20.0))
        engine2.next_decision([base_frame(0.0)])
        pace_decision = engine2.next_decision([base_frame(0.0), base_frame(10.0)])
        self.assertIsNotNone(pace_decision)
        self.assertEqual(pace_decision.event["event_type"], "pace_update")


class TestPriorityTieBreak(unittest.TestCase):
    """Same detection cycle, multiple simultaneous candidates."""

    def test_higher_priority_candidate_wins_over_lower(self):
        # lap increments (priority 4, lap_complete) AND damage jumps
        # (priority 5, contact) in the same window -- contact must win.
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        engine.next_decision([base_frame(0.0, lap=1, damage=0.0)])
        decision = engine.next_decision([
            base_frame(0.0, lap=1, damage=0.0),
            base_frame(0.5, lap=2, damage=6.0),
        ])
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["event_type"], "contact")

    def test_same_priority_tie_break_is_stable_first_appended_wins(self):
        # position_change, contact and off_track are all priority 5. detect_event
        # appends candidates in source order (lap_complete, position_change,
        # contact, off_track, battle, pace_surge, pace_update) and its
        # max(..., key=priority) keeps the FIRST candidate reaching the max
        # value on ties (Python's max only replaces on strictly-greater) --
        # so among these three, position_change (appended first) must win.
        # This pins the actual undocumented tie-break policy rather than
        # assuming one; see docs/commentary_test_matrix.md.
        engine = CommentaryEngine(CommentaryConfig(mode="event"))
        engine.next_decision([base_frame(0.0, race_pos=5, damage=0.0, track_pos=0.0)])
        decision = engine.next_decision([
            base_frame(0.0, race_pos=5, damage=0.0, track_pos=0.0),
            base_frame(0.5, race_pos=4, damage=6.0, track_pos=1.5),
        ])
        self.assertIsNotNone(decision)
        self.assertEqual(decision.event["priority"], 5)
        self.assertEqual(decision.event["event_type"], "position_change")


class TestPerSignatureCooldown(unittest.TestCase):
    """EVENT_COOLDOWNS: same event *signature* (type+reason) must not
    re-fire within its type-specific cooldown window (sim-time based), and
    must be allowed again once the window has passed. The clock is patched
    so the *global* wall-clock cooldown in _can_emit_event (real time.time,
    1.0s by default) never interferes -- it's tested on its own below."""

    def test_same_signature_within_cooldown_is_blocked(self):
        with patch.object(ce_module.time, "time", ControlledClock()):
            engine = CommentaryEngine(CommentaryConfig(mode="event"))
            engine.next_decision([base_frame(0.0, damage=0.0)])
            first = engine.next_decision([base_frame(0.0, damage=0.0), base_frame(0.2, damage=6.0)])
            self.assertIsNotNone(first)
            self.assertEqual(first.event["event_type"], "contact")

            # contact's cooldown is 1.0s (sim time) -- 0.5s later, still the
            # same signature ("Damage jumped by 6.0"), must be blocked.
            second = engine.next_decision([base_frame(0.2, damage=6.0), base_frame(0.7, damage=12.0)])
            self.assertIsNone(second)

    def test_same_signature_after_cooldown_elapses_is_allowed(self):
        with patch.object(ce_module.time, "time", ControlledClock()):
            engine = CommentaryEngine(CommentaryConfig(mode="event"))
            engine.next_decision([base_frame(0.0, damage=0.0)])
            first = engine.next_decision([base_frame(0.0, damage=0.0), base_frame(0.2, damage=6.0)])
            self.assertIsNotNone(first)

            # 1.5s later (> contact's 1.0s cooldown), same-shaped delta
            # ("Damage jumped by 6.0" again) is allowed again.
            second = engine.next_decision([base_frame(0.2, damage=6.0), base_frame(1.7, damage=12.0)])
            self.assertIsNotNone(second)
            self.assertEqual(second.event["event_type"], "contact")


class TestGlobalWallClockCooldown(unittest.TestCase):
    """_can_emit_event's first check: no two events at all within
    config.event_cooldown (default 1.0s) of real wall-clock time, even if
    they're different event types/signatures."""

    def test_different_event_types_within_wall_clock_cooldown_are_blocked(self):
        # step=0.3s per time.time() call -- several calls happen per
        # next_decision (in _can_emit_event and in _run/emit bookkeeping),
        # so use a small step to stay well under the 1.0s cooldown between
        # the two decisions below.
        with patch.object(ce_module.time, "time", ControlledClock(step=0.1)):
            engine = CommentaryEngine(CommentaryConfig(mode="event"))
            engine.next_decision([base_frame(0.0, lap=1, race_pos=5)])
            first = engine.next_decision([
                base_frame(0.0, lap=1, race_pos=5),
                base_frame(0.5, lap=2, race_pos=5),
            ])
            self.assertIsNotNone(first)
            self.assertEqual(first.event["event_type"], "lap_complete")

            second = engine.next_decision([
                base_frame(0.5, lap=2, race_pos=5),
                base_frame(0.6, lap=2, race_pos=4),
            ])
            self.assertIsNone(second)

    def test_different_event_types_after_wall_clock_cooldown_are_allowed(self):
        with patch.object(ce_module.time, "time", ControlledClock(step=2.0)):
            engine = CommentaryEngine(CommentaryConfig(mode="event"))
            engine.next_decision([base_frame(0.0, lap=1, race_pos=5)])
            first = engine.next_decision([
                base_frame(0.0, lap=1, race_pos=5),
                base_frame(0.5, lap=2, race_pos=5),
            ])
            self.assertIsNotNone(first)

            second = engine.next_decision([
                base_frame(0.5, lap=2, race_pos=5),
                base_frame(0.6, lap=2, race_pos=4),
            ])
            self.assertIsNotNone(second)
            self.assertEqual(second.event["event_type"], "position_change")


class TestTextDedupeBeforeDisplay(unittest.TestCase):
    """should_emit_text() / normalize_text_key() -- pure-function half of
    the "text dedup before display" requirement. The end-to-end "does the
    WebSocket broadcast actually get suppressed" half is
    tests/integration/test_commentary_runtime.py::TestDedupeBeforeBroadcast."""

    def test_identical_text_within_window_is_suppressed(self):
        engine = CommentaryEngine(CommentaryConfig(dedupe_seconds=10.0))
        self.assertTrue(engine.should_emit_text("Battle for the lead!", sim_time=100.0))
        self.assertFalse(engine.should_emit_text("Battle for the lead!", sim_time=105.0))

    def test_identical_text_after_window_is_allowed_again(self):
        engine = CommentaryEngine(CommentaryConfig(dedupe_seconds=10.0))
        self.assertTrue(engine.should_emit_text("Battle for the lead!", sim_time=100.0))
        self.assertTrue(engine.should_emit_text("Battle for the lead!", sim_time=111.0))

    def test_different_text_is_never_suppressed(self):
        engine = CommentaryEngine(CommentaryConfig(dedupe_seconds=10.0))
        self.assertTrue(engine.should_emit_text("Battle for the lead!", sim_time=100.0))
        self.assertTrue(engine.should_emit_text("Off into the gravel!", sim_time=100.5))

    def test_near_duplicate_differing_only_in_case_and_punctuation_is_caught(self):
        # normalize_text_key() lowercases, strips punctuation and compares
        # only the first 12 words -- so this counts as the "same" text.
        engine = CommentaryEngine(CommentaryConfig(dedupe_seconds=10.0))
        self.assertTrue(engine.should_emit_text("Battle for the lead!!", sim_time=100.0))
        self.assertFalse(engine.should_emit_text("battle FOR the lead...", sim_time=100.2))

    def test_empty_text_is_always_allowed_and_not_tracked(self):
        engine = CommentaryEngine(CommentaryConfig(dedupe_seconds=10.0))
        self.assertTrue(engine.should_emit_text("", sim_time=100.0))
        self.assertTrue(engine.should_emit_text("", sim_time=100.0))
        self.assertEqual(engine.text_history, {})


if __name__ == "__main__":
    unittest.main()
