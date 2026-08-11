#!/usr/bin/env python3
"""
Continuous car_state pipeline checker for a real TORCS driving session.

Every second: reads the live raw telemetry from midware's /api/telemetry,
runs it through the real race_analyzer.telemetry_to_car_state() (the actual
code being tested), and independently re-derives what the answer should be
(expected_rules.py, written separately from the docs -- see that file's
docstring for why this counts as an independent check and not the code
compared against itself). Every sample is appended to a CSV immediately, so
nothing is lost if the process is killed instead of stopped cleanly.

Requires midware running (TORCS itself is optional -- this only needs live
telemetry flowing, i.e. you actually driving).

Run:
    python3 tools/car_state_pipeline_check/live_driving_logger.py

Stop with Ctrl+C when you're done driving -- it prints a summary and the
exact file path when it exits.
"""

from __future__ import annotations

import csv
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from race_analyzer import telemetry_to_car_state  # noqa: E402
from expected_rules import expected_pipeline_output  # noqa: E402

POLL_SECONDS = 1.0
BASE_URL = config.MIDWARE_BASE_URL
# Results go outside the shared project repo (local machine only, not
# something to commit) -- one timestamped file per run, never overwritten.
LOCAL_RESULTS_DIR = Path("/mnt/c/Users/22494/测试数据")
FIELDNAMES = [
    "timestamp", "raw_speedX", "raw_trackPos", "raw_damage", "raw_fuel", "raw_rpm", "raw_gear", "raw_curLapTime",
    "actual_problems", "expected_problems", "match",
]


def fetch_telemetry() -> dict | None:
    req = urllib.request.Request(f"{BASE_URL}/api/telemetry", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"  [warning] couldn't reach midware ({exc}); retrying...")
        return None
    return payload.get("telemetry")


def main() -> None:
    LOCAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LOCAL_RESULTS_DIR / f"live_driving_log_{datetime.now():%Y%m%d_%H%M%S}.csv"

    print(f"Polling {BASE_URL}/api/telemetry every {POLL_SECONDS:.0f}s.")
    print(f"Saving to: {out_path}")
    print("Go drive. Press Ctrl+C here when you're done.\n")

    total = 0
    mismatches = 0

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        f.flush()

        try:
            while True:
                raw = fetch_telemetry()
                if not raw:
                    print("  [waiting] no live telemetry yet -- get into the car and start driving.")
                    time.sleep(POLL_SECONDS)
                    continue

                actual = telemetry_to_car_state(raw)
                expected = expected_pipeline_output(raw)
                ok = actual == expected
                total += 1
                if not ok:
                    mismatches += 1

                writer.writerow({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "raw_speedX": raw.get("speedX"),
                    "raw_trackPos": raw.get("trackPos"),
                    "raw_damage": raw.get("damage"),
                    "raw_fuel": raw.get("fuel"),
                    "raw_rpm": raw.get("rpm"),
                    "raw_gear": raw.get("gear"),
                    "raw_curLapTime": raw.get("curLapTime"),
                    "actual_problems": ";".join(actual.get("problems", [])),
                    "expected_problems": ";".join(expected.get("problems", [])),
                    "match": "PASS" if ok else "FAIL",
                })
                f.flush()

                marker = "OK  " if ok else "FAIL"
                print(
                    f"  [{total:4d}] {marker} speed={raw.get('speedX', 0):.0f} "
                    f"track_pos={raw.get('trackPos', 0):.2f} damage={raw.get('damage', 0):.0f} "
                    f"fuel={raw.get('fuel', 0):.1f} -> {actual.get('problems')}"
                    + ("" if ok else f"  (expected {expected.get('problems')})"),
                    flush=True,
                )
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            pass

    print()
    print(f"Stopped. Recorded {total} samples, {total - mismatches} passed, {mismatches} failed.")
    print(f"Full log saved to: {out_path}")


if __name__ == "__main__":
    main()
