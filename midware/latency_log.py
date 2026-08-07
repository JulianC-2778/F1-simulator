"""Opt-in, structured latency logging for the t0-t5 commentary pipeline
stages defined in docs/commentary_test_plan.md work package C (section 7.1).

Disabled by default -- `LatencyLog()` with no arguments is a no-op, so
importing/instantiating it changes nothing about production behaviour
unless a caller explicitly turns it on (see runtime.py's `latency_log`
instance and `/api/commentary/config` -- flip it on via
LATENCY_LOG_ENABLED=1 in the environment, not through a code change).

t0_telemetry_received, t1_event_detected, t2_first_token and t3_ai_done are
backend-observable and wired into midware/runtime.py:
  - t0_telemetry_received is written once per *detected event*, not once per
    frame, so the UDP hot path (30-60 frames/s) stays free of writes. Its
    value is the triggering frame's own `_received_at` wall-clock stamp
    converted onto this module's monotonic clock -- see
    runtime.py::_frame_t0_monotonic. Do not substitute the frame's
    `sim_time`: that is TORCS simulation time, a third clock unrelated to
    both, and subtracting it from t1 yields a meaningless number.
  - t4_caption_displayed / t5_tts_started: happen in the browser dashboard,
    not the backend. Both `ai_done` and `tts_audio` carry the same
    `request_id` over the WebSocket, so a listener can time their arrival and
    join on request_id -- see
    evaluation/commentary/scripts/capture_display_latency.py.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock


class LatencyLog:
    def __init__(self, path: str | Path | None = None, enabled: bool | None = None):
        self.enabled = bool(os.environ.get("COMMENTARY_LATENCY_LOG")) if enabled is None else enabled
        self.path = Path(path or os.environ.get("COMMENTARY_LATENCY_LOG_PATH", "commentary_latency.jsonl"))
        self._lock = Lock()
        self._seen_first_token: set[str] = set()

    def record(
        self,
        request_id: str,
        stage: str,
        *,
        event_id: str = "",
        session_id: str = "",
        timestamp: float | None = None,
    ) -> None:
        """Append one (request_id, stage) row.

        `timestamp` defaults to "now" on the `time.monotonic()` clock. Pass it
        explicitly only for a stage that happened *before* this call and whose
        time is known from another source -- t0_telemetry_received is the one
        such stage, since a frame's arrival is stamped on the UDP path with
        `time.time()` and has to be converted onto this clock before it can be
        compared with t1 (see runtime.py::_frame_t0_monotonic). Every value in
        the file must share one clock or the differences are meaningless.
        """
        if not self.enabled or not request_id:
            return
        if stage == "t2_first_token":
            with self._lock:
                if request_id in self._seen_first_token:
                    return
                self._seen_first_token.add(request_id)
        row = {
            "request_id": request_id,
            "event_id": event_id,
            "session_id": session_id,
            "stage": stage,
            "timestamp": time.monotonic() if timestamp is None else float(timestamp),
        }
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    def forget(self, request_id: str) -> None:
        """Drop first-token bookkeeping for a finished/cancelled request so
        the in-memory set doesn't grow unboundedly over a long session."""
        with self._lock:
            self._seen_first_token.discard(request_id)
