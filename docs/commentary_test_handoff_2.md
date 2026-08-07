# AI Live Commentary Testing — Handoff 2 (finish work package C, then D)

> **This document supersedes `docs/commentary_test_handoff.md` for anything
> concerning work package C.** That earlier document is still correct about
> the project, work packages A and B, and the general rules — read it first
> for background — but its section 4 describes an environment and a plan that
> have both moved on. Where the two conflict, this one wins; where either
> conflicts with the code, **the code wins** and you should update the doc.
>
> Written 2026-08-07 at the end of a session that built the measurement
> tooling, collected two runs, and found a real defect. Everything below is
> either verified against the running system or explicitly flagged as
> unverified.

---

## 0. Read this first: the work is NOT committed

Every code change and every result described here was left **uncommitted** on
branch `dev/aibot` (last commit `27a7f70`). If you are a fresh session on a
different machine and `git log` does not show a commit containing
`evaluation/commentary/scripts/build_latency_csv.py`, then the previous
session's work never reached this machine and **nothing in sections 2–4 exists
for you**. Stop and ask the user to push from the original machine.

Files involved:

```
 M .gitignore                                          (un-ignored the eval CSVs)
 M docs/tts-setup.md                                   (Python 3.12 + GPU setup)
 M midware/latency_log.py                              (explicit-timestamp support)
 M midware/runtime.py                                  (t0/t0b logging)
 M torcs_launcher.sh                                   (renderer overridable)
?? evaluation/commentary/scripts/build_latency_csv.py  (new)
?? evaluation/commentary/scripts/capture_display_latency.py (new)
?? evaluation/commentary/templates/                    (new, 5 files)
?? evaluation/commentary/results/real_experiment_*_20260807_run{1,2}.*
```

**`.gitignore` trap, now fixed but worth understanding**: line 48 was a blanket
`*.csv` (aimed at TORCS `player_logs/`). It silently swallowed every evaluation
CSV — which is exactly why work package B's `real_experiment_*.csv` files,
quoted in the earlier handoff's section 3 table, **do not exist in this repo
and never did**. `git log --all -- 'evaluation/commentary/results/real_experiment*'`
is empty. Those numbers currently live only on the machine that produced them.
If the paper cites them, get them from that machine and commit them.

---

## 1. Where work package C actually stands

| | Status |
|---|---|
| Measurement tooling (t0–t5) | **Done and verified end-to-end** |
| Run 1 data | **Usable**: 27 complete samples, 0 failures, all targets met |
| Run 2 data | **Unusable**: 21/31 failed on a real defect (section 3) |
| Sample size | 27 complete, contract asks for 30 — short by 3 |
| Per-event-type breakdown | Not possible for run 1 (a capture bug, since fixed) |
| LaTeX table | Not generated |
| The defect found | Diagnosed, **not fixed** — this is your first task |

### Run 1 results (`real_experiment_latency_20260807_run1.csv`)

t0 = "oldest unseen frame" reading (see section 5.1):

| Stage | N | Median | P95 | Maximum | Failures |
|---|---:|---:|---:|---:|---:|
| Event detection | 27 | 0.475 | 0.508 | 0.510 | 0 |
| First model token | 27 | 0.132 | 0.254 | 0.304 | 0 |
| Complete model response | 27 | 1.633 | 2.175 | 2.213 | 0 |
| Caption displayed | 27 | 2.134 | 2.682 | 2.743 | 0 |
| TTS playback | 27 | 2.134 | 2.682 | 2.743 | 0 |

Against the contract's targets: detection median ≤0.5 ✅, detection P95 ≤1.0 ✅,
first-token median ≤2.0 ✅ (0.132, an order of magnitude under), complete
caption median ≤4.0 ✅, failure rate <5% ✅ (0%). Run 1 had 37 detections: 27
completed, 10 preempted by design, 0 errors.

Do not merge run 1 and run 2 into one dataset. Run 2's failures inflate its
own P95 (complete response 3.882 vs 1.633) because failing requests still
occupy the model broker.

---

## 2. The environment on this machine (different from handoff 1)

Handoff 1 assumes LM Studio on the LAN. **That is not how this machine runs.**

### 2.1 Granite is remote, over an SSH tunnel

```bash
ssh -N -o ServerAliveInterval=30 -L 1234:bp1-gpu001:20648 sg25291@bp1-login.acrc.bris.ac.uk
```

Bristol ACRC (BluePebble), **vLLM** serving `ibm-granite`
(`/user/work/sg25291/granite-4.1-8b`), **`max_model_len: 4096`** — that limit is
the whole of section 3, remember it. Run the tunnel **inside WSL**, not on
Windows: midware runs in WSL and cannot reach a Windows-side `127.0.0.1`.

