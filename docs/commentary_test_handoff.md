# AI Live Commentary Testing — Session Handoff (Work Package C, latency)

> Read this whole document before doing anything. It exists so a fresh
> Claude session on a different machine can pick up exactly where the
> previous session left off, without re-deriving context or repeating
> already-fixed bugs. If anything here conflicts with what you observe in
> the actual code/repo, **trust the code** and update this document.

## 0. What this project is

`F1-simulator` is a TORCS-based racing simulator with an AI middleware
(`midware/`) that adds several features, one of which is **AI Live
Commentary**: a background loop watches UDP telemetry from a human-driven
TORCS session, detects race events (contact, off-track, position change,
lap complete, battle, pace surge), and turns them into natural-language
commentary via a local Granite LLM (served through LM Studio), streamed to
a browser dashboard (`midware/static/dashboard.html`) over WebSocket, with
optional TTS playback.

We are executing a formal test plan for this feature so its evaluation
section of the team's paper is backed by real data, not assumptions. The
authoritative task specification is:

```
docs/AI Live Commentary test contract_8_5.md
```

Read that file's 测试一~测试四 (Test One through Four) sections — they
define exactly what each work package must measure and the acceptance
targets. This handoff document summarises status and gives you the
concrete next steps; the contract document is the source of truth for
*what* "done" means.

## 1. Testing framework: four work packages

| # | Name | Status | Evidence |
|---|---|---|---|
| A | Functional correctness (automated pytest) | **Done** | `tests/unit/test_commentary_*.py`, `tests/integration/test_commentary_runtime.py`; run via `.venv/bin/python tools/commentary_test_report.py`. 74/74 passing, 100%. |
| B | Real race event detection accuracy | **Done** (with a documented methodology caveat) | `evaluation/commentary/results/real_experiment_ground_truth_20260806.csv` + `real_experiment_detected_events_20260806.csv`, scored via `evaluation/commentary/scripts/match_events.py`. See §3 below — do not re-run this unless the user explicitly asks for more sessions. |
| C | End-to-end latency | **In progress — this is your task** | See §4. |
| D | Stability and fault recovery | **Not started** | Comes after C. Scripts already exist (`evaluation/commentary/scripts/analyse_stability.py`) but no real data collected yet. |

Full traceability of every requirement to real code is in
`docs/commentary_test_matrix.md` — read it if you need to understand *why*
a piece of code behaves a certain way.

## 2. Bugs found and fixed during this testing effort

All of these are already committed to the repo (check `git log`). Do not
re-discover or re-fix them; if you suspect one has regressed, write a
failing test first per the contract document's rule 8.

1. **Text dedup ran after broadcast, not before.** `commentary_engine.py`'s
   `should_emit_text()` was checked *after* `ai_done` had already been sent
   to clients — duplicate commentary text could be shown twice. Fixed in
   `midware/runtime.py::generate_commentary` (dedup now gates the
   broadcast) plus all 4 front-end consumers (`overlay-app/src/renderer.js`
   — since removed from the repo, see below — and
   `midware/static/{index,index2,dashboard}.html`).
2. **`pace_surge` compared telemetry frames ~0.06s apart, not the ~0.5s the
   22 km/h threshold assumed**, making it nearly untriggerable by real
   driving. Then, after a naive fix, a single continuous acceleration
   produced 14 separate `pace_surge` events instead of one. Final fix:
   `pace_surge` is now tracked as a continuous burst
   (`CommentaryEngine.pace_surge_active/_start_speed/_peak_speed` in
   `midware/commentary_engine.py`) and reports exactly once when the burst
   ends (throttle drops or speed stops climbing). Also added a guard: both
   endpoints of a tick must be non-negative speed, because a car flung
   backward by a hard collision could swing from e.g. -102 to -46 km/h — a
   real numeric increase but not an intentional acceleration.
3. **`overlay-app`'s Electron overlay was removed from the project
   entirely** (commit "delete overlay part of commentary function") in
   favour of the browser dashboard (`midware/static/dashboard.html`) as
   the sole commentary UI. If you see references to "Overlay" in older
   docs, mentally substitute "browser dashboard".

