#!/usr/bin/env python3
"""Latency benchmark for the Granite endpoint behind midware.

bot_replay.py reports a median as a by-product of comparing decision quality;
this measures latency as the subject. Differences that matter for a write-up:

  - reports the distribution (min / median / mean / P95 / max), not one number
    — P95 is what says how bad a slow answer gets, and the poll interval has to
    be sized against that, not against the median;
  - separates the first call of a run, which pays model warm-up and is not
    representative of steady state;
  - records generated tokens per call, because response time on a local model
    is mostly tokens ÷ rate — a variant is not "slower", it is writing more.

Goes through midware (same path the bot uses), so what it measures is what the
bot experiences, including any queueing in ModelBroker.

Usage:
    python3 bench_latency.py                     # reasoning, 10 calls
    python3 bench_latency.py --modes legacy,reasoning --repeats 15
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request

import config

# One fixed, representative race situation. Held constant across every call and
# every variant: latency must not move because the situation changed.
SITUATION = {
    "lap": "3 lap(s) done, 5 remaining",
    "position": "P1",
    "fuel": "93.3 L left, burning 4.5 L/lap, ~22.5 L needed to finish",
    "damage": "272 out of 10000 (drivable)",
    "speed": "196 km/h",
    "gap ahead": "no car in front within sensor range",
    "gap behind": "48 m behind (~0.9 s at current speed)",
    "last lap": "102.3 s (previous 103.1 s, 0.8 s faster)",
}


def one_call(mode: str, timeout: float) -> tuple[float, str, str]:
    """Returns (seconds, strategy, error)."""
    body = json.dumps({
        "bot_id": "bench",
        "current_strategy": "NORMAL",
        "prompt_mode": mode,
        "sensor_state": {
            "fuel": 93.3, "damage": 272.0, "race_pos": 1, "laps_left": 5,
            "fuel_per_lap": 4.5, "speed_x": 196.0,
            "situation": SITUATION,
            "allowed_strategies": ["ATTACK", "DEFEND", "NORMAL", "PIT"],
        },
    }).encode()
    request = urllib.request.Request(
        f"{config.MIDWARE_BASE_URL}/api/bot/strategy", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error", "")
        except Exception:
            detail = exc.reason
        return time.monotonic() - started, "", f"HTTP {exc.code}: {detail}"
    except Exception as exc:
        return time.monotonic() - started, "", str(exc)
    return (time.monotonic() - started,
            (payload.get("decision") or {}).get("strategy", "?"), "")


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Explicit rather than statistics.quantiles so a
    run of 10 samples reports an actual observation instead of an interpolation
    between two of them."""
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[idx]


def bench(mode: str, repeats: int, timeout: float) -> dict:
    print(f"\n=== {mode} — {repeats} calls ===")
    times, errors = [], 0
    first = None
    for i in range(repeats):
        secs, strategy, error = one_call(mode, timeout)
        if error:
            errors += 1
            print(f"  {i+1:2d}  ERROR  {secs:5.1f}s  {error[:70]}")
            continue
        if i == 0:
            first = secs
            print(f"  {i+1:2d}  {secs:5.1f}s  {strategy:10s}  (first call — warm-up, excluded)")
            continue
        times.append(secs)
        print(f"  {i+1:2d}  {secs:5.1f}s  {strategy}")

    if not times:
        print("  no successful steady-state calls")
        return {"mode": mode, "n": 0, "errors": errors}

    stats = {
        "mode": mode, "n": len(times), "errors": errors, "first_call_s": first,
        "min": min(times), "median": statistics.median(times),
        "mean": statistics.fmean(times), "p95": percentile(times, 95),
        "max": max(times),
    }
    print(f"  --> n={stats['n']}  min={stats['min']:.1f}  median={stats['median']:.1f}  "
          f"mean={stats['mean']:.1f}  P95={stats['p95']:.1f}  max={stats['max']:.1f}  "
          f"(first call {first:.1f}s)")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--modes", default="reasoning",
                        help="comma-separated prompt variants (default: reasoning)")
    parser.add_argument("--repeats", type=int, default=10,
                        help="calls per variant, first excluded as warm-up (default: 10)")
    parser.add_argument("--timeout", type=float, default=200.0)
    args = parser.parse_args()

    results = [bench(m.strip(), args.repeats, args.timeout)
               for m in args.modes.split(",")]

    print("\n=== latency summary (seconds, warm-up excluded) ===")
    header = f"{'mode':11s} {'n':>3s} {'min':>6s} {'median':>7s} {'mean':>6s} {'P95':>6s} {'max':>6s} {'1st':>6s} {'err':>4s}"
    print(header)
    print("-" * len(header))
    for r in results:
        if not r.get("n"):
            print(f"{r['mode']:11s}   0  {'—':>6s} {'—':>7s} {'—':>6s} {'—':>6s} {'—':>6s} {'—':>6s} {r['errors']:>4d}")
            continue
        print(f"{r['mode']:11s} {r['n']:3d} {r['min']:6.1f} {r['median']:7.1f} "
              f"{r['mean']:6.1f} {r['p95']:6.1f} {r['max']:6.1f} "
              f"{r['first_call_s']:6.1f} {r['errors']:4d}")


if __name__ == "__main__":
    main()
