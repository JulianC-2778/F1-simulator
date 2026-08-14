#!/usr/bin/env python3
"""Compute end-to-end latency statistics from a control_latency CSV (schema:
evaluation/bot/schemas/csv_schemas.py::CONTROL_LATENCY_SCHEMA, template:
evaluation/bot/templates/control_latency_template.csv) -- bot_test_plan.md
work package C, sections 6.1-6.4.

Two independent chains live in one file, discriminated by which timestamp
columns a row has populated (the `kind` column is metadata for humans, not
read by this script):

    Control loop (per real frame, hard real-time):
        compute_latency = u1_control_computed - u0_scr_state_received
        send_latency     = u2_control_sent - u1_control_computed
        frame_latency     = u2_control_sent - u0_scr_state_received

    Granite strategy call (every _STRATEGY_INTERVAL seconds, soft real-time):
        granite_rtt         = g2_response_complete - g0_state_snapshot
        debounce_overhead   = g3_strategy_applied - g2_response_complete

bot_test_plan.md 6.2 notes the control-loop chain's `compute_latency` does
not need real TORCS -- it can be measured by driving `compute_control`
directly through 1000+ synthetic frames (see
tests/bot/test_control_latency_l2.py) and exported into this same CSV
schema; `send_latency`/`frame_latency` and the Granite RTT chain still need
a real (or faked-over-real-UDP) `scr_server` / midware + Granite round trip.

A row contributes to a metric's sample only if both endpoints are present,
finite, non-negative and in order (end >= start); otherwise it counts as a
failure for that metric specifically, mirroring
evaluation/commentary/scripts/analyse_latency.py's rule -- rows explicitly
marked failed=true always count as a failure for every timed metric in that
row.

Usage:
    python analyse_control_latency.py --file path/to/control_latency.csv [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from latency_stats import Stats  # noqa: E402

METRICS = [
    ("Control loop (compute)", "u0_scr_state_received", "u1_control_computed"),
    ("Control loop (send, UDP)", "u1_control_computed", "u2_control_sent"),
    ("Control loop (frame, total)", "u0_scr_state_received", "u2_control_sent"),
    ("Granite strategy RTT", "g0_state_snapshot", "g2_response_complete"),
    ("Granite debounce overhead", "g2_response_complete", "g3_strategy_applied"),
]


def _to_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def load_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def compute_stats(rows: list[dict]) -> dict[str, Stats]:
    results: dict[str, Stats] = {}
    for label, start_col, end_col in METRICS:
        samples: list[float] = []
        failures = 0
        for row in rows:
            # One row is either a "frame" reading or a "granite_rtt"
            # reading, not one request going through every stage the way a
            # commentary latency row does -- so failed=true on a
            # granite_rtt row must not inflate the control-loop failure
            # count (and vice versa). applies_to_this_metric mirrors the
            # same kind check used below for missing timestamps.
            kind = str(row.get("kind", "")).strip()
            applies_to_this_metric = (
                (start_col.startswith("u") and kind == "frame")
                or (start_col.startswith("g") and kind == "granite_rtt")
            )
            if not applies_to_this_metric:
                continue
            if str(row.get("failed", "")).strip().lower() in ("true", "1", "yes"):
                failures += 1
                continue
            start, end = _to_float(row.get(start_col)), _to_float(row.get(end_col))
            if start is None or end is None:
                # A row that claims this metric's kind but is missing the
                # timestamp is a genuine capture failure, not "no sample".
                failures += 1
                continue
            if end < start:
                failures += 1
                continue
            samples.append(end - start)
        results[label] = Stats.from_samples(samples, failures)
    return results


def render_table(results: dict[str, Stats]) -> str:
    lines = ["| Stage | N | Median | P95 | Maximum | Failures |", "|---|---:|---:|---:|---:|---:|"]
    for label, _, _ in METRICS:
        lines.append("| " + " | ".join(results[label].row(label)) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    if not args.file.exists():
        print(f"file not found: {args.file}", file=sys.stderr)
        return 2

    rows = load_rows(args.file)
    if not rows:
        print("file has a header but zero data rows (empty dataset)", file=sys.stderr)
        return 1

    results = compute_stats(rows)
    table = render_table(results)
    print(table)

    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        out_path = args.out_dir / "control_latency_summary.md"
        out_path.write_text(table + "\n", encoding="utf-8")
        print(f"\nWrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