**Known, un-fixed limitation (documented, not a bug to fix unless asked):**
`detect_event()` in `midware/commentary_engine.py` evaluates every
detection cycle's candidates by priority and returns only the single
highest-priority match. `contact`/`position_change`/`off_track` are all
priority 5, but the check order is position_change → contact → off_track,
so `off_track` silently loses same-cycle ties to the other two. This was
confirmed with real data (work package B): a session with 19 contact
events recorded exactly 0 off-track events despite the operator recalling
several genuinely happened. This is a legitimate design choice for
deciding what the commentator *says* (narrating two things at once isn't
meaningful) but means the detection log undercounts co-occurring
lower-priority events. Do not "fix" this without the user explicitly
asking — it's currently written up as a paper finding, not a defect.

## 3. Work package B summary (context only — do not redo)

6 real driving sessions (2 tracks) were collected, human-driven, actively
provoking each event type. Ground truth was built by having the operator
**review the system's own detection log and confirm each entry** (not
independently annotate from a recording), except for 6 `off_track` events
in one session that the operator independently recalled were missed — those
were added to ground truth *without* a matching detection.

Result (`evaluation/commentary/results/real_experiment_ground_truth_20260806.csv`
vs `..._detected_events_20260806.csv`, scored at `--tolerance 1.0`):

| Event | GT | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Battle | 18 | 18 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| Contact | 52 | 52 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| Lap complete | 3 | 3 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| Off-track | 14 | 8 | 0 | 6 | 1.000 | 0.571 | 0.727 |
| Pace surge | 33 | 33 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| Position change | 18 | 18 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| **Overall (micro)** | 138 | 132 | 0 | 6 | 1.000 | 0.957 | 0.978 |

**Critical caveat for the paper**: because ground truth for 5 of 6 event
types was derived from the detection log itself, those precision/recall
numbers are not unbiased accuracy measures — they mainly prove a low
false-positive rate. Off-track's 0.571 recall is the only methodologically
unbiased number in the table (independently recalled misses), and it
falls below the contract document's 0.70 per-event target — attributed to
the priority tie-break limitation above.

## 4. Your task: Work package C — end-to-end latency

Read `docs/AI Live Commentary test contract_8_5.md` section "测试三：端到端延迟"
(section 5) for the full spec. Summary:

**Timestamps**: t0 (telemetry received) → t1 (event_detected) → t2 (first
Granite token) → t3 (complete ai_done) → t4 (caption displayed) → t5 (TTS
playback started, only if TTS enabled).

**Metrics**: detection latency (t1-t0), first-token latency (t2-t1),
generation latency (t3-t1), caption latency (t4-t0), audio latency (t5-t0).
For each: count, min, mean, median, stdev, P95, max, failure count.

**Method**: 30 independent real event triggers, final demo config frozen,
failures/timeouts recorded not discarded.

**Targets** (discussion only, not a gate to pass/fail and fabricate around):
Detection median ≤0.5s, Detection P95 ≤1.0s, First-token median ≤2.0s,
Complete-caption median ≤4.0s, failure rate <5%.

### 4.1 What's already built for you

- `midware/latency_log.py`: opt-in logger, captures t1/t2/t3 automatically
  when `COMMENTARY_LATENCY_LOG=1` is set at midware startup (cannot be
  toggled at runtime — must restart with the env var set). Writes one JSON
  line per (request_id, stage) to the path in `COMMENTARY_LATENCY_LOG_PATH`.
- `evaluation/commentary/scripts/analyse_latency.py`: consumes a CSV in
  `evaluation/commentary/schemas/csv_schemas.py::LATENCY_SCHEMA` format
  (columns: `session_id,event_id,request_id,t0_telemetry_received,
  t1_event_detected,t2_first_token,t3_ai_done,t4_caption_displayed,
  t5_tts_started,failed,failure_reason`) and prints/writes the stats table.
- t0 is **not** separately logged (avoids a write on the UDP hot path for
  an opt-in feature) — use the triggering frame's own `sim_time`, obtained
  from `/api/events/recent` right after the event fires, as t0.
