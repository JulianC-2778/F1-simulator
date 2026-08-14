# AI Driving Bot — Real Experiment Report (2026-08-14, session b)

> Status: **real data**, not `SAMPLE`. Raw source:
> [`evaluation/bot/results/real_experiment_bot_drive_20260814b.jsonl`](../evaluation/bot/results/real_experiment_bot_drive_20260814b.jsonl)
> (485 records: 427 `state`, 57 `decision`, 1 `session_start`), produced by
> `ai_bot.py`'s built-in `TraceRecorder` (`TORCS_BOT_TRACE=<path>`) against a
> real TORCS instance and a real local LM Studio server. Reproduce the
> numbers below with:
> ```
> python evaluation/bot/scripts/analyse_bot_trace.py \
>   evaluation/bot/results/real_experiment_bot_drive_20260814b.jsonl
> ```

## 0. What this is, and how it relates to the two earlier reports

Third real data point toward work packages B and C, collected the same
session as [`docs/bot_real_experiment_20260814.md`](bot_real_experiment_20260814.md)
(same day, same `main` commit, same midware/LM Studio setup) but against a
**third distinct track** the operator selected after reopening TORCS —
directly extending the "only 1 track per session so far" gap the first
08-14 report flagged in its §5.

Two things are new in this session compared to both earlier reports:

1. **First real collision incident captured cleanly end-to-end.** Both prior
   sessions (08-12, 08-14 first session) had 0 collisions or a messier
   multi-jump cluster; this session has one clear ~370 s incident window
   (§3.3) with the car recovering and finishing the race regardless — a
   useful case for `safety_filter`'s `DEFEND` threshold behavior, which
   visibly triggered here (§3.4) where it never had before in real data.
2. **First real "clean race end" exit**, not the `_CONNECTION_LOST_FRAMES`
   watchdog. The process log shows `Race ended — exiting loop.` followed by
   `Server sent ***shutdown***.` — TORCS's own SCR server sent an explicit
   end-of-race signal that `run_bot()` handled directly, rather than the
   watchdog inferring a dead connection from a timeout streak (which is
   what ended both earlier sessions). Confirms the normal-termination path
   works against a real server, not just the timeout-based one already
   regression-tested in `tests/bot/test_run_bot_integration.py`.

Same scope caveats as both earlier reports still apply: fully
bot-autonomous (no human ground truth), `round_trip_s` is whole-request
only (no first-token split).

## 1. Environment

| Field | Value |
|---|---|
| Track | Wheel 1 (4445 m, 65 segments, 26 turns, width 14 m, slowest point 71 km/h) |
| Repo / commit | Same WSL-native `~/F1-simulator` checkout as the first 08-14 session; trace written directly into the Windows-mounted repo's `evaluation/bot/results/` via the `/mnt/c/...` cross-mount path |
| Bot invocation | `ai_bot.py --bot --granite`, `TORCS_BOT_TRACE` enabled, started manually from a WSL terminal (handshake succeeded on the first attempt this time — TORCS was already sitting at `Initializing Driver` when the process launched) |
| Model | `ibm-granite` via LM Studio at `http://localhost:1234/v1` |
| Prompt mode / interval | `reasoning` (default), `_STRATEGY_INTERVAL=15.0s`, `_GRANITE_TIMEOUT=180.0s` |
| Initial fuel | 50.0 L |

## 2. Session structure: 1 real driving segment, clean race-end exit

Single `session_start` record (`dbe1f5aa`) — unlike the first 08-14 session,
there was no failed handshake attempt this time.

| Segment | Span (monotonic s) | Duration | State samples |
|---|---|---:|---:|
| 1 | 4777.8 → 5636.2 | 858.4 s (14.3 min) | 427 |

Ended via `Race ended — exiting loop.` / `Server sent ***shutdown***.` — a
genuine TORCS-initiated end-of-race signal, not the connection-lost
watchdog that ended both earlier sessions (§0).

## 3. Work package B — bot autonomous-driving scenario

### 3.1 Completion

- **8 laps completed**, 0 DNF, 0 stalls.
- Lap times (s): `108.5, 103.1, 107.4, 104.2, 104.0, 105.2, 103.8, 103.9`
- Mean **105.02 s**, stdev **1.93 s**, min **103.14 s**, max **108.52 s**,
  CV **1.8%** — the tightest of the three real sessions so far, despite
  this session having the only real collision incident (§3.3) — the
  incident cost time within a lap, not consistency across laps.

### 3.2 Off-track excursions

- **0 excursions.**

### 3.3 Collisions (damage increases)

- **7 damage-jump events**, all part of one real incident window:

