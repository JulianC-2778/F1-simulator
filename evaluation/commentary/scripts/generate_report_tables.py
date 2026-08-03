#!/usr/bin/env python3
"""Assemble every generated table into one "AI Live Commentary Evaluation"
report, matching the section structure commentary_test_plan.md section 13
says the write-up should support:

    5.X.1 Evaluation Method
    5.X.2 Functional Correctness
    5.X.3 Event Detection Accuracy
    5.X.4 End-to-End Latency
    5.X.5 Stability and Fault Recovery
    5.X.6 Limitations

Every input is optional. Sections whose CSV wasn't supplied are written as
"NOT RUN" with the exact command needed to fill them in -- never silently
skipped, never invented. Pass --sample to run against
evaluation/commentary/sample_data/ (all outputs get a loud SAMPLE banner --
see docs/commentary_test_plan.md section 2, "sample/demo, automated test,
real experiment must always be distinguishable").

Usage:
    # After a real experiment:
    python generate_report_tables.py \\
        --test-summary evaluation/commentary/results/automated_test_summary_TIMESTAMP.md \\
        --ground-truth path/to/ground_truth.csv --detections path/to/detected_events.csv \\
        --latency path/to/latency.csv \\
        --endurance path/to/stability_run.csv --faults path/to/fault_recovery.csv \\
        --out evaluation/commentary/results/real_experiment_report_TIMESTAMP.md

    # Demo against the bundled sample data (clearly labelled, not a real result):
    python generate_report_tables.py --sample --out /tmp/sample_report.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = SCRIPTS_DIR.parent / "sample_data"
sys.path.insert(0, str(SCRIPTS_DIR))

from analyse_latency import compute_stats, load_rows as load_latency_rows, render_table as render_latency_table  # noqa: E402
from analyse_stability import (  # noqa: E402
    load_rows as load_stability_rows,
    render_endurance_table,
    render_fault_table,
    summarize_faults,
)
from match_events import load_detections, load_ground_truth, match_all, render_markdown_table, summarize_by_event_type  # noqa: E402


def section_not_run(title: str, command_hint: str) -> str:
    return f"### {title}\n\nNOT RUN -- no data supplied for this run. To fill this in:\n\n```\n{command_hint}\n```\n"


def build_report(args: argparse.Namespace, banner: str) -> str:
    parts = [f"# AI Live Commentary Evaluation ({banner})\n"]
    parts.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")

    parts.append("## 5.X.1 Evaluation Method\n")
    parts.append(
        "See docs/commentary_test_plan.md and docs/commentary_test_matrix.md for the "
        "full requirement-to-code traceability this report is built from.\n"
    )

    parts.append("## 5.X.2 Functional Correctness\n")
    if args.test_summary and args.test_summary.exists():
        parts.append(args.test_summary.read_text(encoding="utf-8"))
    else:
        parts.append(section_not_run(
            "Automated test summary",
            "python tools/commentary_test_report.py",
        ))

    parts.append("## 5.X.3 Event Detection Accuracy\n")
    if args.ground_truth and args.detections:
        ground_truth = load_ground_truth(args.ground_truth)
        detections = load_detections(args.detections)
        results = match_all(ground_truth, detections, args.tolerance)
        parts.append(render_markdown_table(summarize_by_event_type(results)) + "\n")
    else:
        parts.append(section_not_run(
            "Event detection accuracy",
            "python evaluation/commentary/scripts/match_events.py "
            "--ground-truth GT.csv --detections DET.csv --tolerance 1.0",
        ))

    parts.append("## 5.X.4 End-to-End Latency\n")
    if args.latency:
        rows = load_latency_rows(args.latency)
        parts.append(render_latency_table(compute_stats(rows)) + "\n")
    else:
        parts.append(section_not_run(
            "End-to-end latency",
            "python evaluation/commentary/scripts/analyse_latency.py --file latency.csv",
        ))

    parts.append("## 5.X.5 Stability and Fault Recovery\n")
    stability_parts = []
    if args.endurance:
        stability_parts.append(render_endurance_table(load_stability_rows(args.endurance)))
    else:
        stability_parts.append(section_not_run(
            "Endurance run",
            "python evaluation/commentary/scripts/analyse_stability.py endurance --file stability_run.csv",
        ))
    if args.faults:
        stability_parts.append(render_fault_table(summarize_faults(load_stability_rows(args.faults))))
    else:
        stability_parts.append(section_not_run(
            "Fault recovery",
            "python evaluation/commentary/scripts/analyse_stability.py faults --file fault_recovery.csv",
        ))
    parts.append("\n\n".join(stability_parts) + "\n")

    parts.append("## 5.X.6 Limitations\n")
    parts.append(
        "- Work packages B/C/D require a live TORCS + LM Studio/Granite + browser dashboard stack; "
        "any section above marked NOT RUN needs that real environment, not more code.\n"
        "- `max_words` is a prompt-level hint only, not enforced in code -- see "
        "docs/commentary_test_matrix.md section 4.\n"
        "- Sample data (if this report used --sample) demonstrates the pipeline only; "
        "it is not evidence of real detection accuracy, latency or stability.\n"
    )

    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", action="store_true", help="use evaluation/commentary/sample_data/ for every section")
    parser.add_argument("--test-summary", type=Path, default=None)
    parser.add_argument("--ground-truth", type=Path, default=None)
    parser.add_argument("--detections", type=Path, default=None)
    parser.add_argument("--tolerance", type=float, default=1.0)
    parser.add_argument("--latency", type=Path, default=None)
    parser.add_argument("--endurance", type=Path, default=None)
    parser.add_argument("--faults", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.sample:
        args.ground_truth = args.ground_truth or SAMPLE_DIR / "SAMPLE_ground_truth.csv"
        args.detections = args.detections or SAMPLE_DIR / "SAMPLE_detected_events.csv"
        args.latency = args.latency or SAMPLE_DIR / "SAMPLE_latency.csv"
        args.endurance = args.endurance or SAMPLE_DIR / "SAMPLE_stability_run.csv"
        args.faults = args.faults or SAMPLE_DIR / "SAMPLE_fault_recovery.csv"
        banner = "SAMPLE DATA -- NOT REAL RESULTS"
    else:
        banner = "REAL EXPERIMENT" if any(
            [args.ground_truth, args.latency, args.endurance, args.faults]
        ) else "PARTIAL -- SEE NOT RUN SECTIONS BELOW"

    report = build_report(args, banner)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
