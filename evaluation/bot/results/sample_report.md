# AI Driving Bot Evaluation (SAMPLE DATA -- NOT REAL RESULTS)

Generated: 2026-08-14T00:10:43.871918+00:00

## Evaluation Method

See docs/bot_test_plan.md and docs/bot_test_matrix.md for the full requirement-to-code traceability this report is built from. Work package A (automated functional tests) lives in `tests/bot/` -- run with `pytest tests/bot`, not from here.

## Work Package A: Functional Correctness

### Automated test summary

NOT RUN -- no data supplied for this run. To fill this in:

```
python -m pytest tests/bot -q
```

## Work Package B: Strategy Accuracy

| Strategy | Ground truth | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| ATTACK | 1 | 1 | 1 | 0 | 0.5000 | 1.0000 | 0.6667 |
| BLOCK | 1 | 1 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| DEFEND | 1 | 1 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| NORMAL | 3 | 3 | 1 | 0 | 0.7500 | 1.0000 | 0.8571 |
| PIT | 1 | 1 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| SAVE_FUEL | 1 | 0 | 0 | 1 | N/A | 0.0000 | N/A |
| **Overall (micro)** | 8 | 7 | 2 | 1 | 0.7778 | 0.8750 | 0.8235 |

## Work Package B: Driving Quality (bot-autonomous)

| Session | Track | Granite | Laps | Completed | Distance (km) | Off-track excursions | Recoveries | Collisions | Lap time mean (s) | Lap time stdev (s) | Strategy switches |
|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|
| SB1 | g-track-2 | true | 3/3 | true | 9.450 | 1 | 1 | 0 | 98.400 | 1.200 | 4 |
| SB2 | g-track-2 | false | 2/3 | false | 6.200 | 3 | 2 | 1 | 101.100 | 3.400 | 0 |

Completion rate: 1/2 sessions

## Work Package C: End-to-End Latency

| Stage | N | Median | P95 | Maximum | Failures |
|---|---:|---:|---:|---:|---:|
| Control loop (compute) | 5 | 0.002 | 0.002 | 0.002 | 1 |
| Control loop (send, UDP) | 5 | 0.000 | 0.000 | 0.000 | 1 |
| Control loop (frame, total) | 5 | 0.002 | 0.003 | 0.003 | 1 |
| Granite strategy RTT | 2 | 5.975 | 9.417 | 9.800 | 1 |
| Granite debounce overhead | 2 | 0.001 | 0.001 | 0.001 | 1 |

## Work Package D: Stability and Fault Recovery

| Run | Duration (s) | Frames | Strategy requests | Granite failures | Request success rate | Safety filter interventions | Collisions | Off-track excursions | Recoveries | Unhandled exceptions |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| RB01 | 1200.0 | 59800 | 80 | 2 | 97.50% | 6 | 0 | 2 | 2 | 0 |
| RB02 | 1200.0 | 59750 | 79 | 1 | 98.73% | 4 | 1 | 1 | 1 | 0 |
| RB03 | 1200.0 | 59900 | 0 | 0 | N/A | 3 | 0 | 3 | 3 | 0 |
| **Total** | 3600.0 | 179450 | 159 | 3 | 98.11% | 13 | 1 | 6 | 6 | 0 |

| Run | PIT (fuel/damage override) | DEFEND (severe damage) | BLOCK (rear-gap) | ATTACK capped to NORMAL (damage/fuel) |
|---|---:|---:|---:|---:|
| RB01 | 1 | 0 | 4 | 1 |
| RB02 | 0 | 1 | 2 | 1 |
| RB03 | 1 | 1 | 0 | 1 |
| **Total** | 2 | 2 | 6 | 3 |

| Fault condition | Trials | Successful recovery | Median recovery time | Crashes | Result |
|---|---:|---:|---:|---:|---|
| RB-01 | 2 | 2 | 6.00s | 0 | PASS |
| RB-03 | 2 | 1 | 2.10s | 1 | FAIL (crash in 1/2) |
| RB-05 | 1 | 1 | N/A | 0 | PASS |
| RB-06 | 1 | 1 | N/A | 0 | PASS |

## Limitations

- Work packages B/C/D require a live TORCS + LM Studio/Granite stack (driven directly over the SCR UDP protocol, not through midware); any section above marked NOT RUN needs that real environment, not more code.
- Strategy-accuracy matching in work package B only scores `filtered_strategy` (safety_filter's output) against human-annotated expectations -- it does not separately score `raw_granite_strategy`.
- Sample data (if this report used --sample) demonstrates the pipeline only; it is not evidence of real strategy accuracy, driving quality, latency or stability.
