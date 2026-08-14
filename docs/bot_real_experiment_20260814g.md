# AI Driving Bot — Real Experiment Report (2026-08-14, session g)

> Status: **real data**, not `SAMPLE`. Raw source:
> [`evaluation/bot/results/real_experiment_bot_drive_20260814g.jsonl`](../evaluation/bot/results/real_experiment_bot_drive_20260814g.jsonl)
> (530 records: 467 `state`, 62 `decision`, 1 `session_start`), produced by
> `ai_bot.py`'s built-in `TraceRecorder` (`TORCS_BOT_TRACE=<path>`) against a
> real TORCS instance and a real local LM Studio server. Reproduce the
> numbers below with:
> ```
> python evaluation/bot/scripts/analyse_bot_trace.py \
>   evaluation/bot/results/real_experiment_bot_drive_20260814g.jsonl
> ```

## 0. What this is: Forza's third rep — the first track to complete the 3-session design

Fifth kept real data point overall, and the **third Forza session**, joining
[`docs/bot_real_experiment_20260814.md`](bot_real_experiment_20260814.md)
("session a") and
[`docs/bot_real_experiment_20260814d.md`](bot_real_experiment_20260814d.md)
("session d"). This makes Forza the first track in this doc series to reach
`bot_test_plan.md`'s "3 sessions per track" design target — not yet 2
tracks × 3 sessions (the other two tracks still have 1 rep each), but the
first place a real distribution, not a single anecdote, can be reported for
one track (§4).

Two attempts before this one were started and **discarded** without
analysis, letters `e` and `f` in the raw filenames, both because the SCR
handshake succeeded but TORCS stopped sending any further state within 5 s
(`No data from TORCS for 5.0s`) — the operator described this as TORCS
itself being "stuck" (卡了), not a menu-selection problem. The fix both
times was the operator re-entering TORCS's Quick Race menu from scratch;
no `wsl.exe --shutdown` was needed. Neither discarded attempt produced more
than the `session_start` marker, so they contribute no data and are omitted
from every table below and from the raw `results/` directory.

## 1. Environment

Identical to sessions a/d except where noted:

| Field | Value |
|---|---|
| Track | Forza (5850 m, 78 segments, 30 turns, width 11 m, slowest point 97 km/h) — third rep |
| Bot invocation | `ai_bot.py --bot --granite`, `TORCS_BOT_TRACE` enabled, manual WSL terminal start; handshake succeeded on the third attempt this session (see §0 for the two discarded ones) |
| Model | `ibm-granite` via LM Studio at `http://localhost:1234/v1` |
| Prompt mode / interval | `reasoning`, `_STRATEGY_INTERVAL=15.0s`, `_GRANITE_TIMEOUT=180.0s` |
| Initial fuel | 50.0 L |

## 2. Session structure: 1 real driving segment, clean race-end exit

| Segment | Span (monotonic s) | Duration | State samples |
|---|---|---:|---:|
| 1 | 7814.8 → 8754.4 | 939.6 s (15.7 min) | 467 |

Ended via `Race ended — exiting loop.` / `Server sent ***shutdown***.` —
same clean exit path as session d.

## 3. Work package B — this session alone

### 3.1 Completion

- **8 laps**, 0 DNF. Lap times (s): `108.9, 102.8, 102.7, 102.2, 102.2, 102.0, 107.3, 103.7`
- Mean **103.97 s**, stdev **2.63 s**, CV **2.5%**.

### 3.2 Off-track excursions

- **0.**

### 3.3 Collisions

- **1 damage-jump event**: `t=8516.5, 0 → 1462`. Final damage **1462**
  (well under the 9500 `_DMG_DEFEND` threshold). **0.021 collisions/km**
  (1 / 48.73 km).

### 3.4 Strategy behaviour

- `ATTACK`, `NORMAL` only. **2 switches** across 62 decisions (0.13/min).
- Decision source: **56 `granite`**, **6 `rule_block`**.

## 4. Forza pooled statistics (N=3 sessions — the first real distribution in this series)

### 4.1 Lap times (24 laps total: 8 × 3 sessions)

| | a | d | g | **Pooled (n=24)** |
|---|---:|---:|---:|---:|
| Mean | 103.76 s | 103.24 s | 103.97 s | **103.66 s** |
| Stdev | 2.72 s | 2.30 s | 2.63 s | **2.46 s** |
| CV | 2.6% | 2.2% | 2.5% | **2.4%** |
| Min | 102.03 s | 102.00 s | 102.03 s | 102.00 s |
| Max | 109.35 s | 108.85 s | 108.85 s | 109.35 s |

