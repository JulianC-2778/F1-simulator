#!/usr/bin/env python3
"""Offline replay harness for the bot's Granite strategy prompt (Module 4).

Why
---
Tuning a prompt by driving a race is a minutes-per-iteration loop, and the
answer you get is one situation deep.  This replays a fixed set of recorded
race states straight through midware's /api/bot/strategy, so a prompt edit can
be judged against twenty situations in the time it takes to make a coffee —
and against the *same* twenty every time, which is what makes two prompt
variants comparable at all.

It also computes the four measures that decide whether the model is actually
reasoning or just producing confident-sounding filler:

  diversity    how many distinct strategies appeared.  One strategy for every
               situation is mode collapse -- the failure this whole exercise
               is guarding against, and the one a screenshot of a single
               plausible answer will never reveal.
  sensitivity  perturbation test: re-ask with one key number changed (fuel
               8.2 -> 30.0) and see whether the answer moves.  A model whose
               decision and stated reason are identical either way is reciting,
               not reading.
  consistency  ask the same state twice.  Wild disagreement means the answer
               is noise, however good the reasoning text looks.
  (reasonableness stays a human judgement -- the transcript is printed so it
   can be eyeballed; no metric here pretends to automate it.)

Usage
-----
    # capture states from a live race first (ai_bot writes bot_trace.jsonl),
    # or fall back to the built-in synthetic set
    python3 bot_replay.py --mode bare
    python3 bot_replay.py --mode reasoning --states bot_trace.jsonl
    python3 bot_replay.py --compare            # all three variants, side by side

The prompt variant is chosen per request via `prompt_mode` in the body, so a
comparison run needs no server restart and no environment fiddling.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import config

try:
    from ai_bot import build_situation, _GRANITE_STRATEGIES
except Exception as exc:  # pragma: no cover - import diagnostics only
    print(f"could not import ai_bot ({exc}); run this from the repo root")
    raise SystemExit(1)


# Must outwait the slowest variant's own ceiling plus ModelBroker's 15 s
# grace, or the client gives up first and every slow answer looks like a
# network fault instead of the slow answer it is.
TIMEOUT_S = 200.0


# A spread of situations that should NOT all warrant the same answer: healthy
# car early, damaged car, fuel short of the finish, boxed in from behind, and
# a clear-track final lap.  If one strategy covers all five, the model is not
# reading the state.
SYNTHETIC_STATES: list[dict[str, Any]] = [
    {"_label": "healthy, early, clear track",
     "speed_x": 210.0, "fuel": 60.0, "damage": 0.0, "race_pos": 4,
     "laps_left": 6, "remaining_laps": 8, "fuel_per_lap": 4.5,
     "last_lap_time": 84.2, "opponents": [200.0] * 36},
    {"_label": "heavily damaged mid-race",
     "speed_x": 180.0, "fuel": 40.0, "damage": 8600.0, "race_pos": 5,
     "laps_left": 4, "remaining_laps": 8, "fuel_per_lap": 4.5,
     "last_lap_time": 88.9, "opponents": [200.0] * 36},
    {"_label": "fuel short of the finish",
     "speed_x": 200.0, "fuel": 8.2, "damage": 1200.0, "race_pos": 3,
     "laps_left": 2, "remaining_laps": 8, "fuel_per_lap": 4.5,
     "last_lap_time": 85.1, "opponents": [200.0] * 36},
    {"_label": "car right behind, healthy",
     "speed_x": 220.0, "fuel": 45.0, "damage": 500.0, "race_pos": 3,
     "laps_left": 5, "remaining_laps": 8, "fuel_per_lap": 4.5,
     "last_lap_time": 83.7,
     "opponents": [12.0] * 4 + [200.0] * 28 + [12.0] * 4},
    {"_label": "final lap, clear, healthy",
     "speed_x": 240.0, "fuel": 15.0, "damage": 900.0, "race_pos": 2,
     "laps_left": 1, "remaining_laps": 8, "fuel_per_lap": 4.5,
     "last_lap_time": 83.1, "opponents": [200.0] * 36},
]


def load_states(path: str | None) -> list[dict[str, Any]]:
    """Read recorded states from a JSONL trace, or fall back to the synthetic set."""
    if not path:
        return SYNTHETIC_STATES
    states: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            # A trace mixes "state" and "decision" records.  Decision records
            # have no `state` key, so an unguarded .get(..., record) fallback
            # silently promotes them to states and feeds strategy/reason text
            # to the prompt as if it were telemetry — invisible corruption of
            # the sample.  Skip anything that is not a state record; only a
            # file with no `kind` field at all is treated as bare states.
            kind = record.get("kind")
            if kind is not None and kind != "state":
                continue
            state = record.get("state", record)
            if isinstance(state, dict) and state:
                state.setdefault("_label", f"trace @ {state.get('dist_raced', 0):.0f} m")
                states.append(state)
    if not states:
        print(f"no usable states in {path}; falling back to the synthetic set")
        return SYNTHETIC_STATES
    return states


def subsample(states: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    """Thin a corpus down to `limit` states, evenly spaced across the race.

    Evenly spaced rather than random on purpose. Two runs of the same file with
    the same limit must select the *same* states, or the comparison between
    them measures the sample as much as the thing being compared — which is
    exactly what stops the existing LM Studio numbers being lined up against a
    second platform: that sample came from `shuf` and was never kept. Even
    spacing also covers the whole race, where a random draw can cluster in one
    stint and miss the fuel or damage range entirely.
    """
    if not limit or limit >= len(states):
        return states
    step = len(states) / limit
    return [states[int(i * step)] for i in range(limit)]


def ask(state: dict[str, Any], mode: str) -> dict[str, Any]:
    """One strategy request. Returns the decision dict, or {} plus an error note."""
    sensor_state = {
        **{key: value for key, value in state.items() if not key.startswith("_")},
        "situation": build_situation(state, float(state.get("_prev_lap_time", 0.0))),
        "allowed_strategies": sorted(_GRANITE_STRATEGIES),
    }
    body = json.dumps({
        "bot_id": "replay",
        "current_strategy": "NORMAL",
        "sensor_state": sensor_state,
        "prompt_mode": mode,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{config.MIDWARE_BASE_URL}/api/bot/strategy",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The status line alone ("502 Bad Gateway") hides which failure it
        # was — a model timeout and a JSON object truncated at the token
        # ceiling look identical from outside, and they need opposite fixes.
        # The endpoint puts the real exception text in the body.
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        return {"_error": f"HTTP {exc.code}: {detail or exc.reason}",
                "_seconds": time.monotonic() - started}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"_error": str(exc), "_seconds": time.monotonic() - started}
    decision = result.get("decision") or {}
    decision["_seconds"] = time.monotonic() - started
    if not result.get("ok"):
        decision["_error"] = str(result.get("error", "request rejected"))
    return decision


def run_variant(states: list[dict[str, Any]], mode: str, verbose: bool) -> dict[str, Any]:
    """Ask every state once, then run the sensitivity and consistency probes."""
    decisions: list[dict[str, Any]] = []
    print(f"\n=== {mode} — {len(states)} states ===")
    for state in states:
        decision = ask(state, mode)
        decisions.append(decision)
        label = state.get("_label", "state")
        if decision.get("_error"):
            print(f"  {label:34s}  ERROR  {decision.get('_seconds', 0):5.1f}s  "
                  f"{decision['_error'][:150]}")
            continue
        print(f"  {label:34s}  {decision.get('strategy', '?'):10s}"
              f"  {decision.get('_seconds', 0):5.1f}s  {decision.get('reason', '')[:60]}")
        if verbose and decision.get("considered"):
            for item in decision["considered"]:
                print(f"      · {item.get('factor', '?')}: {item.get('value', '')}"
                      f" -> {item.get('implication', '')}")
            if decision.get("rejected"):
                print(f"      x ruled out {decision['rejected'].get('option', '?')}: "
                      f"{decision['rejected'].get('why', '')}")

    strategies = [d.get("strategy") for d in decisions if not d.get("_error") and d.get("strategy")]
    ok = len(strategies)

    # Sensitivity: same situation, one number changed. The fuel-short state is
    # the probe because its correct answer plainly depends on that one number.
    probe = dict(next(s for s in states if s.get("fuel", 99) < 15) if any(
        s.get("fuel", 99) < 15 for s in states) else states[0])
    before = ask(probe, mode)
    probe_changed = {**probe, "fuel": 60.0, "_label": "perturbed"}
    after = ask(probe_changed, mode)
    if before.get("_error") or after.get("_error"):
        # Two failed calls trivially "agree", which would be scored as the
        # model ignoring the change — the opposite of what happened. A probe
        # that did not run is unknown, not negative.
        moved: bool | None = None
    else:
        moved = (before.get("strategy") != after.get("strategy")
                 or before.get("reason", "") != after.get("reason", ""))

    # Consistency: identical input twice.  Same caveat as above.
    repeat_a = ask(states[0], mode)
    repeat_b = ask(states[0], mode)
    stable: bool | None = (
        None if repeat_a.get("_error") or repeat_b.get("_error")
        else repeat_a.get("strategy") == repeat_b.get("strategy")
    )

    summary = {
        "mode": mode,
        "answered": f"{ok}/{len(states)}",
        "diversity": len(set(strategies)),
        "strategies": dict(sorted(
            (s, strategies.count(s)) for s in set(strategies)
        )) if strategies else {},
        "sensitive": moved,
        "consistent": stable,
        "median_s": round(statistics.median(
            [d.get("_seconds", 0.0) for d in decisions]
        ), 1) if decisions else 0.0,
    }
    print(f"  --> answered {summary['answered']}  distinct={summary['diversity']}  "
          f"{summary['strategies']}  sensitive={_yn(moved)}  consistent={_yn(stable)}  "
          f"median {summary['median_s']}s")
    if ok < len(states):
        print("      NOTE: distinct/sensitive are unreliable while calls are "
              "failing — fix the errors before comparing variants.")
    return summary


def _yn(value: bool | None) -> str:
    """Render a tri-state probe result; '?' means the probe could not run."""
    return "?" if value is None else str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    variants = ("legacy", "bare", "reasoning", "concise")
    parser.add_argument("--mode", default="bare", choices=variants)
    parser.add_argument("--states", help="JSONL trace of recorded race states")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="use only N states, evenly spaced across the race "
                             "(deterministic — the same file and N always give "
                             "the same states, so two runs are comparable)")
    parser.add_argument("--compare", action="store_true",
                        help="run every variant over the same states")
    parser.add_argument("--modes", help="comma-separated subset for --compare, "
                                        f"default all of: {', '.join(variants)}")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the model's factor list where present")
    args = parser.parse_args()

    states = load_states(args.states)
    total = len(states)
    states = subsample(states, args.limit)
    if len(states) != total:
        print(f"using {len(states)} of {total} states (evenly spaced)")

    if args.compare:
        modes = tuple(m.strip() for m in args.modes.split(",")) if args.modes else variants
        unknown = [m for m in modes if m not in variants]
        if unknown:
            parser.error(f"unknown mode(s): {', '.join(unknown)}")
    else:
        modes = (args.mode,)
    summaries = [run_variant(states, mode, args.verbose) for mode in modes]

    if len(summaries) > 1:
        print("\n=== comparison ===")
        header = f"{'mode':11s} {'answered':9s} {'distinct':9s} {'sensitive':10s} {'consistent':11s} median"
        print(header)
        print("-" * len(header))
        for summary in summaries:
            print(f"{summary['mode']:11s} {summary['answered']:9s} "
                  f"{summary['diversity']:<9d} {_yn(summary['sensitive']):10s} "
                  f"{_yn(summary['consistent']):11s} {summary['median_s']}s")
        print("\ndistinct == 1 means mode collapse: the model gave one answer to "
              "every situation.\nsensitive == False means it did not notice a key "
              "number changing.")


if __name__ == "__main__":
    sys.exit(main())