| t (monotonic s) | Damage progression |
|---:|---|
| 4880.3 | 0 → 83 |
| 5065.7 | 83 → 539 |
| 5067.7 | 539 → 1147 |
| 5077.8 | 1147 → 1185 |
| 5093.9 | 1185 → 1776 |
| 5245.1 | 1776 → 1897 |
| 5247.1 | 1897 → 1940 |

- Final damage **1940** (out of the `_DMG_DEFEND` threshold at 9500 —
  survivable by a wide margin). **0.202 collisions per km** (7 events /
  34.72 km raced).
- Two separate clusters are visible (t≈4880 and t≈5066–5094, ~200 s apart,
  then a small follow-up at t≈5245–5247) rather than one continuous pileup —
  consistent with "hit something, recovered, got clipped again" rather than
  a single stuck-against-a-wall event, though only a screen recording could
  confirm that definitively.

### 3.4 Strategy behaviour

- Strategies seen: `ATTACK`, `NORMAL`, **`DEFEND`** — the first real session
  where `DEFEND` actually triggered (both 08-12 and the first 08-14 session
  stayed under the damage threshold the whole time). Its `reasoning`
  block explicitly cites damage level as the reason to reject `ATTACK`
  (`"high risk of damage with current moderate damage level"`), i.e. the
  strategy layer responded to the real damage spike from §3.3 as intended.
- **18 strategy value changes** across 57 logged decisions (1.26/min) —
  noticeably more churn than either earlier session (0.28/min both times),
  driven by the post-collision `ATTACK`↔`DEFEND`↔`NORMAL` back-and-forth.
- Decision source breakdown: **46 `granite`**, **11 `rule_block`**.

## 4. Work package C proxy — decision cadence and real per-request RTT

### 4.1 Decision-cadence proxy

| Metric | Value |
|---|---:|
| N | 56 |
| Min | 11.38 s |
| Median | 15.05 s |
| Mean | 15.07 s |
| P95 | 17.72 s |
| Max | 18.05 s |

Matches the first 08-14 session closely (median 15.07 s there vs. 15.05 s
here) — the 15 s reasoning-mode pacing is reproducible across sessions and
tracks, not a one-off.

### 4.2 Real per-request Granite RTT (`round_trip_s`)

| Metric | Value |
|---|---:|
| N | 57 |
| Min | 9.017 s |
| Median | 10.174 s |
| P95 | 12.556 s |
| Max | 13.321 s |
| Mean | 10.463 s |

Slightly higher than the first 08-14 session's median (9.43 s vs. 10.17 s
here) — both comfortably inside the 15 s poll interval either way, but the
~0.7 s gap is itself a small real data point for "RTT varies session to
session," worth remembering before treating either single number as *the*
Granite latency figure.

## 5. Cross-session comparison (all 3 real sessions to date)

| | 08-12 | 08-14 (session a) | 08-14 (session b, this report) |
|---|---:|---:|---:|
| Track | E-Track 3 | Forza | Wheel 1 |
| Duration | 17.4 min (2 segments) | 14.5 min | 14.3 min |
| Laps | 8 | 8 | 8 |
| Lap time CV | 11.5% | 2.6% | 1.8% |
| Off-track excursions | 4 (100% recovered) | 0 | 0 |
| Collisions | 9 events / 2 incidents | 0 | 7 events / 1 incident |
| Strategy switches/min | ~0.46 (8/17.4) | 0.28 | 1.26 |
| Granite RTT median | not measured (pre-`round_trip_s`) | 9.43 s | 10.17 s |
| Exit reason | deliberate stop (moved to work package C) | connection-lost watchdog | clean `Race ended` server signal |

Three different tracks, three different exit paths, one real collision
incident and one real `DEFEND` trigger — useful spread for a handful of
sessions, but still nowhere near the repetition (`bot_test_plan.md` wants
3 sessions *per* track) needed to call any single number here
representative rather than anecdotal.

## 6. What's still missing for a complete B/C submission

- 3 sessions on 3 different tracks, none repeated — the full design wants
  the *same* track run 3 times to get a distribution, not 3 different
  tracks once each.
- Human-driven ground-truth sessions (still 0 across all 3 real sessions).
- First-token/complete-response split inside `_call_granite`.
- Real UDP `send_latency`/`frame_latency` (u0–u2 control-loop chain) —
  still not captured; see `evaluation/bot/README.md`.
- Endurance runs (3×20 minutes) and the fault-injection matrix's remaining
  gaps (`docs/bot_fault_injection_20260812.md` §5) are both still untouched
  by any of the three sessions above, which were all normal-condition runs.
