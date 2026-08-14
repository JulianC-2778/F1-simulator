# AI Driving Bot — Real Experiment Report (2026-08-14)

> Status: **real data**, not `SAMPLE`. Raw source:
> [`evaluation/bot/results/real_experiment_bot_drive_20260814.jsonl`](../evaluation/bot/results/real_experiment_bot_drive_20260814.jsonl)
> (494 records: 434 `state`, 58 `decision`, 2 `session_start`), produced by
> `ai_bot.py`'s built-in `TraceRecorder` (`TORCS_BOT_TRACE=<path>`) against a
> real TORCS instance and a real local LM Studio server. Reproduce the
> numbers below with:
> ```
> python evaluation/bot/scripts/analyse_bot_trace.py \
>   evaluation/bot/results/real_experiment_bot_drive_20260814.jsonl
> ```

## 0. What this is, and how it relates to the 2026-08-12 report

This is a second real data point toward work packages B and C of
[`docs/bot_test_plan.md`](bot_test_plan.md), collected the same opportunistic
way as [`docs/bot_real_experiment_20260812.md`](bot_real_experiment_20260812.md),
on the current `main` (post PR #38-40). Two things changed since that report
that make this run more trustworthy on the specific points the earlier one
had to caveat:

1. **Pacing/model mismatch fixed.** The 08-12 session ran the reasoning
   prompt at the old flat 5 s `_STRATEGY_INTERVAL` against a model that
   needs ~7.6 s+ median to answer — likely saturating the single
   `ModelBroker` slot for most of the session (`bot_test_matrix.md` §9).
   This run used the current default (`_STRATEGY_INTERVAL=15.0s` under
   `reasoning` mode), comfortably above the real median RTT (9.43 s)
   measured below — no saturation.
2. **Real per-request RTT now exists.** `TraceRecorder.decision()` now logs
   `seq`/`round_trip_s` on every completed round trip, unconditionally, not
   only when the answer text changes — so this report has, for the first
   time, an actual Granite request→response timing figure (§4.2), not just
   decision-to-decision cadence. This is the "concrete next step" the 08-12
   report's §4 asked for.

Still **not** a full work-package-B/C design: 1 track only (not 2), fully
bot-autonomous (no human-driven ground-truth session to compare
`safety_filter`'s output against), and `round_trip_s` is a whole-request
timing (request sent → response fully parsed), not a first-token/
complete-response split — `bot_test_plan.md`'s full t0–t5 breakdown still
needs a finer timestamp hook inside `_call_granite`.

## 1. Environment

| Field | Value |
|---|---|
| Track | Forza (5850 m, 78 segments, 30 turns, width 11 m, slowest point 97 km/h) |
| Repo / commit | WSL-native `~/F1-simulator` checkout (the one TORCS actually runs from), a few commits behind the Windows-mounted checkout this evaluation tooling lives in at the time of the run (last common commit `dfca205`) — the trace file was written directly into the Windows-mounted repo's `evaluation/bot/results/` via the `/mnt/c/...` cross-mount path, so no manual copy step was needed |
| Bot invocation | `ai_bot.py --bot --granite`, `TORCS_BOT_TRACE` enabled, started manually from a WSL terminal (not the Dashboard's `Start ai_bot.py` button, so `/api/bot/process/status` doesn't track this pid — expected and documented in `full-stack-e2e-startup.md` §7) |
| Model | `ibm-granite` via LM Studio at `http://localhost:1234/v1` (already configured in midware's in-memory config from an earlier session that day) |
| Prompt mode / interval | `reasoning` (default), `_STRATEGY_INTERVAL=15.0s`, `_GRANITE_TIMEOUT=180.0s` |
| Initial fuel | 50.0 L |

## 2. Session structure: 1 real driving segment (plus one failed handshake attempt)

Two `session_start` records exist in the trace (`331ad5cd` then `c34c8bb2`)
because the first `ai_bot.py` invocation failed its SCR handshake
(`ConnectionError: TORCS did not respond at localhost:3001 after 5 attempts`
— TORCS had moved past the `Initializing Driver` screen after the previous
bot process was stopped for an unrelated reason) and exited before logging
any state samples. The operator re-entered TORCS's Quick Race menu and
stopped again at `Initializing Driver`; the second invocation connected
cleanly. `analyse_bot_trace.py`'s segment detector correctly reports this as
a single active driving segment, since the failed first session left no
state rows to segment against.

The session ended on its own after ~14.5 minutes via the
`_CONNECTION_LOST_FRAMES` watchdog (`No data from TORCS for 5.0s — assuming
the connection is dead. Exiting loop.`), consistent with the TORCS Quick
Race session itself finishing (8 laps completed, `remaining_laps` reads 1
shortly before the drop) rather than a crash: `torcs-bin` was still running
afterward and the process log has no traceback.

| Segment | Span (monotonic s) | Duration | State samples |
|---|---|---:|---:|
| 1 | 2421.2 → 3293.8 | 872.6 s (14.5 min) | 434 |

## 3. Work package B — bot autonomous-driving scenario

### 3.1 Completion

- **8 laps completed**, 0 DNF, 0 stalls requiring intervention.
- Lap times (s): `109.3, 102.6, 106.6, 102.2, 102.2, 102.2, 102.9, 102.0`
- Mean **103.76 s**, stdev **2.72 s**, min **102.03 s**, max **109.35 s**,
  CV **2.6%** — far tighter than 08-12's 11.5% CV; no mid-session incident
  this time (see §3.3).
- The session ended via the connection-lost watchdog after the race itself
  finished, not a deliberate stop or a car failure.

### 3.2 Off-track excursions

- **0 excursions** (`|track_pos| > 1` never triggered).

### 3.3 Collisions (damage increases)

- **0 damage-jump events.** Final damage **0**. **0 collisions per km**
  (0 / 47.54 km raced).

### 3.4 Strategy behaviour

- Strategies seen: `ATTACK`, `NORMAL`, `DEFEND` (`PIT`/`BLOCK` never
  triggered — consistent with damage staying at 0 and fuel never running
  low across a 14.5-minute run from a 50 L tank).
- **4 strategy value changes** across 58 logged decisions (0.28/min).
- Decision source breakdown: **52 `granite`**, **6 `rule_block`**.

## 4. Work package C proxy — decision cadence, and for the first time, real per-request RTT

### 4.1 Decision-cadence proxy (same metric as the 08-12 report, for comparability)

| Metric | Value |
|---|---:|
| N (decision-to-decision intervals) | 57 |
| Min | 11.29 s |
| Median | 15.07 s |
| Mean | 15.03 s |
| P95 | 17.88 s |
| Max | 19.04 s |
| Intervals within one poll cycle (4–6 s) | 0/57 (0%) — expected now: the poll cycle itself is 15 s, not 5 s |

Unlike the 08-12 numbers, this is no longer confounded by the
phrasing-dedup effect described there: `TraceRecorder.decision()` is called
on every completed round trip unconditionally now (§0), and `seq` in the
trace is a clean, gapless `1..58` — every logged decision really is a
distinct completed request, not a same-text re-confirmation inflating the
apparent gap.

### 4.2 Real per-request Granite RTT (`round_trip_s`, new field, not available in the 08-12 report)

Computed directly from the `round_trip_s` field `GraniteStrategist` now
stamps on every decision record:

| Metric | Value |
|---|---:|
| N | 58 |
| Min | 8.035 s |
| Median | 9.430 s |
| P95 | 12.219 s |
| Max | 12.958 s |
| Mean | 9.889 s |

This is the first real measurement in this codebase of actual Granite
request→response time, as opposed to decision-cadence (which is confounded
by the poll interval). It comfortably fits inside both the 15 s poll
interval and the 180 s `_GRANITE_TIMEOUT` for reasoning mode — no
saturation this time, unlike the 08-12 session's likely-saturated single
`ModelBroker` slot (§0). It is still a whole-request figure (request sent →
response fully parsed), not a first-token timestamp — `bot_test_plan.md`'s
full t0–t5 breakdown would still need an intermediate hook inside
`_call_granite`.

## 5. What's still missing for a complete B/C submission

- A second track, and enough sessions per track to reach the 2×3 design —
  still only 1 track *per session* across both real sessions to date
  (E-Track 3 on 08-12, Forza here); no track has 2 sessions yet.
- Human-driven sessions with independently-recorded ground truth (this
  session, like 08-12, was 100% bot-autonomous).
- First-token/complete-response split inside `_call_granite` — `round_trip_s`
  closes the "no real Granite RTT measurement at all" gap flagged in
  08-12's §5, but not the finer t0–t5 breakdown.
- Real UDP `send_latency`/`frame_latency` (the u0–u2 control-loop chain over
  the wire) — still not captured; `tests/bot/test_control_loop_latency.py`
  covers the compute-only `u1-u0` piece synthetically instead (see
  `evaluation/bot/README.md`'s "Capturing real latency data" section).
- A third repetition and a repeat of the same track — 2 real sessions now
  exist (08-12, 08-14), still not enough for a distribution, and driving
  quality (0 collisions here vs. 2 real incidents on 08-12) already shows
  session-to-session variance worth quantifying rather than trusting one
  run.
