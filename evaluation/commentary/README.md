# AI Live Commentary evaluation tooling

Deterministic, dependency-free (stdlib only) scripts supporting work
packages B/C/D of `docs/commentary_test_plan.md`. Work package A (automated
functional tests) lives in `tests/unit/test_commentary_*.py` and
`tests/integration/test_commentary_runtime.py` instead -- run those with
`tools/commentary_test_report.py`, not from here.

Every CSV this package reads or writes is one of exactly three kinds, and
which kind it is should always be obvious from its name/location:

- **template** (`templates/*.csv`): minimal schema examples, 1-3 rows.
- **sample data** (`sample_data/SAMPLE_*.csv`): a small hand-built demo
  dataset used to prove the scripts work end-to-end. **Not a real
  experiment result** -- every report generated from it says so.
- **real experiment data**: whatever you produce running an actual TORCS +
  Granite + Overlay session per `docs/commentary_experiment_protocol.md`.
  Store it under `results/` with a filename that says `real_experiment`,
  not `sample`.

## Layout

```
schemas/csv_schemas.py       column definitions for all 5 CSV kinds (single source of truth)
scripts/
  word_count.py               canonical English word-count function (max_words gap, see matrix.md §4)
  validate_experiment_data.py schema validation CLI
  match_events.py             deterministic 1:1 event matching + Precision/Recall/F1
  latency_stats.py            shared count/min/mean/stdev/median/P95/max helper (P95 method documented in-file)
  analyse_latency.py          t0-t5 -> detection/first-token/generation/caption/tts latency table
  analyse_stability.py        endurance-run and fault-recovery summary tables
  generate_report_tables.py   assembles everything into one report, "NOT RUN" for missing inputs
templates/                    schema examples
sample_data/                  SAMPLE_*.csv demo dataset (see banner rule above)
results/                      generated JUnit XML / summary markdown lands here (gitignore real data if sensitive)
```

## Quick start

```bash
# Work package A -- automated tests + summary table
.venv/bin/python tools/commentary_test_report.py

# Work package B -- validate + match sample data
.venv/bin/python evaluation/commentary/scripts/validate_experiment_data.py \
  --kind ground_truth --file evaluation/commentary/sample_data/SAMPLE_ground_truth.csv
.venv/bin/python evaluation/commentary/scripts/match_events.py \
  --ground-truth evaluation/commentary/sample_data/SAMPLE_ground_truth.csv \
  --detections evaluation/commentary/sample_data/SAMPLE_detected_events.csv \
  --tolerance 1.0

# Work package C -- latency (only meaningful with COMMENTARY_LATENCY_LOG=1
# capturing real t1-t3 during a live session, see midware/latency_log.py)
.venv/bin/python evaluation/commentary/scripts/analyse_latency.py \
  --file evaluation/commentary/sample_data/SAMPLE_latency.csv

# Work package D -- endurance + fault recovery
.venv/bin/python evaluation/commentary/scripts/analyse_stability.py endurance \
  --file evaluation/commentary/sample_data/SAMPLE_stability_run.csv
.venv/bin/python evaluation/commentary/scripts/analyse_stability.py faults \
  --file evaluation/commentary/sample_data/SAMPLE_fault_recovery.csv

# Everything combined into one report (sample data, clearly labelled)
.venv/bin/python evaluation/commentary/scripts/generate_report_tables.py --sample \
  --out evaluation/commentary/results/sample_report.md
```

Run every script's own test suite:

```bash
.venv/bin/python -m pytest evaluation/commentary/tests -q
```

## Capturing real latency data

`midware/latency_log.py` is disabled by default (zero behaviour change,
verified in `tests/unit/test_latency_log.py`). To capture real t1/t2/t3
timestamps during a live session:

```bash
COMMENTARY_LATENCY_LOG=1 COMMENTARY_LATENCY_LOG_PATH=/path/to/latency.jsonl python -m midware.app
```

This produces one JSON line per (request_id, stage). `t0_telemetry_received`
(already present as each frame's `sim_time`) and `t4_caption_displayed` /
`t5_tts_started` (Overlay/browser-side, not backend-observable) are **not**
captured by this logger -- see `docs/commentary_experiment_protocol.md` for
how a real work-package-C session fills those two in and reshapes the JSONL
into the `latency` CSV schema `analyse_latency.py` expects.

## Known gap this tooling exists partly to document

`docs/commentary_test_matrix.md` section 4: `max_words` (default 45) is a
prompt-level hint that never reaches the model and is never enforced on
output. `analyse_stability.py`'s `outputs_over_45_words` /
`violation_rate_pct` measure how often real output actually exceeds it --
this is deliberately a measurement, not a pass/fail gate, per
`commentary_test_plan.md` 5.5's explicit "do not default-truncate" rule.
