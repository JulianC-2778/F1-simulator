"""Opt-in, structured latency logging for the t0-t5 commentary pipeline
stages defined in docs/commentary_test_plan.md work package C (section 7.1).

Disabled by default -- `LatencyLog()` with no arguments is a no-op, so
importing/instantiating it changes nothing about production behaviour
unless a caller explicitly turns it on (see runtime.py's `latency_log`
instance and `/api/commentary/config` -- flip it on via
LATENCY_LOG_ENABLED=1 in the environment, not through a code change).

Only t1_event_detected, t2_first_token and t3_ai_done are backend-
observable and wired into midware/runtime.py:
  - t0_telemetry_received: already present on every frame as `sim_time`,
    not re-logged here to avoid adding a write on the UDP hot path
    (30-60 frames/s) for an opt-in feature.
  - t4_caption_displayed / t5_tts_started: happen in the browser dashboard,
    not the backend -- see docs/commentary_experiment_protocol.md for how
    a real work-package-C run captures those two manually/via browser logs.
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

    def record(self, request_id: str, stage: str, *, event_id: str = "", session_id: str = "") -> None:
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
            "timestamp": time.monotonic(),
        }
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    def forget(self, request_id: str) -> None:
        """Drop first-token bookkeeping for a finished/cancelled request so
        the in-memory set doesn't grow unboundedly over a long session."""
        with self._lock:
            self._seen_first_token.discard(request_id)
