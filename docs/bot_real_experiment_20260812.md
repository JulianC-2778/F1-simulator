# AI Driving Bot — Real Experiment Report (2026-08-12)

> Status: **real data**, not `SAMPLE`. Raw source:
> [`evaluation/bot/results/real_experiment_bot_drive_20260812.jsonl`](../evaluation/bot/results/real_experiment_bot_drive_20260812.jsonl)
> (620 records: 518 `state`, 102 `decision`), produced by `ai_bot.py`'s
> built-in `TraceRecorder` (`TORCS_BOT_TRACE=<path>`) against a real TORCS
> instance and a real local LM Studio server. Reproduce the numbers below
> with:
> ```
> python evaluation/bot/scripts/analyse_bot_trace.py \
>   evaluation/bot/results/real_experiment_bot_drive_20260812.jsonl
> ```

## 0. What this is and isn't

This is a first real data point toward work packages B and C of
[`docs/bot_test_plan.md`](bot_test_plan.md), collected opportunistically in
one session — **not** the full designs those sections specify. Differences,
stated up front rather than glossed over:

| | Full design (`bot_test_plan.md`) | What actually happened here |
|---|---|---|
| Work package B | 2 tracks × 3 sessions, human-driven, ground-truth strategy labels vs. `safety_filter` output, P/R/F1 per strategy | 1 track (E-Track 3), bot fully autonomous (`--bot --granite`), objective driving-quality metrics only (§5.3's "bot 自驾场景" variant) — no human ground-truth labelling was done |
| Work package C | 30 independent triggers, full t0–t5 breakdown (detection → first token → complete → caption → audio) | Only "time between two `decision` log entries" is observable from `TraceRecorder` — no first-token-equivalent timestamp exists inside a single Granite round trip in this codebase yet |

## 1. Environment

| Field | Value |
|---|---|
| Track | E-Track 3 (4208 m, 70 segments, 36 turns) |
| Repo / commit | `home/abcdz/F1-simulator` (WSL-native checkout), `main`, includes PR #37 (`f4ff6ed`) + local pit-system/reasoning-trace commits |
| Bot invocation | `ai_bot.py --bot --granite`, `TORCS_BOT_TRACE` enabled |
| Model | `granite-4.1-8b` via LM Studio at `http://172.21.160.1:1234` (Windows host, reached from WSL2) |
| `_STRATEGY_INTERVAL` | 5.0 s (default, unmodified) |
| Initial fuel | 50.0 L |

## 2. Session structure: 2 real segments, not 1

`time.monotonic()` on this Linux/WSL setup is system-wide (effectively
since boot), not per-process — the trace file was appended to across two
separate `ai_bot.py` invocations today (the process was stopped and
restarted once, when TORCS itself needed to be re-entered into a race).
Naively subtracting the first record's timestamp from the last would claim
a 54.7-minute session; the two real invocations only actually drove for
**17.4 minutes combined**. The analysis script detects the gap
(>15 s between consecutive 2-second state samples) and reports both
numbers — always use the segmented one.

| Segment | Span (monotonic s) | Duration | State samples |
|---|---|---:|---:|
| 1 | 150.3 → 576.0 | 425.7 s (7.1 min) | 212 |
| 2 | 2815.9 → 3431.4 | 615.5 s (10.3 min) | 306 |
| **Total active driving** | | **1041.2 s (17.4 min)** | 518 |

## 3. Work package B — bot autonomous-driving scenario

### 3.1 Completion

- **8 laps completed**, 0 DNF, 0 stalls requiring intervention.
- Lap times (s): `107.9, 102.3, 137.9, 107.4, 102.6, 102.1, 121.1, 103.0`
- Mean **110.55 s**, stdev **12.72 s**, min **102.15 s**, max **137.88 s**,
  CV **11.5%** — the one slow lap (137.9 s) lines up with the first
  collision cluster's timestamp (see §3.3).
- Trace ends with `laps_left=2` — the session was stopped deliberately
  (to move to work package C's latency measurement), not a car failure.

### 3.2 Off-track excursions

- **4 excursions** (`|track_pos| > 1`), **4 recovered (100%)**, none still
  off-track when the trace ends.

### 3.3 Collisions (damage increases)

- **9 damage-jump events**, clustering into **2 real incidents** (one per
  segment), not 9 independent hits:

| Segment | Incident | Damage progression | Duration |
|---|---|---|---|
| 1 | t=233.3–428.8 | 0 → 336 → 853 → 1254 → 1357 → 1379 → 2157 | ~196 s window, 6 jumps |
| 2 | t=3217.7–3225.8 | 0 → 377 → 1443 → 1446 | ~8 s window, 3 jumps |

- Final damage in each segment stayed below `_DMG_DEFEND` (9500) and
  `_DMG_NO_ATTACK` (8000) — `safety_filter` never had to force `DEFEND` or
  downgrade `ATTACK` in this session; both incidents were survivable by
  design margin, not because the safety net intervened.
- **0.392 collision events per km** (9 events / 22.95 km raced).
- Segment 1's incident is a genuine multi-hit pileup (6 jumps over ~3
  minutes) worth reviewing against a screen recording if one exists — it's
  the kind of pattern that's ambiguous between "several real separate
  contacts" and "one contact whose damage value the SCR telemetry reported
  across several ticks."

### 3.4 Strategy behaviour

- Only `ATTACK` and `NORMAL` were ever selected — `DEFEND`/`PIT`/`BLOCK`
  never triggered (consistent with damage staying under their thresholds
  and fuel never getting low in a 17-minute run from a 50 L tank).
- **8 strategy value changes** across 102 logged decisions.
- Decision source breakdown: **82 `granite`**, **20 `rule_block`** — about
  80% of logged strategy re-confirmations came from fresh Granite text,
  20% from the rule layer re-labelling the same frame.

## 4. Work package C proxy — Granite decision cadence

`TraceRecorder.decision()` is only called when
`GraniteStrategist._last_reason` text actually changes (see the call site
comment in `ai_bot.py`: *"Log only when the model has actually answered
again, not on every frame that re-reads the cached answer"*) — so this
measures **time between genuinely new Granite answers**, not literally
every completed HTTP round trip. When two consecutive answers happen to
repeat the exact same phrasing, the interval between the *next* distinct
answer and the last logged one comes out looking like an integer multiple
of the true poll interval. This is reported as-is below rather than
filtered out, because it's a real, honestly-observed property of this
session — not a proxy for network/model slowness.

| Metric | Value |
|---|---:|
| N (decision-to-decision intervals, restart-gap-spanning ones excluded) | 100 |
| Min | 4.38 s |
| Median | 9.88 s |
| Mean | 10.23 s |
| P95 | 21.25 s |
| Max | 35.19 s |
| Intervals within one poll cycle (4–6 s) | 28/100 (28%) |

**Reading this correctly**: the *minimum* observed interval (4.38 s) is
below `_STRATEGY_INTERVAL` (5.0 s) only by normal timing jitter, and is the
cleanest single data point for "a real Granite round trip fits inside one
5 s poll cycle with room to spare, plus text that happened to differ from
the previous answer." The median (9.88 s) is dominated by the phrasing-dedup
effect above, not by the model being slow — this session's Granite replies
were verbally repetitive ("Low damage and ample fuel allow aggressive
pursuit." recurring near-verbatim), which is a prompt/persona property, not
a latency one. **This number should not be quoted as "Granite median
latency" without the caveat above** — it measures decision-cadence as
observed by the dashboard/log, not model response time.

No finer breakdown (first-token, complete-response) is possible from this
trace format — `_call_granite` has no intermediate timestamp today. Adding
one (mirroring `midware/latency_log.py`'s approach for commentary) is the
concrete next step if a real first-token/complete-response split is wanted.

## 5. What's still missing for a complete B/C submission

- A second track, and enough sessions per track to reach the 2×3 design.
- Human-driven sessions with independently-recorded ground truth (this
  session was 100% bot-autonomous — no human strategy judgement to compare
  `safety_filter`'s output against).
- Code-level first-token/complete-response timestamps inside
  `GraniteStrategist._call_granite` for a true latency breakdown, not just
  decision cadence.
- A second and third repetition of this same scenario, since driving
  quality (collision count especially) can vary a lot session to session —
  one 17-minute run is a data point, not a distribution.
