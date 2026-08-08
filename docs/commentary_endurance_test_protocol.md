# AI Live Commentary — Endurance Test Protocol (work package D, part 1)

> Self-contained operational script for the 3×30-minute endurance runs
> (`commentary_test_plan.md` 8.1). Read `docs/commentary_test_handoff.md`
> first if you're a fresh session — it has environment setup, prior
> findings, and file-naming conventions this document assumes. Fault
> injection (RT-01..RT-12) is a separate document,
> `docs/commentary_fault_injection_protocol.md`, and is already complete
> (60/60 trials) as of 2026-08-08 — do not re-run it as part of this task.

## 0. Status and context

Work package D has two parts: fault injection (RT-01..RT-12, done) and
endurance (this document, not yet run on either device as of writing). Two
devices are running this in parallel; the runs will be pooled into one CSV
and one report, the same way work package B pooled 6 separate driving
sessions and work package C pooled multiple latency runs. **Coordinate
`run_id` values with the other device before starting** (e.g. this device
takes `R01`/`R02`, the other takes `R03`) so rows don't collide when merged
— `run_id` must be unique across the *combined* file, not just your own run.

### 0.1 Known findings from fault injection — don't re-discover these

- **RT-07** (`evaluation/commentary/results/real_experiment_fault_recovery_20260808.csv`,
  rows `RT07-1`/`RT07-4`): pushing `speedX`/other numeric telemetry fields
  as the strings `"NaN"` or `"Infinity"` via the debug endpoint gets silently
  accepted, and poisons `/api/health` into returning 500 **persistently**
  (confirmed via repeated calls with no further push in between) until a
  fresh frame with valid numeric fields arrives. Not fixed yet — a real,
  disclosed limitation, not something you need to chase during endurance
  testing. Real TORCS UDP telemetry never produces string `"NaN"`/`"Infinity"`
  values, so this should not surface during normal driving; if `/api/health`
  ever gets stuck returning 500 during your run, check whether something
  (a test script, a stray API call) pushed malformed telemetry, rather than
  assuming it's a new endurance-specific bug.
- **RT-11**: `midware.app`'s startup failure message when UDP port 3101 is
  already in use is a ~35-line raw Python traceback with the actionable
  line (`RuntimeError: Unable to bind telemetry UDP ...`) buried at the
  bottom, not a clean top-level error. Relevant if you ever need to restart
  midware and a previous process is still holding the port — check
  `ps aux | grep midware.app` before assuming a startup crash is new.
