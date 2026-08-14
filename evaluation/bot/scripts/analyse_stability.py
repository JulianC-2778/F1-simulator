#!/usr/bin/env python3
"""Stability/endurance and fault-recovery summary tables --
bot_test_plan.md work package D (sections 7.1-7.2).

Two subcommands, matching the two CSV templates:

    python analyse_stability.py endurance --file stability_run.csv
    python analyse_stability.py faults --file fault_recovery.csv

`endurance` consumes evaluation/bot/schemas/csv_schemas.py::STABILITY_RUN_SCHEMA
(one row per endurance run) and reports strategy-request success rate
((strategy_requests - granite_failures) / strategy_requests * 100%, N/A if
there were zero requests -- never silently reported as 0%), plus the
safety_filter intervention breakdown by type -- bot_test_plan.md 7.1 calls
this out explicitly ("safety_filter interventions 按类型计数, 例如 PIT 强制
触发了几次") since it is the bot-specific stability signal commentary has no
analogue for: it measures how often the safety net had to override Granite,
not whether Granite itself succeeded.

`faults` consumes FAULT_RECOVERY_SCHEMA (one row per fault-injection trial,
same shape as commentary's, RB-01..RB-10 instead of RT-01..RT-12) and
reports, per fault_id: trial count, successful-recovery count, median
recovery time (first_success_after_restore_at_s - service_restored_at_s,
N/A for fault kinds that don't restore -- e.g. RB-05 TORCS disconnect, which
ends the loop cleanly rather than restoring), and crash count.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from latency_stats import Stats  # noqa: E402


def _to_bool(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in ("true", "1", "yes")


def _to_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _to_int(raw: str | None) -> int:
    return int(raw) if raw not in (None, "") else 0


def load_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# endurance
# ---------------------------------------------------------------------------

def success_rate_pct(strategy_requests: int, granite_failures: int) -> str:
    if strategy_requests == 0:
        return "N/A"
    successful = strategy_requests - granite_failures
    return f"{successful / strategy_requests * 100:.2f}%"


def render_endurance_table(rows: list[dict]) -> str:
    lines = [
        "| Run | Duration (s) | Frames | Strategy requests | Granite failures | Request success rate | "
        "Safety filter interventions | Collisions | Off-track excursions | Recoveries | Unhandled exceptions |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    totals = {
        "duration_s": 0.0, "frames": 0, "requests": 0, "failures": 0,
        "interventions": 0, "collisions": 0, "excursions": 0, "recoveries": 0, "exceptions": 0,
    }
    for row in rows:
        requests = _to_int(row.get("strategy_requests"))
        failures = _to_int(row.get("granite_failures"))
        lines.append(
            "| "
            + " | ".join(
                [
                    row["run_id"],
                    row["duration_s"],
                    row["control_frames_processed"],
                    str(requests),
                    str(failures),
                    success_rate_pct(requests, failures),
                    row["safety_filter_interventions"],
                    row["collisions"],
                    row["off_track_excursions"],
                    row["recoveries"],
                    row["unhandled_exceptions"],
                ]
            )
            + " |"
        )
        totals["duration_s"] += float(row["duration_s"])
        totals["frames"] += _to_int(row.get("control_frames_processed"))
        totals["requests"] += requests
        totals["failures"] += failures
        totals["interventions"] += _to_int(row.get("safety_filter_interventions"))
        totals["collisions"] += _to_int(row.get("collisions"))
        totals["excursions"] += _to_int(row.get("off_track_excursions"))
        totals["recoveries"] += _to_int(row.get("recoveries"))
        totals["exceptions"] += _to_int(row.get("unhandled_exceptions"))

    lines.append(
        "| **Total** | "
        + " | ".join(
            [
                f"{totals['duration_s']:.1f}",
                str(totals["frames"]),
                str(totals["requests"]),
                str(totals["failures"]),
                success_rate_pct(totals["requests"], totals["failures"]),
                str(totals["interventions"]),
                str(totals["collisions"]),
                str(totals["excursions"]),
                str(totals["recoveries"]),
                str(totals["exceptions"]),
            ]
        )
        + " |"
    )
    return "\n".join(lines)


SAFETY_FILTER_BREAKDOWN_COLUMNS = [
    ("PIT (fuel/damage override)", "safety_filter_pit_count"),
    ("DEFEND (severe damage)", "safety_filter_defend_count"),
    ("BLOCK (rear-gap)", "safety_filter_block_count"),
    ("ATTACK capped to NORMAL (damage/fuel)", "safety_filter_normal_cap_count"),
]


def render_safety_filter_breakdown_table(rows: list[dict]) -> str:
    lines = [
        "| Run | " + " | ".join(label for label, _ in SAFETY_FILTER_BREAKDOWN_COLUMNS) + " |",
        "|---|" + "---:|" * len(SAFETY_FILTER_BREAKDOWN_COLUMNS),
    ]
    have_any = False
    column_totals = [0] * len(SAFETY_FILTER_BREAKDOWN_COLUMNS)
    for row in rows:
        counts = [row.get(col, "") for _, col in SAFETY_FILTER_BREAKDOWN_COLUMNS]
        if not any(c not in (None, "") for c in counts):
            continue
        have_any = True
        values = [_to_int(c) for c in counts]
        lines.append("| " + row["run_id"] + " | " + " | ".join(str(v) for v in values) + " |")
        for idx, v in enumerate(values):
            column_totals[idx] += v
    if not have_any:
        return "No per-type safety_filter breakdown columns present in this file (optional -- only the aggregate `safety_filter_interventions` column is required)."
    lines.append("| **Total** | " + " | ".join(str(v) for v in column_totals) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# fault recovery
# ---------------------------------------------------------------------------

def recovery_time_s(row: dict) -> float | None:
    restored = _to_float(row.get("service_restored_at_s"))
    first_success = _to_float(row.get("first_success_after_restore_at_s"))
    if restored is None or first_success is None:
        return None
    return first_success - restored


def summarize_faults(rows: list[dict]) -> dict[str, dict]:
    by_fault: dict[str, list[dict]] = {}
    for row in rows:
        by_fault.setdefault(row["fault_id"], []).append(row)

    summary = {}
    for fault_id, trials in by_fault.items():
        recovered = sum(1 for t in trials if _to_bool(t.get("recovered")))
        crashed = sum(1 for t in trials if _to_bool(t.get("crashed")))
        recovery_times = [rt for rt in (recovery_time_s(t) for t in trials) if rt is not None]
        stats = Stats.from_samples(recovery_times, failures=0)
        if crashed > 0:
            result = f"FAIL (crash in {crashed}/{len(trials)})"
        elif recovered == len(trials):
            result = "PASS"
        else:
            result = f"PARTIAL ({recovered}/{len(trials)} recovered)"
        summary[fault_id] = {
            "trials": len(trials),
            "recovered": recovered,
            "median_recovery_s": stats.median,
            "crashed": crashed,
            "result": result,
        }
    return summary


def render_fault_table(summary: dict[str, dict]) -> str:
    lines = [
        "| Fault condition | Trials | Successful recovery | Median recovery time | Crashes | Result |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for fault_id in sorted(summary):
        s = summary[fault_id]
        median = "N/A" if s["median_recovery_s"] is None else f"{s['median_recovery_s']:.2f}s"
        lines.append(
            f"| {fault_id} | {s['trials']} | {s['recovered']} | {median} | {s['crashed']} | {s['result']} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    endurance_parser = sub.add_parser("endurance")
    endurance_parser.add_argument("--file", required=True, type=Path)
    endurance_parser.add_argument("--out-dir", type=Path, default=None)

    faults_parser = sub.add_parser("faults")
    faults_parser.add_argument("--file", required=True, type=Path)
    faults_parser.add_argument("--out-dir", type=Path, default=None)

    args = parser.parse_args()

    if not args.file.exists():
        print(f"file not found: {args.file}", file=sys.stderr)
        return 2
    rows = load_rows(args.file)
    if not rows:
        print("file has a header but zero data rows (empty dataset)", file=sys.stderr)
        return 1

    if args.command == "endurance":
        table = render_endurance_table(rows) + "\n\n" + render_safety_filter_breakdown_table(rows)
        out_name = "endurance_summary.md"
    else:
        table = render_fault_table(summarize_faults(rows))
        out_name = "fault_recovery_summary.md"

    print(table)
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        out_path = args.out_dir / out_name
        out_path.write_text(table + "\n", encoding="utf-8")
        print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
