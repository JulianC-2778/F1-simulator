#!/bin/bash
# Runs every curl-able scenario in evaluation/engineer/results/controlled_experiment_answer_accuracy_20260808.tsv
# against a locally running midware (http://127.0.0.1:8880).
# Every car_state below includes a "problems" field matching what race_analyzer.analyze_car_state()
# would compute for a live telemetry frame with those values -- required because ask_engineer() only
# auto-computes "problems" for live telemetry, not for a car_state supplied directly in the request body.
# Usage: bash evaluation/engineer/run_answer_accuracy_tests.sh
# Every run auto-saves to its own timestamped file (see LOG_FILE below) so
# re-running after a persona/code fix never overwrites a previous run's
# results -- needed to keep before/after evidence for the paper.
set -u
BASE="http://127.0.0.1:8880"
LOG_FILE="evaluation/engineer/results/run_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG_FILE") 2>&1
echo "Logging this run to: $LOG_FILE"

clear_hist() { curl -s -X POST "$BASE/api/engineer/clear" > /dev/null; }
set_style()  { curl -s -X POST "$BASE/api/engineer/style" -H "Content-Type: application/json" -d "{\"style\": \"$1\"}" > /dev/null; echo "-- style: $1 --"; }

echo "############## PROFESSIONAL STYLE ##############"
set_style professional

echo "=== A1 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["normal"]}}' | python3 -m json.tool; echo

echo "=== A2 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 5.0, "lap_time": 90.0, "problems": ["fuel low"]}}' | python3 -m json.tool; echo

echo "=== A3 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 3500.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["car damage high"]}}' | python3 -m json.tool; echo

echo "=== A4 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 1.2, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["off track"]}}' | python3 -m json.tool; echo

echo "=== A5 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.85, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["near track edge"]}}' | python3 -m json.tool; echo

echo "=== A6 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 9000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["rpm too high"]}}' | python3 -m json.tool; echo

echo "=== A7 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 100.0, "rpm": 2000.0, "gear": 4, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["rpm too low"]}}' | python3 -m json.tool; echo

echo "=== A8 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 60.0, "rpm": 3000.0, "gear": 4, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["gear too high"]}}' | python3 -m json.tool; echo

echo "=== A9 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.85, "damage": 3500.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["car damage high", "near track edge"]}}' | python3 -m json.tool; echo

echo "=== A10 (3 real triggers, MAX_PROBLEMS=2 cutoff check) ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 1.2, "damage": 3500.0, "fuel": 5.0, "lap_time": 90.0, "problems": ["off track", "car damage high"]}}' | python3 -m json.tool; echo

echo "=== B1 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "Should I pit?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 5.0, "lap_time": 90.0, "problems": ["fuel low"]}}' | python3 -m json.tool; echo

echo "=== B2 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "Should I pit?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["normal"]}}' | python3 -m json.tool; echo

echo "=== B3 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "Should I pit?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 3500.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["car damage high"]}}' | python3 -m json.tool; echo

echo "=== B4 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "Should I pit?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 1.2, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["off track"]}}' | python3 -m json.tool; echo

echo "=== B5 (pit-priority generalization: rpm too high, not off-track) ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "Should I pit?", "car_state": {"speed": 200.0, "rpm": 9000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["rpm too high"]}}' | python3 -m json.tool; echo

echo "=== C1 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.79, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["normal"]}}' | python3 -m json.tool; echo

echo "=== C2 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.81, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["near track edge"]}}' | python3 -m json.tool; echo

echo "=== C3 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 1.01, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["off track"]}}' | python3 -m json.tool; echo

echo "=== C4 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 1499.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["normal"]}}' | python3 -m json.tool; echo

echo "=== C5 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 1501.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["car damage medium"]}}' | python3 -m json.tool; echo

echo "=== C6 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 2999.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["car damage medium"]}}' | python3 -m json.tool; echo

echo "=== C7 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 3001.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["car damage high"]}}' | python3 -m json.tool; echo

echo "=== C8 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 7.9, "lap_time": 90.0, "problems": ["fuel low"]}}' | python3 -m json.tool; echo

