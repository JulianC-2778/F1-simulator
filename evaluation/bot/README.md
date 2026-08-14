# AI Driving Bot evaluation tooling

Deterministic, dependency-free (stdlib only) scripts supporting work
packages B/C/D of `docs/bot_test_plan.md`. Work package A (automated
functional tests) lives in `tests/bot/` instead -- run with
`python -m pytest tests/bot -q`, not from here. Direct counterpart of
`evaluation/commentary/`; see `docs/bot_test_plan.md` section 0 for why the
two directions (AI Live Commentary, AI Driving Bot) share one evaluation
methodology, and `docs/bot_test_matrix.md` for how far each work package
actually got.

Every CSV this package reads or writes is one of exactly three kinds, and
which kind it is should always be obvious from its name/location:

- **template** (`templates/*.csv`): minimal schema examples, 1-2 rows.
- **sample data** (`sample_data/SAMPLE_*.csv`): a small hand-built demo
  dataset used to prove the scripts work end-to-end. **Not a real
  experiment result** -- every report generated from it says so.
- **real experiment data**: whatever you produce running an actual TORCS +
  Granite + `ai_bot.py --bot --granite` session (see `docs/full-stack-e2e-startup.md`
  for bringing the stack up). Store it under `results/` with a filename
  that says `real_experiment`, not `sample`.

## Layout

```
schemas/csv_schemas.py         column definitions for all 6 CSV kinds (single source of truth)
scripts/
  latency_stats.py              shared count/min/mean/stdev/median/P95/max helper (P95 method documented in-file)
  validate_experiment_data.py   schema validation CLI
  match_strategy_decisions.py   deterministic 1:1 strategy-reading matching + Precision/Recall/F1
  analyse_control_latency.py    u0-u2 control loop + g0-g3 Granite RTT -> latency table
  analyse_stability.py          endurance-run, safety_filter-breakdown, and fault-recovery summary tables
  generate_report_tables.py     assembles everything into one report, "NOT RUN" for missing inputs
templates/                      schema examples
sample_data/                    SAMPLE_*.csv demo dataset (see banner rule above)
results/                        generated real-experiment CSVs / summary markdown lands here
```

`evaluation/bot/scripts/analyse_bot_trace.py` (pre-existing, not listed
above) is a separate, complementary tool: it computes work-package-B/C
*proxy* metrics directly from a real `ai_bot.py` `TraceRecorder` JSONL log
(`TORCS_BOT_TRACE`), without needing hand-annotated ground truth first. See
its own docstring and `docs/bot_real_experiment_20260812.md` for how its
output relates to (but does not replace) the CSV-based pipeline here --
in particular, the `lap_performance` CSV this package validates is meant to
be filled in by hand-summarizing one `analyse_bot_trace.py` run per row,
one row per real session.

## Quick start

```bash
# Work package A -- automated tests
.venv/bin/python -m pytest tests/bot -q

# Work package B (strategy accuracy) -- validate + match sample data
.venv/bin/python evaluation/bot/scripts/validate_experiment_data.py \
  --kind ground_truth_strategy --file evaluation/bot/sample_data/SAMPLE_ground_truth_strategy.csv
.venv/bin/python evaluation/bot/scripts/match_strategy_decisions.py \
  --ground-truth evaluation/bot/sample_data/SAMPLE_ground_truth_strategy.csv \
  --detections evaluation/bot/sample_data/SAMPLE_detected_strategy.csv \
  --tolerance 2.0

# Work package B (driving quality, bot-autonomous) -- validate only, no
# matching step (there's no ground truth/detection pair for this one)
.venv/bin/python evaluation/bot/scripts/validate_experiment_data.py \
  --kind lap_performance --file evaluation/bot/sample_data/SAMPLE_lap_performance.csv

# Work package C -- control-loop + Granite RTT latency
.venv/bin/python evaluation/bot/scripts/analyse_control_latency.py \
  --file evaluation/bot/sample_data/SAMPLE_control_latency.csv

# Work package D -- endurance + fault recovery
.venv/bin/python evaluation/bot/scripts/analyse_stability.py endurance \
  --file evaluation/bot/sample_data/SAMPLE_stability_run.csv
.venv/bin/python evaluation/bot/scripts/analyse_stability.py faults \
  --file evaluation/bot/sample_data/SAMPLE_fault_recovery.csv

# Everything combined into one report (sample data, clearly labelled)
.venv/bin/python evaluation/bot/scripts/generate_report_tables.py --sample \
  --out evaluation/bot/results/sample_report.md
```

## Capturing real latency data