- t4/t5 are **not backend-observable** — the backend has no idea when the
  browser actually painted the caption or started audio playback. Options,
  cheapest first:
  1. Approximate t4 using the WebSocket `ai_done` message's *arrival* time
     at a listener you run yourself (network delivery time is a lower
     bound on render time, but very close in practice on localhost) — this
     is what the previous session did and is an acceptable approximation
     if you disclose it as one.
  2. For a real t5 with TTS: after Kokoro is running and TTS is enabled in
     `/api/config/tts`, the `tts_audio` WebSocket message's arrival time is
     the equivalent approximation for t5.

### 4.2 Concrete operational steps

**Step 0 — environment.** Same LM Studio + TORCS setup as work package B,
but see §5 below for TTS since your machine doesn't have Kokoro yet, and
note the LM Studio LAN address will very likely be **different** on this
machine — do not assume `192.168.56.1:1234`, verify with
`curl http://<address>:1234/v1/models`.

**Step 1 — start midware with latency logging on:**

```bash
cd ~/F1-simulator   # adjust to wherever this repo actually lives on this machine
mkdir -p evaluation/commentary/results
COMMENTARY_LATENCY_LOG=1 \
COMMENTARY_LATENCY_LOG_PATH=evaluation/commentary/results/real_experiment_latency_raw_<DATE>.jsonl \
  nohup .venv/bin/python -m midware.app > /tmp/midware.log 2>&1 &
disown
```