Measured: login-node RTT 16 ms, tunnel HTTP overhead ~35 ms, streaming first
byte 32–43 ms, 60-token completion 1.0–1.26 s. The network is not the
bottleneck.

**Before any collection run, check the SLURM job's remaining walltime** —
`ssh sg25291@bp1-login.acrc.bris.ac.uk 'squeue -u sg25291'`. It needs
interactive auth so an agent cannot do it; ask the user. If the job expires
mid-run every remaining request fails and the run is wasted.

There is also an LM Studio process on the Windows host. It is a **red herring**
— its server was not running and it holds no model. Ignore it, and make sure
midware points at the tunnel (`http://localhost:1234/v1`).

### 2.2 TTS runs on the GPU in its own venv

The machine has a **GTX 1650 (4 GB)** with working WSL passthrough. Full setup
and rationale is in `docs/tts-setup.md`, which was rewritten this session.
Three things that will waste your time if you do not know them:

- `kokoro` requires **Python < 3.13**; the repo venv is 3.14. TTS therefore
  lives in a **separate `.venv-tts`** (Python 3.12, created with `uv`, no root
  needed). Repo venv `.venv` is unchanged.
- The NVIDIA **driver is 517.00 (2022), capped at CUDA 11.7**. Current torch
  wheels need 525+. Pinned to `torch==2.7.1+cu118`, the newest cu118 build for
  cp312. GPU synthesis 0.21–0.27 s vs 2.07 s on CPU.
- `spacy.load("en_core_web_sm")` is needed by kokoro's G2P but is **nobody's
  declared dependency**, and its self-install prints success while installing
  into the wrong environment. Install the wheel explicitly.

Start it detached with `setsid` (plain `nohup &` dies when the WSL session
ends):

```bash
cd ~/summer-project/F1-simulator
setsid nohup .venv-tts/bin/python tts_server.py > /tmp/tts_server.log 2>&1 < /dev/null &
```

**First synthesis after startup costs ~8.9 s** (CUDA warmup), then ~0.22 s.
Always fire one throwaway `POST /tts` before collecting, or the first t5 is a
9 s outlier you are not allowed to quietly drop.

### 2.3 TORCS runs from a different directory

The user's configured game is **not** the repo's BUILD:

```bash
cd /home/jay/projects/for_summer_project/BUILD && export DISPLAY=:0 && \
  export LIBGL_ALWAYS_SOFTWARE=1 && export GALLIUM_DRIVER=llvmpipe && ./bin/torcs
```

This is fine and needs no change: `src/drivers/human/player_logger.cpp` sends
UDP to `127.0.0.1:3101` by default, and midware (running from the repo) listens
on `0.0.0.0:3101`. The emitted CSV header matches `MAIN_CSV_FIELDS` exactly —
verified.

`for_summer_project/midware/` is a **stale partial copy** (no `runtime.py`, no
`app.py`, no `latency_log.py`). The repo copy is the live one. Nothing to sync.
The "keep both copies in sync" memory note applies to `ai_bot.py`, not midware.

Hardware rendering is available but untested: `d3d12_dri.so` is installed and
`/dev/dxg` exists, so `GALLIUM_DRIVER=d3d12 LIBGL_ALWAYS_SOFTWARE=0` should
hand OpenGL to the GPU and free the CPU cores llvmpipe eats. `torcs_launcher.sh`
now honours pre-set values. Worth trying, but **whatever you pick must stay
fixed for the whole collection run** — CPU load affects the detection-loop
timing you are measuring.

### 2.4 WSL gotchas that cost time this session

- `pkill -f midware.app` **kills the shell running it**, because the pattern
  matches your own command line. Use `pkill -f "[m]idware.app"`, and never in
  the same command as one that starts it.
- Background processes need `setsid`; `nohup … &` alone dies with the session.
- `time.monotonic()` on Linux is CLOCK_MONOTONIC and is **shared system-wide** —
  verified equal to `/proc/uptime` across processes and interpreters. That is
  what makes the two log files joinable with no conversion. It breaks only
  across a reboot / `wsl.exe --shutdown`.
- Restarting midware resets all in-memory config. Always re-POST
  `/api/config/api`, `/api/config/tts`, `/api/features/enabled`.

---

## 3. YOUR FIRST TASK: the context-length defect

This is a genuine bug, found by run 2, and **work package D's 3×30-minute
endurance runs will hit it every time** until it is fixed.

### Symptom

After roughly 15–20 minutes of continuous commentary in one midware process,
**every** request starts failing:

