#!/usr/bin/env python3
"""Join midware's backend latency log (t0-t3) with capture_display_latency.py's
client-side log (t4/t5) into one LATENCY_SCHEMA CSV for analyse_latency.py.

    midware JSONL   {request_id, event_id, session_id, stage, timestamp}
    capture JSONL   {session_id, request_id, event_type, stage, timestamp, ...}
                                    |
                                    v
    latency CSV     session_id,event_id,request_id,t0..t5,failed,failure_reason

Both sides stamp `time.monotonic()`, which is system-wide on Linux, so the
join needs no clock conversion -- see capture_display_latency.py's CLOCK note
for the one case where that breaks (a reboot mid-experiment).

HOW ROWS ARE CLASSIFIED
-----------------------
commentary_test_plan.md 7.2 is explicit that failures and timeouts must count
as failures rather than vanish from the sample, so the default is to keep
everything. Two categories are handled deliberately:

  failed=true   The request produced an `error` broadcast, or never reached
                t3_ai_done for any reason other than preemption. Kept in the
                CSV; analyse_latency.py counts it under Failures for every
                stage.

  preempted     No t3, but a later request was detected afterwards. In
                interrupt mode (the default) a new event whose priority is >=
                the running one cancels the in-flight commentary --
                runtime.py::_run_commentary logs "解说被新事件中断" and the
                request stops before t3 by design, so the commentator can
                narrate the newer event instead of a stale one. These are NOT
                failures: counting them as such reported a 27% failure rate on
                a run whose true failure count was zero. They go to a
                `*_preempted.csv` sidecar and are reported in the summary.
                Cross-check the count against the midware log:
                    grep -c "解说被新事件中断" /tmp/midware.log

  deduplicated  `ai_done` arrived with duplicate=true: the display-time dedup
                deliberately suppressed the text, so no caption was ever shown
                and no audio was synthesised. This is neither a success nor a
                failure of the latency pipeline -- counting it as a failure
                would inflate the failure rate with a working feature. These
                are written to a separate `*_deduplicated.csv` sidecar and
                reported in the summary, never silently dropped. Pass
                --include-duplicates to keep them in the main CSV instead.

Usage:
    python build_latency_csv.py \
        --backend  evaluation/commentary/results/real_experiment_latency_raw_DATE.jsonl \
        --capture  evaluation/commentary/results/real_experiment_display_raw_DATE.jsonl \
        --out      evaluation/commentary/results/real_experiment_latency_DATE.csv \
        --session-id S1
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schemas"))
from csv_schemas import LATENCY_SCHEMA  # noqa: E402

COLUMNS = [c.name for c in LATENCY_SCHEMA.columns]
TIME_STAGES = (
    "t0_telemetry_received", "t1_event_detected", "t2_first_token",
    "t3_ai_done", "t4_caption_displayed", "t5_tts_started",
)
# Diagnostic stage logged alongside t0 by runtime.py::_record_detection: the
# arrival of the newest frame rather than the oldest unseen one. Swapping it in
# for t0 (--t0 newest) reports detection latency excluding the detection loop's
# polling wait. Never a column of its own -- LATENCY_SCHEMA has no slot for it.
T0_NEWEST_STAGE = "t0b_newest_frame"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            print(f"  warning: {path.name} line {line_no} is not valid JSON, skipped", file=sys.stderr)
    return rows


def merge(
    backend: list[dict],
    capture: list[dict],
    session_id: str,
    t0_source: str = "oldest",
) -> tuple[list[dict], dict]:
    requests: dict[str, dict] = {}
    order: list[str] = []
    stats = {"disconnects": 0, "unmatched_capture_requests": 0, "t0_newest_used": 0}

    def slot(request_id: str) -> dict:
        if request_id not in requests:
            requests[request_id] = {
                "request_id": request_id,
                "event_type": "",
                "duplicate": False,
                "failure_reason": "",
                "times": {},
            }
            order.append(request_id)
        return requests[request_id]

    for row in backend:
        rid = str(row.get("request_id") or "")
        stage = str(row.get("stage") or "")
        if not rid:
            continue
        if stage == T0_NEWEST_STAGE:
            slot(rid)["t0_newest"] = float(row["timestamp"])
            continue
        if stage not in TIME_STAGES:
            continue
        entry = slot(rid)
        # A stage should appear once; if it repeats, the earliest observation
        # is the true one (a later duplicate means a retry, not a faster path).
        entry["times"].setdefault(stage, float(row["timestamp"]))

    if t0_source == "newest":
        for entry in requests.values():
            if "t0_newest" in entry:
                entry["times"]["t0_telemetry_received"] = entry["t0_newest"]
                stats["t0_newest_used"] += 1

    backend_ids = set(requests)

    for row in capture:
        stage = str(row.get("stage") or "")
        if stage == "disconnected":
            stats["disconnects"] += 1
            continue
        rid = str(row.get("request_id") or "")
        if not rid:
            continue
        entry = slot(rid)
        if row.get("event_type"):
            entry["event_type"] = entry["event_type"] or str(row["event_type"])
        if row.get("duplicate"):
            entry["duplicate"] = True
        if stage == "failure":
            entry["failure_reason"] = entry["failure_reason"] or str(row.get("reason") or "error")
            continue
        if stage in TIME_STAGES:
            entry["times"].setdefault(stage, float(row["timestamp"]))

    stats["unmatched_capture_requests"] = len(set(requests) - backend_ids)

    # event_id is a required, non-empty column of LATENCY_SCHEMA, and midware
    # never populates it, so mint a stable one per request in detection order,
    # carrying the event type when the capture log identified it.
    # A request that never reached t3 was preempted rather than failed if the
    # detection loop went on to report another event: interrupt mode cancels
    # the running commentary only when a new one arrives. The last incomplete
    # request has no successor, so it stays a genuine failure (the session just
    # ended mid-generation).
    last_detected_index = max(
        (i for i, rid in enumerate(order) if "t1_event_detected" in requests[rid]["times"]),
        default=-1,
    )

    kept: list[dict] = []
    for index, rid in enumerate(order, start=1):
        entry = requests[rid]
        times = entry["times"]
        label = entry["event_type"] or "event"
        incomplete = "t3_ai_done" not in times
        preempted = (
            incomplete
            and not entry["failure_reason"]
            and "t1_event_detected" in times
            and (index - 1) < last_detected_index
        )
        failed = bool(entry["failure_reason"]) or (incomplete and not preempted)
        reason = entry["failure_reason"]
        if preempted:
            reason = "preempted by a higher/equal priority event (interrupt mode, by design)"
        elif failed and not reason:
            reason = "no t3_ai_done recorded (generation did not complete)"
        row = {
            "session_id": session_id,
            "event_id": f"{label}_{index:03d}",
            "request_id": rid,
            "failed": "true" if failed else "false",
            "failure_reason": reason,
        }
        for stage in TIME_STAGES:
            value = times.get(stage)
            row[stage] = f"{value:.6f}" if value is not None else ""
        row["_duplicate"] = entry["duplicate"]
        row["_preempted"] = preempted
        kept.append(row)

    return kept, stats


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in COLUMNS})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", required=True, type=Path, help="midware COMMENTARY_LATENCY_LOG_PATH jsonl")
    parser.add_argument("--capture", required=True, type=Path, help="capture_display_latency.py jsonl")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--include-duplicates", action="store_true",
                        help="keep dedup-suppressed requests in the main CSV")
    parser.add_argument("--t0", choices=("oldest", "newest"), default="oldest",
                        help="which t0 reading to use: 'oldest' (default) is the earliest "
                             "frame that could have carried the event, so detection latency "
                             "includes the detection loop's polling wait; 'newest' uses the "
                             "frame the engine evaluated, excluding that wait")
    args = parser.parse_args()

    rows, stats = merge(read_jsonl(args.backend), read_jsonl(args.capture),
                        args.session_id, t0_source=args.t0)
    if not rows:
        print("no requests found in either log -- nothing to write", file=sys.stderr)
        return 1

    duplicates = [r for r in rows if r["_duplicate"]]
    preempted = [r for r in rows if r["_preempted"]]
    excluded = {id(r) for r in preempted}
    if not args.include_duplicates:
        excluded |= {id(r) for r in duplicates}
    main_rows = [r for r in rows if id(r) not in excluded]

    write_csv(args.out, main_rows)
    for label, subset, suffix in (
        ("dedup-suppressed", duplicates if not args.include_duplicates else [], "_deduplicated"),
        ("preempted (by design, not failures)", preempted, "_preempted"),
    ):
        if subset:
            sidecar = args.out.with_name(args.out.stem + suffix + ".csv")
            write_csv(sidecar, subset)
            print(f"  {len(subset)} {label} request(s) written to {sidecar}", file=sys.stderr)

    failed = sum(1 for r in main_rows if r["failed"] == "true")
    complete = sum(1 for r in main_rows if all(r[s] for s in TIME_STAGES))
    print(f"wrote {args.out}: {len(main_rows)} row(s), {failed} failed, {complete} with all six timestamps")
    print(f"  t0 source: {args.t0}"
          + ("  (detection latency INCLUDES the ~0-0.5s detection-loop polling wait)"
             if args.t0 == "oldest"
             else f"  ({stats['t0_newest_used']} row(s) re-anchored; polling wait EXCLUDED)"))
    for stage in TIME_STAGES:
        have = sum(1 for r in main_rows if r[stage])
        print(f"  {stage:24s} {have}/{len(main_rows)}")
    if stats["disconnects"]:
        print(f"  WARNING: {stats['disconnects']} websocket disconnect(s) during capture -- "
              f"events during those gaps have no t4/t5", file=sys.stderr)
    if stats["unmatched_capture_requests"]:
        print(f"  WARNING: {stats['unmatched_capture_requests']} request(s) seen only by the capture "
              f"client, not by midware's log -- was COMMENTARY_LATENCY_LOG set?", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