Work package C's control-loop chain (`u0_scr_state_received` ->
`u1_control_computed` -> `u2_control_sent`) and Granite chain
(`g0_state_snapshot` -> `g2_response_complete` -> `g3_strategy_applied`) are
not yet emitted by `ai_bot.py` itself -- there is no `TORCS_BOT_LATENCY_LOG`
opt-in hook analogous to `midware/latency_log.py`
(`COMMENTARY_LATENCY_LOG`). Two ways to fill in `control_latency.csv` today:

- **Compute-only, no real TORCS** (bot_test_plan.md 6.2's explicit
  exception): drive `compute_control` directly through 1000+ synthetic
  frames using a fake `ScrClient` (the same harness
  `tests/bot/test_run_bot_integration.py` already uses) and record
  wall-clock deltas around each call -- this measures `u1-u0` only, pure
  CPU cost, no network involved.
- **Real UDP `send_latency`/`frame_latency`**: would need a logging hook
  added to `ai_bot.py`'s `run_bot()` loop, gated behind an env var the
  same way `midware/latency_log.py` is. **Descoped by the operator on
  2026-08-14** rather than left as an open gap: `send_control()` is one
  synchronous local UDP `socket.send()` call, not a network wait, so the
  real bottleneck for keeping up with `scr_server`'s 20ms step is almost
  entirely `u1-u0` (already measured and passing, see the compute-only
  bullet above); a `u2-u1` number measured over WSL2's loopback network
  wouldn't represent a real deployment and wouldn't change any decision.
- **Real Granite RTT**: this part of the gap is closed as of PR #38-40 --
  `GraniteStrategist` now stamps a real `round_trip_s` on every completed
  request, and `TraceRecorder.decision()` logs it unconditionally (not only
  when the answer text changes). Run with `TORCS_BOT_TRACE=<path>` and read
  `round_trip_s` off every `"kind": "decision"` record in the resulting
  JSONL -- see `docs/bot_real_experiment_20260814.md` §4.2 for a worked
  example (N=58, median 9.43s). Still a whole-request figure, not a
  first-token timestamp -- `_call_granite` has no intermediate hook for
  that yet.

## Known gaps this tooling exists partly to measure, not fabricate

- `ai_bot.py` has no opt-in latency-logging hook for real UDP
  `send_latency`/`frame_latency` (see above) -- descoped by the operator
  on 2026-08-14, not planned. The `control_latency` schema and
  `analyse_control_latency.py` remain valid for the compute-only `u1-u0`
  piece, which is real, measured, and passing
  (`tests/bot/test_control_loop_latency.py`).
- Work packages B/C/D's full designs (2 tracks x 3 sessions, 30
  independent Granite RTT samples) have not been run in full -- but the
  "5 repetitions per RB-01..RB-10 fault" part of work package D **has**:
  all ten fault IDs reached 5 real trials each as of 2026-08-14, all 50
  passing (`docs/bot_fault_injection_20260812.md` /
  `..._20260814.md` / `..._20260814b.md`). Two other parts of the original
  design were descoped by the operator on 2026-08-14 and are not part of
  this gap list anymore: human-driven ground-truth sessions / the P/R/F1
  strategy-accuracy evaluation (`docs/bot_test_plan.md` section 5.1 --
  human driving judged too different from the bot's to make the
  comparison meaningful; work package B is now validated only via the
  bot-autonomous driving-quality scenario) and the 3x20-minute
  endurance-run requirement (`docs/bot_test_plan.md` section 7.1) -- see
  `docs/bot_test_matrix.md`
  section 6 and
  `docs/bot_real_experiment_20260812.md` / `docs/bot_real_experiment_20260814.md`
  / `docs/bot_real_experiment_20260814b.md` / `docs/bot_real_experiment_20260814d.md`
  / `docs/bot_real_experiment_20260814g.md`
  (5 real bot-autonomous sessions so far across 3 tracks -- Forza's 3-rep
  design is complete) for exactly what real data exists so far. Repeating
  E-Track 3 and Wheel 1 to match Forza's 3, and adding a code-level
  first-token timestamp inside `_call_granite` (the two other still-open
  items from earlier in this project), were both explicitly descoped by
  the operator on 2026-08-14 -- Forza's 3-rep distribution already showed
  the numbers are reproducible, and driving decisions only act on the
  complete response anyway, so the finer first-token split (unlike for
  commentary's progressive text display) doesn't change any actual
  behavior. See `docs/bot_test_matrix.md` section 6 for the full
  reasoning. Real UDP `send_latency`/`frame_latency` (the line right above
  this one) is a separate, still-open, not-descoped gap.
