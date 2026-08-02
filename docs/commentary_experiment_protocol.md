# AI Live Commentary — Real Experiment Protocol (work packages B/C/D)

> Everything in this document requires a human operator with a working
> TORCS + LM Studio/Granite + Overlay stack (L4/L5 per `docs/testing-plan.md`).
> None of it can run in CI or without that environment — see
> `docs/commentary_test_matrix.md` section 6 for what was and wasn't
> possible to automate. Nothing in this repository claims these experiments
> have been run for real; every script that consumes their output insists
> on a `SAMPLE`/`real_experiment` filename distinction (see
> `evaluation/commentary/README.md`).

Reuses the environment setup, WebSocket listener and launch sequence
already documented in `docs/manual-test-guide.md` sections 0.1-0.4 and
5.0 — read those first if you haven't run L5 before. All commands below
assume you're in `~/summer-project/F1-simulator` with `.venv` active.

## 0. Before every session

```bash
bash tools/run_tests.sh --service   # L0-L3 must be green first
python evaluation/commentary/scripts/validate_experiment_data.py --help  # sanity: scripts importable
```

Fill in `evaluation/commentary/config.example.yaml` → copy to a
session-specific `config_<DATE>.yaml` and record it alongside your raw
data. Fix `git_commit` (`git rev-parse HEAD`), `commentary_mode`,
`detection_interval_s`, `temperature`, `max_tokens`, `streaming`,
`tts_enabled` for the entire batch of sessions below — work package C's
statistics are only comparable across trials run under identical config.

Start the stack per `docs/manual-test-guide.md` 5.0 method B, plus the
WebSocket listener from 0.4 running in its own terminal the whole time —
it's how you'll notice `ai_start`/`ai_done`/`error` events live.

## 1. Work package B — real race event ground truth (section 6)

**Design**: 2 tracks × 3 sessions each = 6 sessions, 5-8 minutes each
(~30-45 min total). Human-driven (not bot), so you can deliberately create
events. `pace_update` is checked separately (see step 1.4) and does not
count toward ground truth / event F1 — do not log it as a `GT####` row.

### 1.1 Start a session

```bash
# Session naming: S<track-letter><session-number>, e.g. SA1, SA2, SA3 (track A),
# SB1, SB2, SB3 (track B). Record the exact wall-clock start time (UTC) --
# this is session time zero, everything in the ground-truth CSV is
# seconds-since-this-moment, matching sim_time semantics.
date -u +%Y-%m-%dT%H:%M:%SZ   # note this down as the session's t=0
cd ~/projects/for_summer_project
TORCS_PLAYER_UDP_HOST=127.0.0.1 TORCS_PLAYER_UDP_PORT=3101 \
  bash ~/summer-project/F1-simulator/torcs_launcher.sh
# Quick Race -> pick the track -> scr_server 1 as usual
```

Simultaneously start a screen recording (for later cross-checking
ambiguous events) and, if you want a raw telemetry copy independent of the
midware's own 30s ring buffer, tail it:

```bash
watch -n 2 'curl -s 127.0.0.1:8880/api/telemetry/history?seconds=30 \
  | python -c "import json,sys; d=json.load(sys.stdin); print(len(d[\"frames\"]), d[\"status\"])"'
```

### 1.2 During the session, actively create events

Aim for **at least 10 real instances per event type across the whole
6-session batch** (not per session): `contact`, `position_change`,
`off_track`, `lap_complete`, `battle`, `pace_surge`. Drive deliberately to
provoke each — e.g. brake late and clip an opponent for `contact`, run
wide on purpose for `off_track`, tuck in behind an opponent and swap
positions for `battle`/`position_change`, floor the throttle out of a slow
corner for `pace_surge`.

### 1.3 Stop a session and log ground truth

```bash
# stop: return to pits / quit race, or Ctrl+C the launcher
date -u +%Y-%m-%dT%H:%M:%SZ   # session end time, for your own records
```

Immediately after (while memory is fresh, cross-checked against the screen
recording), fill in a `ground_truth` CSV row per real event, using
`evaluation/commentary/templates/ground_truth_template.csv`'s schema.
`start_time_s`/`end_time_s` are seconds since the session's t=0 logged in
1.1. Validate before moving on:

```bash
python evaluation/commentary/scripts/validate_experiment_data.py \
  --kind ground_truth --file results/real_experiment_ground_truth_<DATE>.csv
```

### 1.4 Detected events + pace_update interval check