- **`interrupt_mode` default changed today** (2026-08-08): the *production*
  UI default is now `"queue"` (`midware/commentary_engine.py`,
  `dashboard.html`'s "New event vs. current playback" dropdown), changed
  per the user's explicit request. **Endurance testing must still use
  `"interrupt"` explicitly** — that's what every prior work package C/D
  measurement on this project used, and mixing modes across runs makes the
  pooled counters incomparable (queue mode also changes which requests get
  a `t0_telemetry_received`/`t1_event_detected` timestamp at all — see
  work package C's own postmortem on this, section 2 below). Set it
  explicitly; do not rely on whatever the dashboard defaults to.

## 1. Before starting: three choices

### 1.1 Model backend

Endurance testing exercises the *middleware's* stability (crashes, task
pile-up, memory growth, fault recovery), not the model's — any
OpenAI-compatible backend is valid. Two real options seen on this project
so far:

- **Local LM Studio** (`http://localhost:1234/v1` or, from inside WSL,
  the Windows host IP — `http://<host-ip>:1234/v1`).
- **DeepSeek's remote API** (`https://api.deepseek.com/v1`) — confirmed
  working on the other device as of 2026-08-08.

**If you use local LM Studio *and* local Kokoro TTS together, expect
severe slowdown.** Confirmed today: with both running on the same machine,
Granite generation that normally takes <1s took 30-60s, apparently from
GPU/compute contention between the two local processes. This isn't a
midware bug, but it will make a 30-minute endurance run process far fewer
requests than expected and skew every counter. Either use a remote model
backend (no local GPU contention), or disable TTS for this batch if you
must use both locally (see 1.2).

### 1.2 TTS on or off

Your choice, but decide before starting and record it in your notes/paper
alongside the run — it affects `commentary_requests` pacing and is one of
the 4 faults you inject (1.3) only applies if TTS is on for this batch.

### 1.3 The 4 faults to inject once each, during the run

Per `commentary_test_plan.md` 8.1: one WebSocket/dashboard disconnect
+reconnect, one Granite interruption+recovery, one telemetry/UDP
interruption+recovery, and (only if TTS is enabled for this batch) one TTS
failure+recovery. Spread them out — don't cluster them near the start or
end. A reasonable schedule for a 30-minute (1800s) run:

| Offset | Fault |
|---|---|
| ~5 min (300s) | WebSocket disconnect + reconnect |
| ~12 min (720s) | Granite unavailable + restored |
| ~20 min (1200s) | Telemetry/UDP interruption + restored |
| ~26 min (1560s) | TTS failure + restored (only if TTS enabled this batch) |

## 2. Why a dedicated monitor script, not manual counting

Work package C found a real methodological trap today: two people counting
"requests" or "events" by hand, or relying on different implicit
assumptions (e.g. which `interrupt_mode` is active), produced
non-comparable numbers. To avoid the same problem here,
`evaluation/commentary/scripts/monitor_endurance_run.py` (new, written
2026-08-08) is the single source of truth for every counter in
`STABILITY_RUN_SCHEMA` except `reconnect_recovery_time_s`. **Both devices
must run this exact script**, not hand-rolled equivalents — read its
docstring for the precise definition of each counter (e.g.
`successful_outputs` = `ai_done` with `duplicate != true` and non-empty
`content`; `model_failures` = `error` broadcasts, not scheduler-level
retries). It samples the midware process's CPU/RSS from `/proc` every 5s,
listens on the real WebSocket for the whole run, and writes one CSV row at
the end.

It does **not** measure `reconnect_recovery_time_s` — that fault is
operator-injected mid-run (1.3), so you time it by hand exactly as in
`commentary_fault_injection_protocol.md`'s RT-03/RT-04, then fill that one
field into the row the script already wrote (see step 3.4).

## 3. Procedure (repeat once per 30-minute run)

### 3.1 Start the stack

```bash
cd ~/F1-simulator
source .venv/bin/activate   # or use .venv/bin/python directly throughout

# midware — fresh process, latency logging not needed for this work package
setsid nohup .venv/bin/python -m midware.app > /tmp/midware.log 2>&1 < /dev/null &
disown
sleep 3
curl -s -o /dev/null -w "health: %{http_code}\n" http://127.0.0.1:8880/api/health

# re-POST config (a restart wipes it) -- fill in your chosen backend from 1.1
curl -s -X POST 127.0.0.1:8880/api/config/api -H 'Content-Type: application/json' \
  -d '{"base_url":"<YOUR_BACKEND_URL>","model":"<YOUR_MODEL_ID>"}'
curl -s -X POST 127.0.0.1:8880/api/features/enabled -H 'Content-Type: application/json' \
  -d '{"enabled":["commentary","engineer","coach","bot"]}'
# TTS -- only if you decided "on" in 1.2
curl -s -X POST 127.0.0.1:8880/api/config/tts -H 'Content-Type: application/json' \
  -d '{"enabled":true,"provider":"kokoro","url":"http://127.0.0.1:8881/tts","voice":"af_heart"}'

# interrupt_mode -- MUST be explicit, do not rely on the dashboard default (0.1)
curl -s -X POST 127.0.0.1:8880/api/commentary/config -H 'Content-Type: application/json' \
  -d '{"interrupt_mode":"interrupt"}'
curl -s -X POST 127.0.0.1:8880/api/commentary/clear   # clean history

# verify before driving
curl -s http://127.0.0.1:8880/api/commentary/config
```

Start TORCS, Quick Race, Player + a few `tita`, **Player's skill level set
to "Pro"** in Configure Players (otherwise damage/contact never registers).
Confirm telemetry is flowing: `curl -s http://127.0.0.1:8880/api/telemetry`
should be non-null after the first corner.

### 3.2 Start the monitor

In a separate terminal, once you've confirmed telemetry is flowing:

```bash
cd ~/F1-simulator
PID=$(pgrep -f "midware\.app")
echo "midware pid: $PID"

.venv/bin/python evaluation/commentary/scripts/monitor_endurance_run.py \
  --pid "$PID" --duration 1800 --run-id R01 \
  --log /tmp/midware.log \
  --out evaluation/commentary/results/real_experiment_stability_run_<DATE>.csv
```

It prints a live one-line status (events/requests/ok/fail/dup counts) every
2 seconds and blocks for the full 1800s. Let the user drive continuously
for the whole window.

### 3.3 Inject the 4 faults on schedule (1.3)

Reuse the exact procedures from `docs/commentary_fault_injection_protocol.md`
RT-01/RT-02 (Granite), RT-03/RT-04 (WebSocket), RT-05/RT-06 (telemetry),
RT-10 (TTS) — same commands, just executed once each at the scheduled
offsets instead of 5 times back-to-back. **Note the wall-clock time you
inject the WebSocket disconnect and the time you reconnect** (or, if you
used a WS listener script as the "client" per that document, when it
reconnects and receives a fresh `ai_start`) — that's the only number this
whole procedure asks you to time by hand.

### 3.4 After 1800s: finish the row

The monitor script prints a JSON summary and appends the row (with
`reconnect_recovery_time_s` blank) to your `--out` CSV. Compute
`reconnect_recovery_time_s` = (time of first fresh `ai_start` after
reconnecting) − (time you reconnected), then edit that one field into the
CSV row you just got — it's the only column not filled in automatically.

```bash
.venv/bin/python evaluation/commentary/scripts/validate_experiment_data.py \
  --kind stability_run --file evaluation/commentary/results/real_experiment_stability_run_<DATE>.csv
```

Fix the file if validation fails (most likely cause: forgetting to fill in
`reconnect_recovery_time_s`, or a stray blank line).

### 3.5 Repeat

Run it again for your other assigned `run_id`(s) — fresh `midware.app`
process each time (step 3.1 again), same CSV `--out` path (the script
appends).

## 4. After all runs (both devices)

Once both devices' rows are merged into one
`real_experiment_stability_run_<DATE>.csv` (3 rows total, `run_id`s
`R01`/`R02`/`R03` or whatever you coordinated):

```bash
.venv/bin/python evaluation/commentary/scripts/analyse_stability.py endurance \
  --file evaluation/commentary/results/real_experiment_stability_run_<DATE>.csv \
  --out-dir evaluation/commentary/results/
```

This prints/writes the endurance summary table (success rate, 45-word
violation rate, per run and aggregated) used in the paper. Keep whatever it
produces even if a run shows a low success rate or a nonzero
`unhandled_exceptions` count — per the test contract, results are not to be
adjusted after seeing them; a real finding is more valuable than a clean
number.

## 5. File naming (see `docs/commentary_test_handoff.md` for the general
rule)

- `evaluation/commentary/results/real_experiment_stability_run_<DATE>.csv`
  — the merged 3-row result. `<DATE>` = the date the *last* of the 3 runs
  was collected, `YYYYMMDD`.
- Never `sample`/`SAMPLE` in the filename — that prefix is reserved for the
  fake demo fixtures in `evaluation/commentary/sample_data/`.
