#!/usr/bin/env python3
"""Stability/endurance and fault-recovery summary tables --
commentary_test_plan.md work package D (sections 8.1-8.2).

Two subcommands, matching the two CSV templates:

    python analyse_stability.py endurance --file stability_run.csv
    python analyse_stability.py faults --file fault_recovery.csv

`endurance` consumes evaluation/commentary/schemas/csv_schemas.py::STABILITY_RUN_SCHEMA
(one row per endurance run) and reports success rate
(successful_outputs / commentary_requests * 100%, N/A if there were zero
requests -- never silently reported as 0%) plus the 45-word violation rate,
per run and aggregated.

`faults` consumes FAULT_RECOVERY_SCHEMA (one row per fault-injection trial)
and reports, per fault_id: trial count, successful-recovery count, median
recovery time (first_success_after_restore_at_s - service_restored_at_s,
N/A for fault kinds that don't restore -- e.g. RT-07 invalid telemetry,
which has no restoration step), and crash count.
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


def load_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# endurance
# ---------------------------------------------------------------------------

def success_rate_pct(successful_outputs: int, commentary_requests: int) -> str:
    if commentary_requests == 0:
        return "N/A"
    return f"{successful_outputs / commentary_requests * 100:.2f}%"


def violation_rate_pct(over_45: int | None, total: int | None) -> str:
    if not total:
        return "N/A"
    return f"{(over_45 or 0) / total * 100:.2f}%"


def render_endurance_table(rows: list[dict]) -> str:
    lines = [
        "| Run | Duration (s) | Events | Requests | Successful | Success rate | "
        "Model failures | Duplicate displays | Unhandled exceptions | 45-word violation rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    total_duration = total_events = total_requests = total_success = 0
    total_failures = total_dupes = total_exceptions = 0
    total_over45 = total_outputs = 0
    for row in rows:
        requests = int(row["commentary_requests"])
        successful = int(row["successful_outputs"])
        over45 = int(row["outputs_over_45_words"]) if row.get("outputs_over_45_words") else None
        outputs_total = int(row["outputs_total"]) if row.get("outputs_total") else None
        lines.append(
            "| "
            + " | ".join(
                [
                    row["run_id"],
                    row["duration_s"],
                    row["events_detected"],
                    str(requests),
                    str(successful),
                    success_rate_pct(successful, requests),
                    row["model_failures"],
                    row["duplicate_user_visible_displays"],
                    row["unhandled_exceptions"],
                    violation_rate_pct(over45, outputs_total),
                ]
            )
            + " |"
        )
        total_duration += float(row["duration_s"])
        total_events += int(row["events_detected"])
        total_requests += requests
        total_success += successful
        total_failures += int(row["model_failures"])
        total_dupes += int(row["duplicate_user_visible_displays"])
        total_exceptions += int(row["unhandled_exceptions"])
        total_over45 += over45 or 0
        total_outputs += outputs_total or 0

    lines.append(
        "| **Total** | "
        + " | ".join(
            [
                f"{total_duration:.1f}",
                str(total_events),
                str(total_requests),
                str(total_success),
                success_rate_pct(total_success, total_requests),
                str(total_failures),
                str(total_dupes),
                str(total_exceptions),
                violation_rate_pct(total_over45, total_outputs),
            ]
        )
        + " |"
    )
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
        table = render_endurance_table(rows)
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
