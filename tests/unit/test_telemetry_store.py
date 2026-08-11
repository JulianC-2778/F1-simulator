"""Regression test for a real defect found during work package D endurance
testing: TelemetryStore._should_reset_session_locked()'s reset branch clears
_frames/_latest_frame/_session_id on a session restart (e.g. the player
restarting a race in TORCS) but leaves _last_progress_sim_time/_last_progress_at
pointing at the *previous* session's sim_time. Since the new session's
sim_time starts back near zero, it can take a long time (or the whole
session, for a short race) to climb back past the stale leftover threshold,
so status()'s "is telemetry advancing" check reports is_stale=True with
reason "not advancing sim_time" indefinitely after every restart, even
though fresh, progressing telemetry is arriving. Confirmed live: after ~20
race restarts in one endurance run, is_stale stayed stuck True for the rest
of the session. See docs/commentary_endurance_test_protocol.md.
"""

import unittest
from unittest.mock import patch

from midware.telemetry import TelemetryStore


class SessionResetProgressTrackingTests(unittest.TestCase):
    def _push(self, store, now, sim_time, lap=1):
        with patch("midware.telemetry.time.time", return_value=now):
            store.push({"sim_time": sim_time, "lap": lap})

    def test_progress_tracking_resets_when_a_new_session_starts(self):
        store = TelemetryStore()

        # First session: telemetry arrives and progresses normally.
        self._push(store, now=1000.0, sim_time=50.0)

        # Player restarts the race in TORCS: sim_time rolls back by more
        # than SESSION_RESET_TIME_ROLLBACK_SECONDS, which is exactly the
        # condition that triggers a session reset.
        self._push(store, now=1010.0, sim_time=0.5)

        # New session's telemetry keeps arriving and progressing (0.5 -> 3.0),
        # each push a few seconds after the last -- real, fresh telemetry.
        self._push(store, now=1013.0, sim_time=3.0)

        with patch("midware.telemetry.time.time", return_value=1013.5):
            status = store.status(stale_after_seconds=3.0)

        self.assertEqual(status["session_id"], 2, "session should have reset")
        self.assertFalse(
            status["is_stale"],
            f"telemetry is fresh and progressing within the new session, but "
            f"is_stale=True (reason={status['stale_reason']!r}) -- "
            f"_last_progress_at/_last_progress_sim_time were not reset "
            f"alongside _frames/_latest_frame/_session_id on session restart",
        )

    def test_progress_timestamp_is_fresh_immediately_after_a_session_reset(self):
        store = TelemetryStore()
        self._push(store, now=1000.0, sim_time=80.0)
        self._push(store, now=1010.0, sim_time=0.2)

        with patch("midware.telemetry.time.time", return_value=1010.1):
            status = store.status(stale_after_seconds=3.0)

        self.assertEqual(
            status["last_progress_at"], 1010.0,
            "the new session's first frame should establish a fresh progress "
            "baseline immediately, not inherit the previous session's",
        )


if __name__ == "__main__":
    unittest.main()
