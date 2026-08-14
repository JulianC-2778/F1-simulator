#!/usr/bin/env python3
"""Assemble every generated table into one "AI Driving Bot Evaluation"
report, matching bot_test_plan.md's work-package structure (section 0: A
functional correctness, B strategy accuracy + driving quality, C latency, D
stability/fault recovery) -- direct counterpart of
evaluation/commentary/scripts/generate_report_tables.py.

Every input is optional. Sections whose CSV wasn't supplied are written as
"NOT RUN" with the exact command needed to fill them in -- never silently
skipped, never invented. Pass --sample to run against
evaluation/bot/sample_data/ (all outputs get a loud SAMPLE banner --
bot_test_plan.md 1's "sample/demo, automated test, real experiment must
always be distinguishable" rule, inherited from commentary_test_plan.md 2).

Usage:
    # After a real experiment:
    python generate_report_tables.py \\
        --test-summary path/to/pytest_summary.md \\
        --ground-truth path/to/ground_truth_strategy.csv --detections path/to/detected_strategy.csv \\
        --lap-performance path/to/lap_performance.csv \\
        --control-latency path/to/control_latency.csv \\
        --endurance path/to/stability_run.csv --faults path/to/fault_recovery.csv \\
        --out evaluation/bot/results/real_experiment_report_TIMESTAMP.md

    # Demo against the bundled sample data (clearly labelled, not a real result):
    python generate_report_tables.py --sample --out /tmp/sample_report.md
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = SCRIPTS_DIR.parent / "sample_data"
sys.path.insert(0, str(SCRIPTS_DIR))

from analyse_control_latency import compute_stats, load_rows as load_latency_rows, render_table as render_latency_table  # noqa: E402
from analyse_stability import (  # noqa: E402
    load_rows as load_stability_rows,
    render_endurance_table,
    render_fault_table,
    render_safety_filter_breakdown_table,
    summarize_faults,
)
from match_strategy_decisions import (  # noqa: E402
    load_detections,
    load_ground_truth,
    match_all,
    render_markdown_table,
    summarize_by_strategy,
)


def section_not_run(title: str, command_hint: str) -> str:
    return f"### {title}\n\nNOT RUN -- no data supplied for this run. To fill this in:\n\n```\n{command_hint}\n```\n"


def render_lap_performance_table(rows: list[dict]) -> str:
    lines = [
        "| Session | Track | Granite | Laps | Completed | Distance (km) | Off-track excursions | Recoveries | "
        "Collisions | Lap time mean (s) | Lap time stdev (s) | Strategy switches |",
        "|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    row["session"],
                    row["track"],
                    row.get("granite_enabled", ""),
                    f"{row['laps_completed']}/{row['laps_target']}",
                    row["completed"],
                    row["distance_km"],
                    row["off_track_excursions"],
                    row["off_track_recoveries"],
                    row["collisions"],
                    row.get("lap_time_mean_s") or "N/A",
                    row.get("lap_time_stdev_s") or "N/A",
                    row.get("strategy_switches") or "N/A",
                ]
            )
            + " |"
        )
    completed_count = sum(1 for r in rows if str(r.get("completed", "")).strip().lower() in ("true", "1", "yes"))
    lines.append(f"\nCompletion rate: {completed_count}/{len(rows)} sessions")
    return "\n".join(lines)


def load_lap_performance_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_report(args: argparse.Namespace, banner: str) -> str:
    parts = [f"# AI Driving Bot Evaluation ({banner})\n"]
    parts.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")

    parts.append("## Evaluation Method\n")
    parts.append(
        "See docs/bot_test_plan.md and docs/bot_test_matrix.md for the full "
        "requirement-to-code traceability this report is built from. Work "
        "package A (automated functional tests) lives in `tests/bot/` -- run "
        "with `pytest tests/bot`, not from here.\n"
    )

    parts.append("## Work Package A: Functional Correctness\n")
    if args.test_summary and args.test_summary.exists():
        parts.append(args.test_summary.read_text(encoding="utf-8"))
    else:
        parts.append(section_not_run(
            "Automated test summary",
            "python -m pytest tests/bot -q",
        ))

    parts.append("## Work Package B: Strategy Accuracy\n")
    if args.ground_truth and args.detections:
        ground_truth = load_ground_truth(args.ground_truth)
        detections = load_detections(args.detections)
        results = match_all(ground_truth, detections, args.tolerance)
        parts.append(render_markdown_table(summarize_by_strategy(results)) + "\n")
    else:
        parts.append(section_not_run(
            "Strategy accuracy",
            "python evaluation/bot/scripts/match_strategy_decisions.py "
            "--ground-truth GT.csv --detections DET.csv --tolerance 2.0",
        ))

    parts.append("## Work Package B: Driving Quality (bot-autonomous)\n")
    if args.lap_performance:
        parts.append(render_lap_performance_table(load_lap_performance_rows(args.lap_performance)) + "\n")
    else:
        parts.append(section_not_run(
            "Driving quality",
            "python evaluation/bot/scripts/validate_experiment_data.py "
            "--kind lap_performance --file lap_performance.csv",
        ))

    parts.append("## Work Package C: End-to-End Latency\n")
    if args.control_latency:
        rows = load_latency_rows(args.control_latency)
        parts.append(render_latency_table(compute_stats(rows)) + "\n")
    else:
        parts.append(section_not_run(
            "End-to-end latency",
            "python evaluation/bot/scripts/analyse_control_latency.py --file control_latency.csv",
        ))

    parts.append("## Work Package D: Stability and Fault Recovery\n")
    stability_parts = []
    if args.endurance:
        endurance_rows = load_stability_rows(args.endurance)
        stability_parts.append(render_endurance_table(endurance_rows))
        stability_parts.append(render_safety_filter_breakdown_table(endurance_rows))
    else:
        stability_parts.append(section_not_run(
            "Endurance run",
            "python evaluation/bot/scripts/analyse_stability.py endurance --file stability_run.csv",
        ))
    if args.faults:
        stability_parts.append(render_fault_table(summarize_faults(load_stability_rows(args.faults))))
    else:
        stability_parts.append(section_not_run(
            "Fault recovery",
            "python evaluation/bot/scripts/analyse_stability.py faults --file fault_recovery.csv",
        ))
    parts.append("\n\n".join(stability_parts) + "\n")

    parts.append("## Limitations\n")
    parts.append(
        "- Work packages B/C/D require a live TORCS + LM Studio/Granite stack "
        "(driven directly over the SCR UDP protocol, not through midware); any "
        "section above marked NOT RUN needs that real environment, not more code.\n"
        "- Strategy-accuracy matching in work package B only scores "
        "`filtered_strategy` (safety_filter's output) against human-annotated "
        "expectations -- it does not separately score `raw_granite_strategy`.\n"
        "- Sample data (if this report used --sample) demonstrates the pipeline "
        "only; it is not evidence of real strategy accuracy, driving quality, "
        "latency or stability.\n"
    )

    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", action="store_true", help="use evaluation/bot/sample_data/ for every section")
    parser.add_argument("--test-summary", type=Path, default=None)
    parser.add_argument("--ground-truth", type=Path, default=None)
    parser.add_argument("--detections", type=Path, default=None)
    parser.add_argument("--tolerance", type=float, default=2.0)
    parser.add_argument("--lap-performance", type=Path, default=None)
    parser.add_argument("--control-latency", type=Path, default=None)
    parser.add_argument("--endurance", type=Path, default=None)
    parser.add_argument("--faults", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.sample:
        args.ground_truth = args.ground_truth or SAMPLE_DIR / "SAMPLE_ground_truth_strategy.csv"
        args.detections = args.detections or SAMPLE_DIR / "SAMPLE_detected_strategy.csv"
        args.lap_performance = args.lap_performance or SAMPLE_DIR / "SAMPLE_lap_performance.csv"
        args.control_latency = args.control_latency or SAMPLE_DIR / "SAMPLE_control_latency.csv"
        args.endurance = args.endurance or SAMPLE_DIR / "SAMPLE_stability_run.csv"
        args.faults = args.faults or SAMPLE_DIR / "SAMPLE_fault_recovery.csv"
        banner = "SAMPLE DATA -- NOT REAL RESULTS"
    else:
        banner = "REAL EXPERIMENT" if any(
            [args.ground_truth, args.control_latency, args.endurance, args.faults]
        ) else "PARTIAL -- SEE NOT RUN SECTIONS BELOW"

    report = build_report(args, banner)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