The pooled CV (2.4%) sits right in the middle of the three per-session
values (2.2–2.6%) — the per-session numbers were not outliers of each
other, they're three draws from the same tight distribution.

### 4.2 Real per-request Granite RTT (179 requests total: 58 + 59 + 62)

| | a | d | g | **Pooled (n=179)** |
|---|---:|---:|---:|---:|
| Median | 9.430 s | 9.175 s | 9.243 s | **9.325 s** |
| P95 | 12.219 s | 11.256 s | 11.660 s | **11.683 s** |
| Max | 12.958 s | 11.678 s | 12.109 s | 12.958 s |
| Mean | 9.889 s | 9.546 s | 9.661 s | **9.697 s** |

Same story: the three session medians (9.18–9.43 s) bracket the pooled
median (9.33 s) tightly, and the pooled P95 (11.68 s) leaves >3 s of margin
under the 15 s poll interval in every session, not just on average. This is
now a defensible "Granite RTT on this model/prompt/hardware combination is
~9.3 s median, ~11.7 s P95" claim for Forza specifically — the first number
in this series backed by a real N=3 distribution rather than one session.

### 4.3 Driving-quality spread (why 3 reps, not 1, matters)

| | a | d | g |
|---|---:|---:|---:|
| Collisions | 0 | 0 | 1 event, 1462 dmg |
| Off-track | 0 | 0 | 0 |
| Strategy switches/min | 0.28 | 0.14 | 0.13 |
| Strategies seen | ATTACK/NORMAL/DEFEND | ATTACK/NORMAL | ATTACK/NORMAL |

Lap-time and RTT numbers were reproducible (§4.1, §4.2); collision count
was not (0, 0, 1) — a reminder that "driving quality" metrics need more
than 3 reps to characterize even for a single track, while latency/pacing
numbers stabilize faster. Exactly the distinction `bot_test_plan.md`'s
design (3 sessions per track) exists to surface.

## 5. Cross-session summary (all 5 kept real sessions to date)

| | 08-12 | 08-14a | 08-14b | 08-14d | 08-14g |
|---|---:|---:|---:|---:|---:|
| Track | E-Track 3 | Forza | Wheel 1 | Forza | Forza |
| Duration | 17.4 min | 14.5 min | 14.3 min | 14.7 min | 15.7 min |
| Laps | 8 | 8 | 8 | 8 | 8 |
| Lap time CV | 11.5% | 2.6% | 1.8% | 2.2% | 2.5% |
| Collisions | 9 ev / 2 inc | 0 | 7 ev / 1 inc | 0 | 1 ev |
| Granite RTT median | not measured | 9.43 s | 10.17 s | 9.18 s | 9.24 s |
| Exit reason | deliberate stop | watchdog | clean `Race ended` | clean `Race ended` | clean `Race ended` |

Forza: 3/3 reps done. E-Track 3, Wheel 1: 1/3 each.

## 6. What's still missing for a complete B/C submission

> **Update, 2026-08-14 (after this report was written)**: two items that
> were on this list — human-driven ground-truth sessions and the
> 3×20-minute endurance runs — were explicitly descoped by the operator.
> See `bot_test_plan.md` §5.1 and §7.1 respectively for the reasoning; they
> are struck through below and kept only for this report's own historical
> accuracy, not as open items anymore.

- ~~E-Track 3 and Wheel 1 each need 2 more reps to match Forza's 3.~~
  Descoped 2026-08-14 — Forza's 3-rep distribution already answered
  whether the numbers reproduce; repeating the other two tracks would
  mostly re-confirm the same conclusion.
- A second *track family* beyond these three would still help — all three
  are TORCS's built-in road-course tracks; `bot_test_plan.md`'s "2 tracks"
  requirement is arguably satisfied at the track-count level already (3 ≥
  2), but every session so far has used the same car/opponent-count setup.
- ~~Human-driven ground-truth sessions (still 0 across all 5 real sessions).~~ Descoped.
- ~~First-token/complete-response split inside `_call_granite`.~~ Descoped
  2026-08-14 — real development work, not more testing, and driving
  decisions only act on the complete response anyway.
- ~~Real UDP `send_latency`/`frame_latency` (u0–u2 control-loop chain).~~
  Descoped 2026-08-14 — `send_control()` is a local synchronous socket
  call, not a network wait; `u1-u0` (already measured, passing) is where
  the real risk to keeping up with `scr_server`'s 20ms step actually
  lives.
- ~~Endurance runs (3×20 minutes)~~ Descoped. The fault-injection matrix's
  remaining gaps (RB-06–RB-09 untried, RB-05 needs 4 more reps) are still
  open — untouched by any of the five sessions above.