```
API 400: This model's maximum context length is 4096 tokens. However, you
requested 512 output tokens and your prompt contains at least 3585 input
tokens, for a total of at least 4097 tokens.
```

Run 2: 44 detections → 10 completed, 13 preempted, **21 hard failures**. Run 1,
on a freshly started midware, had zero.

### Cause

`ContextManager.build_messages()` at `midware/context_manager.py:219`:

```python
budget = self.config.max_context_tokens - self.config.max_response_tokens
```

with defaults `max_context_tokens = 4096` and `max_response_tokens = 512`. So
the prompt is trimmed to 3584 tokens and 512 output tokens are requested —
**exactly 4096, the model's entire context, with zero headroom.**

The trimming itself works. The problem is that it is driven by
`estimate_tokens()` (`context_manager.py:19`), a heuristic — 1 token ≈ 4 ASCII
chars / 2 CJK chars — with no real tokenizer. Any underestimate overfills. The
observed miss was **one token** (3585 actual vs ≤3584 estimated).

Three conditions must coincide, which is why it looks intermittent:
a long-lived session (history large enough to actually reach the budget), a
budget that exactly equals the model limit, and an approximate estimator.

Note `max_context_tokens` defaulting to 4096 also silently assumes the served
model's context is 4096. It happens to match `granite-4.1-8b` here. It is a
coupling worth making explicit.

### How to fix it

Handoff 1's rule 8 (from the contract document) applies: **write the failing
test first.**

1. Add a test under `tests/unit/` that fills a `ContextManager` with enough
   history to saturate the budget, then asserts `build_messages()` leaves a
   real margin — e.g. that estimated prompt tokens + `max_response_tokens` is
   comfortably under `max_context_tokens`, not equal to it. Confirm it fails
   on current code.
2. Fix by giving the budget explicit headroom. The smallest honest change is a
   `safety_margin_tokens` field on `ContextConfig` (something like 10% of
   `max_context_tokens`, minimum ~128) subtracted in `build_messages()`. Do
   **not** "fix" it by making `estimate_tokens` cleverer — the estimator will
   always be approximate; the budget is what must be conservative.
3. Consider whether `max_context_tokens` should be discovered from the served
   model (`/v1/models` reports `max_model_len`) rather than hardcoded. Discuss
   with the user before doing this — it is a behaviour change beyond the fix.
4. Run the full suites:
   ```bash
   .venv/bin/python -m pytest tests/unit/test_commentary_*.py \
     tests/integration/test_commentary_runtime.py evaluation/commentary/tests -q
   ```
   Baseline at handoff time: **140 passed, 0 failed**. Stop midware first —
   the integration tests bind UDP 3101 and error out if it is in use.

### Does fixing it invalidate run 1?

No, and say so explicitly in the paper rather than hiding it. Run 1's 27
requests never came near the budget ceiling (fresh process, short history), so
they did not exercise the code path the fix changes. Run 1 is a valid
"pre-fix" dataset. The plan is to make **run 3, post-fix, the headline result**
and keep run 1 as corroboration.

---

## 4. YOUR SECOND TASK: collect run 3

Target **45+ detections** so that after preemptions you still have **30+
complete samples**. Run 1 produced 37 detections in 148 s of driving, so this
is roughly 4–5 minutes at the wheel. The user drives; you cannot.

### Procedure

```bash
cd ~/summer-project/F1-simulator

# 0. tunnel up? model reachable? ask the user to check SLURM walltime first.
curl -s http://127.0.0.1:1234/v1/models

# 1. TTS server (see 2.2) — start if not already running
curl -s http://127.0.0.1:8881/health      # {"ok":true,"model_loaded":true}

# 2. midware, latency logging on, FRESH process (this also clears the history
#    that triggered the section-3 defect)
COMMENTARY_LATENCY_LOG=1 \
COMMENTARY_LATENCY_LOG_PATH=evaluation/commentary/results/real_experiment_latency_raw_<DATE>_run3.jsonl \
  setsid nohup .venv/bin/python -m midware.app > /tmp/midware.log 2>&1 < /dev/null &

# 3. re-POST config (a restart wipes it)
curl -s -X POST 127.0.0.1:8880/api/config/api -H 'Content-Type: application/json' \
  -d '{"base_url":"http://localhost:1234/v1","model":"ibm-granite"}'
curl -s -X POST 127.0.0.1:8880/api/config/tts -H 'Content-Type: application/json' \
  -d '{"enabled":true,"provider":"kokoro","url":"http://127.0.0.1:8881/tts","voice":"bm_lewis"}'
curl -s -X POST 127.0.0.1:8880/api/features/enabled -H 'Content-Type: application/json' \
  -d '{"enabled":["commentary","engineer","coach","bot"]}'
# verify: GET /api/config and /api/commentary/config
# expect mode=hybrid interrupt_mode=interrupt stream=True tts.enabled=True

# 4. t4/t5 capture client
setsid nohup .venv/bin/python evaluation/commentary/scripts/capture_display_latency.py \
  --out evaluation/commentary/results/real_experiment_display_raw_<DATE>_run3.jsonl \
  --session-id S3 > /tmp/capture.log 2>&1 < /dev/null &

# 5. warm up BOTH (neither needs TORCS running)
curl -s -X POST http://127.0.0.1:8881/tts -H 'Content-Type: application/json' \
  -d '{"text":"warmup","voice":"bm_lewis"}' -o /dev/null
curl -s -X POST http://127.0.0.1:1234/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"ibm-granite","max_tokens":8,"messages":[{"role":"user","content":"warmup"}]}' -o /dev/null
```

