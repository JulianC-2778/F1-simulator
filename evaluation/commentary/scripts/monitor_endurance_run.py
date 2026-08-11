#!/usr/bin/env python3
"""Endurance-run monitor for work package D (commentary_test_plan.md 8.1).

Runs for --duration seconds, listening on the real WebSocket feed and
sampling the midware process's CPU/memory, then writes exactly one row
matching evaluation/commentary/schemas/csv_schemas.py::STABILITY_RUN_SCHEMA.
Two devices running this same script produce directly comparable rows --
that is the whole point of it existing rather than each operator hand-timing
things differently (see docs/commentary_endurance_test_protocol.md).

Column definitions used here (must match analyse_stability.py's reading):
    events_detected                 count of `event_detected` messages
    commentary_requests              count of `ai_start` (source=commentary)
    successful_outputs / outputs_total
                                      count of `ai_done` (source=commentary)
                                      with duplicate!=true and non-empty content
    model_failures                    count of `error` messages (source=commentary)
    duplicate_user_visible_displays  count of `ai_done` with duplicate=true
    outputs_over_45_words             successful_outputs whose content exceeds
                                      45 words (word_count.count_words)
    unhandled_exceptions              count of new "Traceback" lines appended
                                      to --log during the run window
    cpu_avg_pct / cpu_peak_pct        sampled from /proc/<pid>/stat every
                                      --sample-interval seconds
    mem_initial_mb / mem_final_mb / mem_peak_mb
                                      VmRSS from /proc/<pid>/status

reconnect_recovery_time_s is NOT measured by this script -- the WebSocket
disconnect/reconnect fault is operator-injected partway through the run (see
the protocol doc), and the operator notes the two timestamps by hand exactly
as in RT-03/RT-04, then fills that one field into the row this script prints.

Usage:
    python monitor_endurance_run.py \\
        --pid $(pgrep -f 'midware\\.app') \\
        --duration 1800 \\
        --run-id R01 \\
        --log /tmp/midware.log \\
        --out evaluation/commentary/results/real_experiment_stability_run_<DATE>.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from threading import Thread, Event

import websocket

sys.path.insert(0, str(Path(__file__).resolve().parent))
from word_count import count_words  # noqa: E402

COLUMNS = [
    "run_id", "duration_s", "events_detected", "commentary_requests",
    "successful_outputs", "model_failures", "duplicate_user_visible_displays",
    "unhandled_exceptions", "reconnect_recovery_time_s", "cpu_avg_pct",
    "cpu_peak_pct", "mem_initial_mb", "mem_final_mb", "mem_peak_mb",
    "outputs_total", "outputs_over_45_words",
]


def read_proc_stat(pid: int) -> tuple[int, int]:
    """Returns (utime+stime in clock ticks, hertz) for CPU% computation."""
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().split()
    utime, stime = int(fields[13]), int(fields[14])
    return utime + stime, _clk_tck()


_HERTZ = None


def _clk_tck() -> int:
    global _HERTZ
    if _HERTZ is None:
        import os
        _HERTZ = os.sysconf("SC_CLK_TCK")
    return _HERTZ


def read_rss_mb(pid: int) -> float:
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                kb = int(line.split()[1])
                return kb / 1024.0
    return 0.0


class Sampler(Thread):
    def __init__(self, pid: int, interval: float, stop_event: Event):
        super().__init__(daemon=True)
        self.pid = pid
        self.interval = interval
        self.stop_event = stop_event
        self.cpu_samples: list[float] = []
        self.mem_samples: list[float] = []
        self.mem_initial: float | None = None
        self.mem_final: float = 0.0

    def run(self):
        prev_ticks, hertz = read_proc_stat(self.pid)
        prev_time = time.monotonic()
        self.mem_initial = read_rss_mb(self.pid)
        while not self.stop_event.wait(self.interval):
            now = time.monotonic()
            ticks, _ = read_proc_stat(self.pid)
            dt = now - prev_time
            dticks = ticks - prev_ticks
            cpu_pct = 100.0 * (dticks / hertz) / dt if dt > 0 else 0.0
            self.cpu_samples.append(cpu_pct)
            prev_ticks, prev_time = ticks, now
            mem = read_rss_mb(self.pid)
            self.mem_samples.append(mem)
            self.mem_final = mem


def count_new_tracebacks(log_path: Path, start_line: int) -> int:
    if not log_path.exists():
        return 0
    with log_path.open(encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return sum(1 for l in lines[start_line:] if "Traceback" in l)


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pid", required=True, type=int, help="midware.app process id")
    parser.add_argument("--duration", required=True, type=float, help="seconds to run for (e.g. 1800 for 30min)")
    parser.add_argument("--run-id", required=True, help="e.g. R01, R02, R03 -- keep distinct across BOTH devices")
    parser.add_argument("--log", required=True, type=Path, help="midware stdout/stderr log path")
    parser.add_argument("--out", required=True, type=Path, help="stability_run CSV to append the finished row to")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8880/ws")
    parser.add_argument("--sample-interval", type=float, default=5.0)
    args = parser.parse_args()

    log_start_line = line_count(args.log)
    stop_event = Event()
    sampler = Sampler(args.pid, args.sample_interval, stop_event)
    sampler.start()

    events_detected = 0
    commentary_requests = 0
    successful_outputs = 0
    model_failures = 0
    duplicate_displays = 0
    over_45 = 0

    ws = websocket.create_connection(args.ws_url, timeout=10)
    ws.settimeout(2.0)
    start = time.monotonic()
    deadline = start + args.duration
    print(f"[{args.run_id}] monitoring pid={args.pid} for {args.duration:.0f}s ...", file=sys.stderr)

    while time.monotonic() < deadline:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            continue
        mtype = msg.get("type")
        source = msg.get("source")
        if mtype == "event_detected":
            events_detected += 1
        elif mtype == "ai_start" and source == "commentary":
            commentary_requests += 1
        elif mtype == "error" and source == "commentary":
            model_failures += 1
        elif mtype == "ai_done" and source == "commentary":
            if msg.get("duplicate"):
                duplicate_displays += 1
            else:
                content = (msg.get("content") or "").strip()
                if content:
                    successful_outputs += 1
                    if count_words(content) > 45:
                        over_45 += 1
        remaining = deadline - time.monotonic()
        if remaining > 0:
            print(f"\r[{args.run_id}] {remaining:6.0f}s left | events={events_detected} "
                  f"requests={commentary_requests} ok={successful_outputs} fail={model_failures} "
                  f"dup={duplicate_displays}", end="", file=sys.stderr)

    ws.close()
    print(file=sys.stderr)
    actual_duration = time.monotonic() - start
    stop_event.set()
    sampler.join(timeout=args.sample_interval + 5)

    unhandled_exceptions = count_new_tracebacks(args.log, log_start_line)
    cpu_avg = sum(sampler.cpu_samples) / len(sampler.cpu_samples) if sampler.cpu_samples else 0.0
    cpu_peak = max(sampler.cpu_samples) if sampler.cpu_samples else 0.0
    mem_peak = max(sampler.mem_samples) if sampler.mem_samples else (sampler.mem_initial or 0.0)

    row = {
        "run_id": args.run_id,
        "duration_s": f"{actual_duration:.1f}",
        "events_detected": events_detected,
        "commentary_requests": commentary_requests,
        "successful_outputs": successful_outputs,
        "model_failures": model_failures,
        "duplicate_user_visible_displays": duplicate_displays,
        "unhandled_exceptions": unhandled_exceptions,
        "reconnect_recovery_time_s": "",  # operator fills this in by hand -- see protocol doc
        "cpu_avg_pct": f"{cpu_avg:.1f}",
        "cpu_peak_pct": f"{cpu_peak:.1f}",
        "mem_initial_mb": f"{sampler.mem_initial or 0.0:.1f}",
        "mem_final_mb": f"{sampler.mem_final:.1f}",
        "mem_peak_mb": f"{mem_peak:.1f}",
        "outputs_total": successful_outputs,
        "outputs_over_45_words": over_45,
    }

    new_file = not args.out.exists()
    with args.out.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)

    print(f"wrote row {args.run_id} to {args.out}", file=sys.stderr)
    print(json.dumps(row, indent=2))
    print("\nIMPORTANT: reconnect_recovery_time_s is blank -- fill it in by hand "
          "(see docs/commentary_endurance_test_protocol.md step 3) before this row "
          "is analysis-ready.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