Pull what the engine actually detected for the same session from its
rolling buffer (`commentary_engine.recent_events`, capped at the last 25 —
poll during/right after the session, don't wait):

```bash
curl -s 127.0.0.1:8880/api/events/recent | python -m json.tool
```

Transcribe non-`pace_update` entries into a `detected_events` CSV
(`evaluation/commentary/templates/detected_events_template.csv` schema),
using the WebSocket listener's `[event_detected]` log lines for the
matching detection timestamps. Separately, list every `pace_update`
timestamp and confirm consecutive gaps are close to `baseline_interval`
(default 10s) — report this as its own small table, not mixed into the P/R/F1
run.

### 1.5 Score it

```bash
python evaluation/commentary/scripts/match_events.py \
  --ground-truth results/real_experiment_ground_truth_<DATE>.csv \
  --detections results/real_experiment_detected_events_<DATE>.csv \
  --tolerance 1.0 \
  --out-dir results/
```

Keep the real result even if it misses the discussion targets in
`commentary_test_plan.md` 6.4 (Overall P/R/F1 ≥ 0.80, key-event recall ≥
0.70) — report the miss and the error breakdown, don't adjust the
tolerance or the matching rule after seeing the number.

## 2. Work package C — end-to-end latency (section 7)

**Design**: 30 independent event-triggered generations, fixed config (see
step 0), the same session/config metadata recorded once for the whole
batch.

### 2.1 Enable latency capture

```bash
# in the terminal that runs midware.app, before starting it:
COMMENTARY_LATENCY_LOG=1 \
COMMENTARY_LATENCY_LOG_PATH=results/real_experiment_latency_<DATE>.jsonl \
python -m midware.app
```

This captures `t1_event_detected`, `t2_first_token`, `t3_ai_done`
per request (see `midware/latency_log.py`). Two stages it can *not*
capture from the backend:

- `t0_telemetry_received`: use the triggering frame's own `sim_time` from
  `/api/events/recent`'s logged event (or the ground-truth timestamp if
  you're reusing a work-package-B session) as t0.
- `t4_caption_displayed` / `t5_tts_started`: Overlay/browser-side. Either
  add a one-line `console.log(performance.now())` at the top of
  `overlay-app/src/renderer.js`'s `case 'ai_done':`/`'tts_audio':` handlers
  for this session only (revert after), or approximate from the screen
  recording's timestamps against the WebSocket listener's own printed
  timestamps.

### 2.2 Run 30 triggers and reshape into the latency CSV

Drive/trigger 30 events (reuse work-package-B sessions, or `curl -s -X POST
127.0.0.1:8880/api/commentary/manual -d '{"prompt":"..."}'` for
manually-forced ones — note that manual-prompt commentary does *not* get a
`t1_event_detected` from the auto-loop, so prefer real driving-triggered
events for a representative sample). Convert the JSONL + your t0/t4/t5
notes into `evaluation/commentary/templates/latency_template.csv`'s
column layout (one row per `request_id`) and validate:

```bash
python evaluation/commentary/scripts/validate_experiment_data.py \
  --kind latency --file results/real_experiment_latency_<DATE>.csv
```

### 2.3 Analyse

```bash
python evaluation/commentary/scripts/analyse_latency.py \
  --file results/real_experiment_latency_<DATE>.csv --out-dir results/
```

Failed/timed-out requests must appear in the `latency` CSV with
`failed=true` (and stay in the file) rather than being left out — the
script counts them under Failures per stage rather than dropping them from
the sample, per `commentary_test_plan.md` 7.2.

## 3. Work package D — stability and fault recovery (section 8)

### 3.1 Endurance runs (3 × 30 minutes)

For each of 3 runs: start the full stack, drive/let the bot drive
continuously for 30 minutes, and during that window inject (once each,
timestamped in your notes):

- one WebSocket/Overlay disconnect+reconnect (`docs/manual-test-guide.md` 5.8's `pkill -f midware.app` / restart, or just close+reopen the Overlay window)
- one Granite/LM Studio interruption+recovery (stop/restart LM Studio's server)
- one telemetry/UDP interruption+recovery (`pkill` the TORCS process briefly, or unplug the UDP send by killing `torcs_launcher.sh`'s env)
- one TTS failure, only if TTS is enabled for this batch (stop `tts_server.py` briefly)

Track the counters listed in `commentary_test_plan.md` 8.1 by hand or via
`/api/health` + `/api/stats` polling (duration, events_detected,
commentary_requests successful_outputs, model_failures,
duplicate_user_visible_displays, unhandled_exceptions,
reconnect_recovery_time_s, cpu/mem via `top`/`ps`, outputs_total /
outputs_over_45_words using
`evaluation/commentary/scripts/word_count.py` against the transcript in
your WebSocket listener's log). Fill one row per run into
`evaluation/commentary/templates/stability_run_template.csv`'s schema, then:

```bash
python evaluation/commentary/scripts/analyse_stability.py endurance \
  --file results/real_experiment_stability_run_<DATE>.csv --out-dir results/
```

### 3.2 Fault injection (RT-01..RT-12, 5 trials each)

Table of faults and expected behaviour is in
`docs/commentary_test_plan.md` section 8.2 — reuse it verbatim as your
checklist. For each trial: note `fault_injected_at_s` (seconds since
midware start), the action taken, `service_restored_at_s` (when you undid
the fault), and `first_success_after_restore_at_s` (first `ai_done` in the
WebSocket listener after restoration — leave blank for fault kinds with no
restoration step, e.g. RT-07 invalid telemetry, RT-09 high frequency,
RT-12 commentary disabled). If a fault kind genuinely doesn't apply to
this architecture (see `commentary_test_plan.md` 8.2's explicit permission
to say so), write that in `notes` and leave `recovered`/`crashed` blank
rather than inventing a trial.

```bash
python evaluation/commentary/scripts/validate_experiment_data.py \
  --kind fault_recovery --file results/real_experiment_fault_recovery_<DATE>.csv
python evaluation/commentary/scripts/analyse_stability.py faults \
  --file results/real_experiment_fault_recovery_<DATE>.csv --out-dir results/
```

## 4. Assemble the final report

```bash
python evaluation/commentary/scripts/generate_report_tables.py \
  --test-summary evaluation/commentary/results/automated_test_summary_<TIMESTAMP>.md \
  --ground-truth results/real_experiment_ground_truth_<DATE>.csv \
  --detections results/real_experiment_detected_events_<DATE>.csv \
  --latency results/real_experiment_latency_<DATE>.csv \
  --endurance results/real_experiment_stability_run_<DATE>.csv \
  --faults results/real_experiment_fault_recovery_<DATE>.csv \
  --out results/real_experiment_report_<DATE>.md
```

Any section you didn't get to run stays "NOT RUN" in the output — do not
hand-edit the generated report to fill in numbers that weren't actually
measured.