echo "=== C9 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 8.1, "lap_time": 90.0, "problems": ["normal"]}}' | python3 -m json.tool; echo

echo "=== F1 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "What is the weather like today?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["normal"]}}' | python3 -m json.tool; echo

echo "=== F2 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "Do you think this game is fun?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["normal"]}}' | python3 -m json.tool; echo

echo "=== F3 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "What is 1 plus 1?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["normal"]}}' | python3 -m json.tool; echo

echo "=== H2 (missing fuel field, no problems -- deliberately malformed) ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "lap_time": 90.0}}' | python3 -m json.tool; echo

echo "=== H3 (fuel wrong type, no problems -- deliberately malformed) ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": "low", "lap_time": 90.0}}' | python3 -m json.tool; echo

echo "=== H4 (empty car_state, no problems -- deliberately malformed) ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {}}' | python3 -m json.tool; echo

echo "############## D: MULTI-TURN (history intentionally kept between the two turns) ##############"
echo "=== D1 turn 1 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "Should I pit?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 5.0, "lap_time": 90.0, "problems": ["fuel low"]}}' | python3 -m json.tool; echo
echo "=== D1 turn 2 (same car_state, no clear_hist -- must not contradict turn 1) ==="
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "Why?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 5.0, "lap_time": 90.0, "problems": ["fuel low"]}}' | python3 -m json.tool; echo

echo "=== D2 turn 1 ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["normal"]}}' | python3 -m json.tool; echo
echo "=== D2 turn 2 (same car_state, no clear_hist -- must build on turn 1) ==="
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "Can I push for a sprint then?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["normal"]}}' | python3 -m json.tool; echo

echo "############## CONCISE STYLE SPOT-CHECKS ##############"
set_style concise

echo "=== A1-concise ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["normal"]}}' | python3 -m json.tool; echo

echo "=== A3-concise ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 3500.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["car damage high"]}}' | python3 -m json.tool; echo

echo "=== B1-concise ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "Should I pit?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 5.0, "lap_time": 90.0, "problems": ["fuel low"]}}' | python3 -m json.tool; echo

echo "=== C7-concise ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.1, "damage": 3001.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["car damage high"]}}' | python3 -m json.tool; echo

echo "=== A9-concise (first multi-problem case under concise style) ==="; clear_hist
curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
  -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.85, "damage": 3500.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["car damage high", "near track edge"]}}' | python3 -m json.tool; echo

echo "############## BACK TO PROFESSIONAL: REPEAT-CONSISTENCY TRIALS ##############"
set_style professional

for i in 1 2 3; do
  echo "=== A9-R$i ==="; clear_hist
  curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
    -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 0.85, "damage": 3500.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["car damage high", "near track edge"]}}' | python3 -m json.tool; echo
done

for i in 1 2 3; do
  echo "=== A4-R$i ==="; clear_hist
  curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
    -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 6000.0, "gear": 5, "track_pos": 1.2, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["off track"]}}' | python3 -m json.tool; echo
done

for i in 1 2 3; do
  echo "=== A6-R$i ==="; clear_hist
  curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
    -d '{"question": "How is the car right now?", "car_state": {"speed": 200.0, "rpm": 9000.0, "gear": 5, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["rpm too high"]}}' | python3 -m json.tool; echo
done

for i in 1 2 3; do
  echo "=== A7-R$i ==="; clear_hist
  curl -s -X POST "$BASE/api/engineer/ask" -H "Content-Type: application/json" \
    -d '{"question": "How is the car right now?", "car_state": {"speed": 100.0, "rpm": 2000.0, "gear": 4, "track_pos": 0.1, "damage": 0.0, "fuel": 50.0, "lap_time": 90.0, "problems": ["rpm too low"]}}' | python3 -m json.tool; echo
done

echo "############## DONE ##############"
echo "Remaining manual step NOT covered by this script:"
echo "  H1 -- stop LM Studio / model backend, then run one curl by hand (see TSV), then restart LM Studio"
