#!/usr/bin/env python3
"""Compute end-to-end latency statistics from a latency CSV (schema:
evaluation/commentary/schemas/csv_schemas.py::LATENCY_SCHEMA, template:
evaluation/commentary/templates/latency_template.csv) -- commentary_test_plan.md
work package C, sections 7.2-7.3.

    detection_latency   = t1 - t0
    first_token_latency = t2 - t1
    generation_latency  = t3 - t1
    caption_latency      = t4 - t0
    tts_latency           = t5 - t0   (only rows with t5 present)

A row contributes to a metric's sample only if both endpoints are present,
finite, non-negative and *in order* (end >= start); otherwise it counts as
a failure for that metric specifically (a request can fail generation but
still have a valid detection_latency, for example) -- explicit rows marked
failed=true always count as a failure for every timed metric, per
commentary_test_plan.md 7.2 ("failure and timeout must count toward
failures, not be silently dropped from the sample").

Usage:
    python analyse_latency.py --file path/to/latency.csv [--out-dir DIR]
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
    ("Event detection", "t0_telemetry_received", "t1_event_detected"),
    ("First model token", "t1_event_detected", "t2_first_token"),
    ("Complete model response", "t1_event_detected", "t3_ai_done"),
    ("Caption displayed", "t0_telemetry_received", "t4_caption_displayed"),
    ("TTS playback, if enabled", "t0_telemetry_received", "t5_tts_started"),
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
            if str(row.get("failed", "")).strip().lower() in ("true", "1", "yes"):
                failures += 1
                continue
            start, end = _to_float(row.get(start_col)), _to_float(row.get(end_col))
            if start is None or end is None:
                # Missing timestamp for this stage. TTS is opt-in (t5 is
                # legitimately absent when TTS is disabled) so a missing t5
                # is not a failure, just "no sample" -- every other stage
                # missing its timestamp is a genuine capture failure.
                if end_col != "t5_tts_started":
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
        out_path = args.out_dir / "latency_summary.md"
        out_path.write_text(table + "\n", encoding="utf-8")
        print(f"\nWrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
