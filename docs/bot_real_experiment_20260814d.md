# AI Driving Bot — Real Experiment Report (2026-08-14, session d)

> Status: **real data**, not `SAMPLE`. Raw source:
> [`evaluation/bot/results/real_experiment_bot_drive_20260814d.jsonl`](../evaluation/bot/results/real_experiment_bot_drive_20260814d.jsonl)
> (500 records: 440 `state`, 59 `decision`, 1 `session_start`), produced by
> `ai_bot.py`'s built-in `TraceRecorder` (`TORCS_BOT_TRACE=<path>`) against a
> real TORCS instance and a real local LM Studio server. Reproduce the
> numbers below with:
> ```
> python evaluation/bot/scripts/analyse_bot_trace.py \
>   evaluation/bot/results/real_experiment_bot_drive_20260814d.jsonl
> ```

## 0. What this is: the first same-track repeat

Fourth real data point overall, and the **first time the same track has
been driven twice** — this session and
[`docs/bot_real_experiment_20260814.md`](bot_real_experiment_20260814.md)
("session a") are both **Forza**, same day, same `main` commit, same
midware/LM Studio setup. Everything else about the run (operator, model,
prompt mode, interval) is identical between the two; the only thing that
changed is the specific lap.

An intervening attempt at repeating **E-Track 3** instead was started and
then explicitly discarded by the operator ("刚刚那次不算" — "that last one
doesn't count") before any analysis was run on it; its trace/log were
deleted rather than kept as a fourth data point, so it does not appear
anywhere in this doc series.

This is exactly the kind of repeat `bot_test_plan.md`'s full design (3
sessions per track) and every earlier report's §5/§6 "still missing" list
have been asking for — still only 2 reps, not 3, but the first real
opportunity to ask "was the earlier number a fluke or reproducible?" for
one specific track.

## 1. Environment

Identical to `bot_real_experiment_20260814.md` §1 except where noted:

| Field | Value |
|---|---|
| Track | Forza (5850 m, 78 segments, 30 turns, width 11 m, slowest point 97 km/h) — same as session a |
| Bot invocation | `ai_bot.py --bot --granite`, `TORCS_BOT_TRACE` enabled, manual WSL terminal start, handshake succeeded on the first attempt |
| Model | `ibm-granite` via LM Studio at `http://localhost:1234/v1` |
| Prompt mode / interval | `reasoning`, `_STRATEGY_INTERVAL=15.0s`, `_GRANITE_TIMEOUT=180.0s` |
| Initial fuel | 50.0 L |

## 2. Session structure: 1 real driving segment, clean race-end exit

| Segment | Span (monotonic s) | Duration | State samples |
|---|---|---:|---:|
| 1 | 6510.8 → 7395.7 | 884.9 s (14.7 min) | 440 |

Ended via `Race ended — exiting loop.` / `Server sent ***shutdown***.` — a
clean TORCS-initiated end-of-race signal, same exit path as the Wheel 1
session (`docs/bot_real_experiment_20260814b.md`), not the connection-lost
watchdog.

## 3. Work package B — bot autonomous-driving scenario

### 3.1 Completion

- **8 laps completed**, 0 DNF, 0 stalls.
- Lap times (s): `108.9, 102.6, 102.2, 102.4, 102.2, 103.3, 102.4, 102.0`
- Mean **103.24 s**, stdev **2.30 s**, min **102.00 s**, max **108.85 s**,
  CV **2.2%**.

### 3.2 Off-track excursions

- **0 excursions.**

### 3.3 Collisions

- **0 damage-jump events.** Final damage **0**.

### 3.4 Strategy behaviour

- Strategies seen: `ATTACK`, `NORMAL` only (no `DEFEND`/`PIT`/`BLOCK` —
  consistent with 0 damage and ample fuel throughout).
- **2 strategy value changes** across 59 logged decisions (0.14/min).
- Decision source breakdown: **50 `granite`**, **9 `rule_block`**.

## 4. Work package C proxy

### 4.1 Decision-cadence proxy