Then the user starts TORCS (section 2.3), Quick Race, Player + a few `tita`,
and — this matters — **Player's skill level must be "Pro" in Configure
Players**, or damage never registers and `contact` cannot fire.

Ask them to cover all six event types (contact, off_track, position_change,
battle, pace_surge, lap_complete); run 1 was heavy on contact and
position_change with only one lap_complete. Run 3's capture logs event types,
so a per-type latency table becomes possible — that is one of the reasons run 3
exists.

**Check telemetry is arriving after the first corner** (`GET /api/telemetry`
should be non-null) rather than discovering at the end that nothing was
recorded.

### Building the results

```bash
.venv/bin/python evaluation/commentary/scripts/build_latency_csv.py \
  --backend evaluation/commentary/results/real_experiment_latency_raw_<DATE>_run3.jsonl \
  --capture evaluation/commentary/results/real_experiment_display_raw_<DATE>_run3.jsonl \
  --out     evaluation/commentary/results/real_experiment_latency_<DATE>_run3.csv \
  --session-id S3
.venv/bin/python evaluation/commentary/scripts/validate_experiment_data.py \
  --kind latency --file evaluation/commentary/results/real_experiment_latency_<DATE>_run3.csv
.venv/bin/python evaluation/commentary/scripts/analyse_latency.py \
  --file evaluation/commentary/results/real_experiment_latency_<DATE>_run3.csv \
  --out-dir evaluation/commentary/results/
```

Sanity-check the preemption count against the log — they matched exactly (10
and 10) in run 1, which is what validates the inference:

```bash
grep -c "解说被新事件中断" /tmp/midware.log
```

---

## 5. Measurement subtleties you MUST disclose in the paper

These are not bugs. They are properties of the design that make the numbers
mean something narrower than their labels suggest. Each was verified.

### 5.1 Detection latency has two defensible readings, 10× apart

The detection loop polls: `await asyncio.sleep(0.5)` (`runtime.py`, in
`_auto_commentary_loop`), and `CommentaryEngine` evaluates state deltas against
`frames[-1]` only. So an event that physically happens between two polls is
first seen at the later one. `_record_detection` logs both anchors:

| Stage in JSONL | Reading | Run 1 median |
|---|---|---:|
| `t0_telemetry_received` | oldest frame the cycle had not seen — includes the polling wait | **0.475 s** |
| `t0b_newest_frame` | the frame the engine actually evaluated — processing only | **0.041 s** |

`build_latency_csv.py --t0 oldest|newest` selects which becomes the CSV's t0;
`oldest` is the default. The true value is between them and is not observable
without evaluating detectors per frame (a behaviour change — do not do it
mid-experiment).

Recommended framing: report `oldest`, and state that detection latency is
dominated by the 0.5 s polling interval while the middleware's own processing
is ~41 ms. That is honest and points at the real lever. Reporting `newest`
alone would understate user-perceived delay by an order of magnitude.

### 5.2 TTS synthesis is inside t3, not after it

`runtime.py`'s `generate_commentary` awaits `call_tts(reply)` **before**
recording `t3_ai_done` and broadcasting. The code comment says why: caption and
audio are deliberately released together so there is never text without sound.

Consequences: with TTS enabled, "generation latency" (t3−t1) **includes** ~0.2 s
of Kokoro synthesis and is not pure model time; caption latency is likewise
delayed on purpose; and **t5−t4 ≈ 0** (1.5–4 ms measured) because `ai_done` and
`tts_audio` are broadcast back-to-back from the same `_commit()`. Do not
present t5−t4 as an audio-startup cost. The t3 placement is *correct* per the
contract's definition of t3 ("完整 `ai_done`"), so no code change is wanted.

