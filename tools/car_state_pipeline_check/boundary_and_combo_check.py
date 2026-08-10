#!/usr/bin/env python3
"""
Static test battery for the car_state pipeline (race_analyzer.telemetry_to_car_state
+ analyze_car_state): boundary values, multi-problem combos, the fuel==0 edge
case, and malformed/missing raw fields.

Does not need TORCS or midware running -- calls race_analyzer.py's real
functions directly and compares against expected_rules.py's independently
derived answer. Finishes in under a second; run it any time.

Run:
    python3 tools/car_state_pipeline_check/boundary_and_combo_check.py
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

# Results go outside the shared project repo (local machine only, not
# something to commit) -- one timestamped file per run, never overwritten.
LOCAL_RESULTS_DIR = Path("/mnt/c/Users/22494/测试数据")

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from race_analyzer import telemetry_to_car_state  # noqa: E402
from expected_rules import expected_pipeline_output  # noqa: E402

BASE_RAW = {"speedX": 200.0, "rpm": 6000.0, "gear": 5, "trackPos": 0.1, "damage": 0.0, "fuel": 50.0, "curLapTime": 90.0}


def _raw(**overrides):
    merged = dict(BASE_RAW)
    merged.update(overrides)
    return merged


# (case_id, description, raw_telemetry)
CASES: list[tuple[str, str, dict]] = [
    # --- track_pos boundary (0.8 / 1.0, both signs) ---
    ("B01", "track_pos 0.79 (just under near-edge)", _raw(trackPos=0.79)),
    ("B02", "track_pos 0.81 (just over near-edge)", _raw(trackPos=0.81)),
    ("B03", "track_pos 1.01 (just over off-track)", _raw(trackPos=1.01)),
    ("B04", "track_pos -0.81 (near-edge, left side)", _raw(trackPos=-0.81)),
    ("B05", "track_pos -1.01 (off-track, left side)", _raw(trackPos=-1.01)),
    # --- damage boundary (1500 / 3000) ---
    ("B06", "damage 1499 (just under medium)", _raw(damage=1499.0)),
    ("B07", "damage 1501 (just over medium)", _raw(damage=1501.0)),
    ("B08", "damage exactly 3000 (should stay medium, not high)", _raw(damage=3000.0)),
    ("B09", "damage 3001 (just over high)", _raw(damage=3001.0)),
    # --- fuel boundary (0 / 8), including the fuel==0 gap ---
    ("B10", "fuel exactly 0.0 (known gap: rule is 0<fuel<8, excludes 0)", _raw(fuel=0.0)),
    ("B11", "fuel 0.01 (just above zero)", _raw(fuel=0.01)),
    ("B12", "fuel 7.9 (just under low-fuel ceiling)", _raw(fuel=7.9)),
    ("B13", "fuel exactly 8.0 (should NOT be low)", _raw(fuel=8.0)),
    # --- rpm boundary (2500 / 8500) ---
    ("B14", "rpm exactly 8500 (should NOT be too-high)", _raw(rpm=8500.0)),
    ("B15", "rpm 8501 (just over too-high)", _raw(rpm=8501.0)),
    ("B16", "rpm 2499 + gear 3 (too-low)", _raw(rpm=2499.0, gear=3)),
    ("B17", "rpm exactly 2500 + gear 3 (should NOT be too-low)", _raw(rpm=2500.0, gear=3)),
    ("B18", "rpm 2000 + gear 2 (gear not >2, should NOT trigger)", _raw(rpm=2000.0, gear=2)),
    # --- speed+gear boundary (80 / 3) ---
    ("B19", "speed 79 + gear 4 (gear-too-high)", _raw(speedX=79.0, gear=4)),
    ("B20", "speed exactly 80 + gear 4 (should NOT trigger)", _raw(speedX=80.0, gear=4)),
    ("B21", "speed 79 + gear 3 (gear not >3, should NOT trigger)", _raw(speedX=79.0, gear=3)),
    # --- multi-problem combos (priority/truncation logic) ---
    ("C01", "damage 3500 + track_pos 0.85 (top-2: damage high, near edge)",
     _raw(damage=3500.0, trackPos=0.85)),
    ("C02", "off-track + damage-high + fuel-low at once (only top-2 survive)",
     _raw(trackPos=1.2, damage=3500.0, fuel=5.0)),
    ("C03", "rpm-too-high + gear-too-high (both low severity, both fit in top-2)",
     _raw(rpm=9000.0, speedX=70.0, gear=4)),
    # --- malformed / missing raw fields ---
    ("M01", "damage field missing entirely", {k: v for k, v in BASE_RAW.items() if k != "damage"}),
    ("M02", "fuel is a string, not a number", _raw(fuel="low")),
    ("M03", "empty raw telemetry dict", {}),
    ("M04", "negative gear (reverse), low rpm (regression -- seen live, must stay normal)",
     _raw(rpm=2117.0, gear=-1, speedX=-19.0, trackPos=0.07)),
]


def run() -> int:
    rows = []
    mismatches = 0
    for case_id, description, raw in CASES:
        actual = telemetry_to_car_state(raw)
        expected = expected_pipeline_output(raw)
        ok = actual == expected
        if not ok:
            mismatches += 1
        rows.append({
            "case_id": case_id,
            "description": description,
            "raw": raw,
            "expected": expected,
            "actual": actual,
            "match": "PASS" if ok else "FAIL",
        })

    LOCAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LOCAL_RESULTS_DIR / f"boundary_and_combo_report_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "description", "raw", "expected", "actual", "match"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"{'ID':5} {'RESULT':6} DESCRIPTION")
    for row in rows:
        marker = "OK  " if row["match"] == "PASS" else "FAIL"
        print(f"{row['case_id']:5} {marker:6} {row['description']}")
        if row["match"] == "FAIL":
            print(f"      expected: {row['expected']}")
            print(f"      actual:   {row['actual']}")

    print()
    print(f"{len(rows)} cases, {len(rows) - mismatches} passed, {mismatches} failed.")
    print(f"Full report saved to: {out_path}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(run())