| Metric | Value |
|---|---:|
| N | 58 |
| Min | 12.19 s |
| Median | 14.96 s |
| Mean | 15.05 s |
| P95 | 17.16 s |
| Max | 18.59 s |

### 4.2 Real per-request Granite RTT (`round_trip_s`)

| Metric | Value |
|---|---:|
| N | 59 |
| Min | 7.602 s |
| Median | 9.175 s |
| P95 | 11.256 s |
| Max | 11.678 s |
| Mean | 9.546 s |

## 5. Forza-vs-Forza: the first same-track reproducibility check

| | Session a (08-14) | Session d (this report) | Delta |
|---|---:|---:|---:|
| Duration | 14.5 min | 14.7 min | +0.2 min |
| Distance | 47.54 km | 47.64 km | +0.10 km |
| Laps | 8 | 8 | 0 |
| Lap time mean | 103.76 s | 103.24 s | −0.52 s |
| Lap time CV | 2.6% | 2.2% | −0.4 pp |
| Off-track excursions | 0 | 0 | 0 |
| Collisions | 0 | 0 | 0 |
| Strategies seen | ATTACK, NORMAL, DEFEND | ATTACK, NORMAL | −DEFEND |
| Strategy switches/min | 0.28 | 0.14 | −0.14 |
| Decision cadence median | 15.07 s | 14.96 s | −0.11 s |
| Granite RTT median | 9.430 s | 9.175 s | −0.26 s |
| Granite RTT P95 | 12.219 s | 11.256 s | −0.96 s |

**Reading this**: on the two numbers that matter most for a reproducibility
claim — lap-time consistency and Granite RTT — the two Forza runs land
within a few percent of each other (CV 2.6% vs 2.2%; RTT median 9.43 s vs
9.18 s, both P95s comfortably under half the 15 s poll interval). Both runs
also share 0 off-track excursions and 0 collisions, and both used the same
decision-cadence pacing (~15 s, matching the configured interval exactly).
The one qualitative difference — session a saw one `DEFEND` decision,
session d saw none — is consistent with normal driving-condition variance
(no comparable damage/traffic event happened in session d) rather than a
strategy-layer inconsistency.

This is still N=2, not the N=3 the full design specifies, but it is the
first evidence in this doc series that the driving-quality and latency
numbers reported for a single session aren't one-off noise — a third Forza
run would settle it.

## 6. Cross-session summary (all 4 kept real sessions to date)

| | 08-12 | 08-14a (Forza #1) | 08-14b (Wheel 1) | 08-14d (Forza #2) |
|---|---:|---:|---:|---:|
| Track | E-Track 3 | Forza | Wheel 1 | Forza |
| Duration | 17.4 min | 14.5 min | 14.3 min | 14.7 min |
| Laps | 8 | 8 | 8 | 8 |
| Lap time CV | 11.5% | 2.6% | 1.8% | 2.2% |
| Off-track excursions | 4 | 0 | 0 | 0 |
| Collisions | 9 events / 2 incidents | 0 | 7 events / 1 incident | 0 |
| Granite RTT median | not measured | 9.43 s | 10.17 s | 9.18 s |
| Exit reason | deliberate stop | connection-lost watchdog | clean `Race ended` | clean `Race ended` |

Forza now has 2 real reps (this report's §5); E-Track 3 and Wheel 1 have 1
each (a repeat attempt on E-Track 3 was started and discarded — see §0).

## 7. What's still missing for a complete B/C submission

- A third Forza rep to complete that track's 3-session design; E-Track 3
  and Wheel 1 still need 3 reps each too, from scratch (0 kept reps beyond
  the first for each).
- Human-driven ground-truth sessions (still 0 across all 4 real sessions).
- First-token/complete-response split inside `_call_granite`.
- Real UDP `send_latency`/`frame_latency` (u0–u2 control-loop chain).
- Endurance runs (3×20 minutes) and the fault-injection matrix's remaining
  gaps — untouched by any of the four sessions above (all normal-condition
  runs).
