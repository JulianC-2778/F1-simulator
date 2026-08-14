#!/usr/bin/env python3
"""Work package B (bot autonomous-driving scenario) and work package C
(Granite decision cadence) metrics from a real ai_bot.py TraceRecorder
JSONL log (TORCS_BOT_TRACE): completion, off-track recovery, collision
frequency, lap-time consistency, strategy switch frequency, and Granite
decision-to-decision timing.

Not a substitute for the full designs in docs/bot_test_plan.md sections 5
(work package B: 2 tracks x 3 sessions, human-driven ground truth) and 6
(work package C: full t0-t5 latency breakdown) -- this is what a real,
single-track, bot-autonomous TraceRecorder log can honestly support: no
human-labelled ground truth, and no finer-than-"decision landed" timestamp
inside a single Granite round trip. See
evaluation/bot/results/real_experiment_bot_drive_20260812.jsonl's own
report for the full caveats.

Usage: python3 analyse_bot_trace.py <trace.jsonl>
"""
import json
import sys
import statistics

path = sys.argv[1] if len(sys.argv) > 1 else "latency_run1.jsonl"

states = []
decisions = []
with open(path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec["kind"] == "state":
            states.append(rec)
        elif rec["kind"] == "decision":
            decisions.append(rec)

print(f"Total records: {len(states)} state, {len(decisions)} decision")
if not states:
    sys.exit(0)

# time.monotonic() on Linux is system-wide (typically since boot), not
# per-process -- separate ai_bot.py invocations (this file was appended to
# across several restarts today) do NOT reset to 0, so naive last-t minus
# first-t would silently include real-world gaps where no process was
# actually running/connected (TORCS down, manual restarts, etc.) as if they
# were active driving time. Detect those gaps (state samples are on a ~2s
# interval; anything much larger than that is a restart gap, not a sampling
# stall) and split into contiguous run segments instead.
GAP_THRESHOLD_S = 15.0
segments = []
seg_start_idx = 0
for i in range(1, len(states)):
    if states[i]["t"] - states[i - 1]["t"] > GAP_THRESHOLD_S:
        segments.append((seg_start_idx, i - 1))
        seg_start_idx = i
segments.append((seg_start_idx, len(states) - 1))

print(f"\nDetected {len(segments)} contiguous run segment(s) (gap threshold {GAP_THRESHOLD_S}s):")
active_duration_s = 0.0
for si, (a, b) in enumerate(segments):
    seg_t0, seg_t1 = states[a]["t"], states[b]["t"]
    seg_dur = seg_t1 - seg_t0
    active_duration_s += seg_dur
    print(f"  segment {si+1}: t={seg_t0:.1f}->{seg_t1:.1f}  dur={seg_dur:.1f}s ({seg_dur/60:.1f} min)  {b-a+1} state samples")

t0, t1 = states[0]["t"], states[-1]["t"]
wall_span_s = t1 - t0
print(f"\nNaive wall-clock span (misleading if segments>1): {wall_span_s:.1f}s ({wall_span_s/60:.1f} min)")
duration_s = active_duration_s
print(f"Real active driving time (sum of segments): {duration_s:.1f}s ({duration_s/60:.1f} min)")

# --- dist_raced / laps ---
dist_raced_vals = [s["state"].get("dist_raced", 0.0) for s in states]
total_dist_km = max(dist_raced_vals) / 1000.0
print(f"Total distance raced: {max(dist_raced_vals):.0f} m ({total_dist_km:.2f} km)")

# --- lap completion: last_lap_time changes ---
lap_times = []
prev_llt = None
for s in states:
    llt = s["state"].get("last_lap_time", 0.0) or 0.0
    if llt > 0.0 and (prev_llt is None or abs(llt - prev_llt) > 1e-3):
        lap_times.append(llt)
        prev_llt = llt
print(f"\n--- Completion ---")
print(f"Laps completed: {len(lap_times)}")
if lap_times:
    print(f"Lap times (s): {[f'{x:.1f}' for x in lap_times]}")
if len(lap_times) >= 2:
    print(f"Lap time mean={statistics.mean(lap_times):.2f}s "
          f"stdev={statistics.stdev(lap_times):.2f}s "
          f"min={min(lap_times):.2f}s max={max(lap_times):.2f}s "
          f"CV={statistics.stdev(lap_times)/statistics.mean(lap_times)*100:.1f}%")
last_state = states[-1]["state"]
laps_left = last_state.get("laps_left", -1)
print(f"laps_left at end of trace: {laps_left} (trace ended mid-session, not a DNF signal by itself)")

# --- off-track excursions + recovery ---
print(f"\n--- Off-track excursions ---")
off_track = False
excursions = 0
recovered = 0
excursion_start_t = None
max_abs_tpos_this_excursion = 0.0
for s in states:
    tpos = abs(s["state"].get("track_pos", 0.0))
    t = s["t"]
    if not off_track and tpos > 1.0:
        off_track = True
        excursions += 1
        excursion_start_t = t
        max_abs_tpos_this_excursion = tpos
    elif off_track:
        max_abs_tpos_this_excursion = max(max_abs_tpos_this_excursion, tpos)
        if tpos <= 1.0:
            recovered += 1
            off_track = False
still_off_at_end = off_track
print(f"Excursions (|track_pos|>1): {excursions}")
print(f"Recovered before trace end: {recovered}")
print(f"Still off-track at end of trace: {still_off_at_end}")
if excursions:
    print(f"Recovery rate: {recovered/excursions*100:.0f}%")

# --- collisions: damage increases ---
print(f"\n--- Collisions (damage increases) ---")
collisions = 0
prev_damage = states[0]["state"].get("damage", 0.0)
damage_events = []
for s in states:
    dmg = s["state"].get("damage", 0.0)
    if dmg > prev_damage + 1e-6:
        collisions += 1
        damage_events.append((s["t"], prev_damage, dmg))
    prev_damage = dmg
final_damage = states[-1]["state"].get("damage", 0.0)
print(f"Collision events (damage jumps): {collisions}")
print(f"Final damage: {final_damage:.0f}")
if total_dist_km > 0:
    print(f"Collisions per km: {collisions/total_dist_km:.3f}")
for t, before, after in damage_events[:10]:
    print(f"  t={t:.1f}  {before:.0f} -> {after:.0f}")

# --- strategy switch frequency ---
print(f"\n--- Strategy switches ---")
strategies = [d["strategy"] for d in decisions]
switches = sum(1 for i in range(1, len(strategies)) if strategies[i] != strategies[i-1])
distinct = set(strategies)
print(f"Distinct strategies seen: {sorted(distinct)}")
print(f"Total decision events: {len(decisions)}")
print(f"Strategy value changes: {switches}")
if duration_s > 0:
    print(f"Switches per minute: {switches/(duration_s/60):.2f}")
    print(f"Decisions per minute: {len(decisions)/(duration_s/60):.2f}")

# --- source breakdown ---
sources = [d.get("source", "") for d in decisions]
from collections import Counter
print(f"Decision source breakdown: {dict(Counter(sources))}")

# --- work package C: Granite decision cadence ---
# recorder.decision() is only called when GraniteStrategist._last_reason
# actually changes text (see ai_bot.py's call site comment: "Log only when
# the model has actually answered again, not on every frame that re-reads
# the cached answer") -- so consecutive decision timestamps are a real proxy
# for "time between fresh Granite answers", but a completed round trip that
# happens to repeat the exact same reason text verbatim is NOT logged again,
# which inflates some gaps to ~integer multiples of the true interval rather
# than reflecting a genuinely slow response. Reported as-is, not smoothed.
print(f"\n--- Work package C proxy: Granite decision cadence ---")
decision_ts = [d["t"] for d in decisions]
deltas = [decision_ts[i] - decision_ts[i - 1] for i in range(1, len(decision_ts))]
# drop any delta that spans a detected restart-gap segment boundary -- that
# gap is dead time (TORCS/bot down), not a real decision-to-decision latency
seg_boundaries_t = {states[b]["t"] for _, b in segments[:-1]}
clean_deltas = []
for i in range(1, len(decision_ts)):
    if not any(decision_ts[i - 1] <= bt <= decision_ts[i] for bt in seg_boundaries_t):
        clean_deltas.append(deltas[i - 1])
if clean_deltas:
    sd = sorted(clean_deltas)
    n = len(sd)
    p95 = sd[min(n - 1, int(round(0.95 * (n - 1))))]
    print(f"N={n} intervals (after dropping any spanning a restart gap)")
    print(f"min={min(sd):.2f}s  median={statistics.median(sd):.2f}s  "
          f"mean={statistics.mean(sd):.2f}s  p95={p95:.2f}s  max={max(sd):.2f}s")
    near_interval = sum(1 for d in sd if 4.0 <= d <= 6.0)
    print(f"Intervals within one poll cycle (4-6s): {near_interval}/{n} "
          f"({near_interval/n*100:.0f}%) -- the rest are ~integer multiples, "
          f"consistent with the text-unchanged dedup above, not slow responses")