(If `.venv` doesn't exist at repo root on this machine: `python3 -m venv
.venv && .venv/bin/python -m pip install -r requirements-core.txt`, same as
was done for the first machine.)

**Step 2 — point it at LM Studio and enable features:**

```bash
curl -s -X POST 127.0.0.1:8880/api/config/api -H 'Content-Type: application/json' \
  -d '{"base_url":"http://<LM_STUDIO_ADDRESS>:1234/v1","model":"<MODEL_ID_FROM_/v1/models>"}'
curl -s -X POST 127.0.0.1:8880/api/features/enabled -H 'Content-Type: application/json' \
  -d '{"enabled":["commentary","engineer","coach","bot"]}'
```

**Step 3 — TORCS.** `bash torcs_launcher.sh`, Quick Race, Select Drivers
(Player + a handful of `tita`/other bots — see `docs/commentary_test_matrix.md`
or just ask the user; **check Player's skill level is "Pro" in Configure
Players**, otherwise damage/contact won't register — this was a real
issue on the first machine, see §2 fix history above), select a track,
start.

**Step 4 — drive and trigger ~30 real events.** No need to space them out
manually — normal driving with deliberate event-provoking (as in work
package B) naturally produces well-spaced triggers because of the event
cooldowns already in the code. Mix event types; don't just spam one kind.

**Step 5 — after driving, build the latency CSV.** Read
`COMMENTARY_LATENCY_LOG_PATH`'s JSONL (one row per
`{request_id, stage, timestamp}` — `timestamp` is `time.monotonic()`, only
comparable to other rows in the *same file/process run*, never to wall
clock or across restarts). For each `request_id`, pull t1/t2/t3 from the
JSONL, t0 from the corresponding `/api/events/recent` entry's `sim_time`
captured at the time (or approximate t0≈t1 if you didn't capture it live
and say so), and t4 (+t5 if TTS on) from your WebSocket listener's arrival
timestamps. Validate before analysing:

```bash
.venv/bin/python evaluation/commentary/scripts/validate_experiment_data.py \
  --kind latency --file evaluation/commentary/results/real_experiment_latency_<DATE>.csv
.venv/bin/python evaluation/commentary/scripts/analyse_latency.py \
  --file evaluation/commentary/results/real_experiment_latency_<DATE>.csv \
  --out-dir evaluation/commentary/results/
```

Failed/timed-out requests must stay in the CSV with `failed=true`, not be
dropped — the script counts them under Failures per stage rather than
silently excluding them from the sample.

**Step 6 — generate the LaTeX table** the same way the previous session
did for work package B (ask the user if they want it, they've been
pasting these into the paper directly — match the `booktabs` style already
used in the paper for Table 4.1/4.2).

## 5. Setting up Kokoro TTS on this machine (not installed yet)

Full reference: `docs/tts-setup.md`. Condensed here:

```bash
# 1. Activate whichever venv this machine's midware setup uses
#    (docs/tts-setup.md assumes midware/.venv; the primary test machine
#    instead used a repo-root .venv/ -- use whichever actually exists here,
#    check with `ls .venv midware/.venv 2>/dev/null`)
source .venv/bin/activate   # or: source midware/.venv/bin/activate

# 2. Install TTS-specific dependencies (on top of requirements-core.txt)
pip install -r requirements-tts.txt
pip install kokoro soundfile huggingface_hub   # if requirements-tts.txt doesn't already pin these

# 3. Download model weights + voices (~350MB, needs internet) -- run from repo root
python -c "
from huggingface_hub import hf_hub_download
hf_hub_download('hexgrad/Kokoro-82M', 'kokoro-v1_0.pth', local_dir='.')
hf_hub_download('hexgrad/Kokoro-82M', 'voices/af_heart.pt', local_dir='.')
hf_hub_download('hexgrad/Kokoro-82M', 'voices/bm_lewis.pt', local_dir='.')
"

# 4. Start the TTS server (separate process/terminal from midware)
python tts_server.py
# Expect: "[INFO] Kokoro model loaded." then "TTS server ready -> http://localhost:8881"

# 5. Sanity check
curl http://localhost:8881/health
# {"ok": true, "model_loaded": true}
```

Once it's running, enable TTS in midware so `t5_tts_started` becomes
measurable:

```bash
curl -s -X POST 127.0.0.1:8880/api/config/tts -H 'Content-Type: application/json' \
  -d '{"enabled": true, "provider": "kokoro", "url": "http://127.0.0.1:8881/tts", "voice": "bm_lewis"}'
```

If Kokoro setup fails or eats too much of the session (it's not the
primary goal), it's fine to run work package C with TTS off and just skip
t5/audio latency for this round — say so explicitly in the results rather
than fabricating a number, per the contract document's rule 2.

## 6. File and naming conventions (don't break these)

- Anything derived from a real driving session goes in
  `evaluation/commentary/results/` with a filename containing
  `real_experiment` and the date, e.g.
  `real_experiment_latency_20260807.csv` — **never** `sample` in the name
  for real data, and never overwrite the work-package-B files already
  there.
- `evaluation/commentary/sample_data/SAMPLE_*.csv` are hand-built demo
  files proving the scripts work — do not confuse these with real data or
  edit them to "help."
- Every script in `evaluation/commentary/scripts/` has its own passing
  unit tests in `evaluation/commentary/tests/` — run
  `.venv/bin/python -m pytest evaluation/commentary/tests -q` if you touch
  any of them, and `tests/unit/test_commentary_*.py` +
  `tests/integration/test_commentary_runtime.py` if you touch
  `midware/commentary_engine.py` or `midware/runtime.py`.

## 7. After work package C: work package D

Stability (3×30min endurance) and fault injection (RT-01..RT-12, 5 trials
each) — see contract document section 6, and
`docs/commentary_experiment_protocol.md` section 3 for the operational
procedure. Not started yet; do not begin until the user asks, since it's
the most time-consuming remaining piece.

## 8. Miscellaneous gotchas learned the hard way

- The `/api/events/recent` buffer only keeps the **last 25 events** — for
  sessions longer than a couple of minutes, poll it periodically during
  the session, not just at the end, or early events get silently evicted.
- `pkill -f 'midware.app'` in this sandboxed environment sometimes returns
  an odd exit code (144) that truncates command output — verify with
  `ps aux | grep midware.app` afterward rather than trusting the exit
  code; the kill itself still works.
- Restarting midware resets all in-memory config (model endpoint, enabled
  features, TTS settings) — always re-POST `/api/config/api` and
  `/api/features/enabled` after every restart.
- TORCS AI opponents use genuinely different car classes (`car1-ow1` is
  open-wheel, ~600kg; `carN-trb1`/`p406` are ~1150kg road cars) — mixing
  them makes the field wildly uneven. The human `Player` defaults to
  `car7-trb1`. Not relevant to latency testing directly, but relevant if
  the user complains driving feels unbalanced again.