### 5.3 Preemption is not failure

Interrupt mode cancels an in-flight commentary when a new event of equal or
higher priority arrives, so the request never reaches t3. Run 1: 10 of 37.
Counting those as failures reported a **27% failure rate on a run whose true
failure count was zero**.

`build_latency_csv.py` classifies them: no t3 + a later detection exists →
preempted, written to `*_preempted.csv`, excluded from the failure count. The
last incomplete request has no successor and stays a genuine failure. The
inference was cross-validated against `grep -c "解说被新事件中断"`.

Genuine failures (`error` broadcasts) and dedup-suppressed requests are handled
separately — see the script's docstring. Nothing is ever silently dropped;
everything lands either in the main CSV or a named sidecar.

### 5.4 t4/t5 are delivery times, not paint times

The capture client stamps when the WebSocket message arrived at a local
client — a lower bound on when a browser rendered the caption or started
audio. Sub-millisecond on loopback, so a good approximation, but disclose it.

---

## 6. What is left after run 3

1. **LaTeX table** for the paper, `booktabs`, matching Table 4.1/4.2's existing
   style. Ask the user — they have been pasting these in directly.
2. **Work package D**: stability (3×30 min) and fault injection (RT-01..RT-12,
   5 trials each). See the contract document section 6 and
   `docs/commentary_experiment_protocol.md` section 3. Not started.
   **Fix section 3's defect before attempting this** — a 30-minute endurance
   run is precisely the condition that triggers it, and you would spend the
   run measuring a known bug.
3. **Work package B's missing CSVs** — retrieve from the original machine and
   commit them now that `.gitignore` allows it (section 0).
4. Optional: record an explicit cancellation stage in `_run_commentary`'s
   `CancelledError` handler, so preemption becomes a logged fact rather than an
   inference. Small, and it would make section 5.3 airtight for work package D.

---

## 7. Data inventory as of this handoff

All under `evaluation/commentary/results/`:

| File | What it is |
|---|---|
| `real_experiment_latency_raw_20260807_run1.jsonl` | run 1 backend t0/t0b/t1/t2/t3 |
| `real_experiment_display_raw_20260807_run1.jsonl` | run 1 t4/t5 (event types empty — capture bug, fixed after) |
| `real_experiment_latency_20260807_run1.csv` | run 1 merged, 27 rows, validates |
| `real_experiment_latency_20260807_run1_preempted.csv` | run 1's 10 preempted requests |
| `real_experiment_latency_raw_20260807_run2.jsonl` | run 2 backend |
| `real_experiment_display_raw_20260807_run2.jsonl` | run 2 t4/t5 (event types present) |
| `real_experiment_latency_20260807_run2.csv` | run 2 merged, 31 rows, **21 failed** — keep as evidence of the defect, do not use for the latency table |
| `real_experiment_latency_20260807_run2_preempted.csv` | run 2's 13 preempted |
| `latency_summary.md` | analyse_latency output for run 1 |

Naming rules from handoff 1 still hold: real-session files contain
`real_experiment` and the date, never `sample`, and never overwrite work
package B's files.

---

## 8. Tooling reference

- `evaluation/commentary/scripts/capture_display_latency.py` — WebSocket client
  that stamps `ai_done` (t4) and `tts_audio` (t5) arrivals. Both carry the same
  `request_id`, so the join with the backend log is exact. It also binds the
  event type from the preceding `event_detected` (whose key is `event_type`,
  **not** `type` — reading the wrong key cost run 1 its per-type breakdown).
  Records `error` broadcasts as failures and websocket drops as `disconnected`.
- `evaluation/commentary/scripts/build_latency_csv.py` — joins the two JSONL
  files into a `LATENCY_SCHEMA` CSV. Mints `event_id` (a required non-empty
  column that midware never populates) as `<event_type>_<NNN>`. Flags
  `--t0 oldest|newest`, `--include-duplicates`.
- `midware/latency_log.py` — `record()` now takes an optional explicit
  `timestamp`, used only for t0, whose true time comes from the frame's
  wall-clock `_received_at` and must be converted onto the monotonic clock
  (`runtime.py::_frame_t0_monotonic`). Handoff 1 told you to use the frame's
  `sim_time` for t0 — **that was wrong**: `sim_time` is TORCS simulation time,
  a third unrelated clock, and subtracting it from t1 yields nonsense.
- `evaluation/commentary/templates/` — five header+example CSVs, one per
  schema. `test_all_five_templates_are_valid` reads them; they did not exist
  in the repo before this session and that test was failing.
