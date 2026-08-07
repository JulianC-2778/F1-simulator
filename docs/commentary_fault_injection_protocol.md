# AI Live Commentary — Fault Injection Protocol (work package D, part 2)

> Self-contained operational script for the 12-fault RT-01..RT-12 matrix.
> Read `docs/commentary_test_handoff.md` first if you're a fresh session —
> it has environment setup, prior findings, and file-naming conventions
> this document assumes. The fault catalog below comes from
> `docs/AI Live Commentary test contract_8_5.md` section 6.2 (which says
> "each fault repeated 5 times" but doesn't enumerate fault types) combined
> with the more detailed RT-01..RT-12 table from the original
> `docs/commentary_test_plan.md` section 8.2 — this document reconciles
> the two into one executable checklist.

## 0. Before starting

```bash
bash tools/run_tests.sh --service   # L0-L3 must be green
```

Every trial writes one row into
`evaluation/commentary/results/real_experiment_fault_recovery_<DATE>.csv`,
schema (`evaluation/commentary/schemas/csv_schemas.py::FAULT_RECOVERY_SCHEMA`):

```
trial_id,fault_id,fault_injected_at_s,service_restored_at_s,first_success_after_restore_at_s,recovered,crashed,notes
```

- `*_at_s`: seconds since **midware process start** (`date +%s` at launch
  minus current `date +%s`, or just use wall-clock epoch seconds
  consistently for all three columns in a trial — what matters is the
  three timestamps in one row are on the same clock).
- `recovered`/`crashed`: literal `true`/`false`.
- Leave `service_restored_at_s`/`first_success_after_restore_at_s` blank
  for fault kinds with no restoration step (RT-07, RT-08, RT-09, RT-12 —
  noted per-fault below).
- If a fault kind genuinely doesn't apply to this architecture, write why
  in `notes` and leave `recovered`/`crashed` blank rather than inventing a
  trial — this is explicitly allowed by the contract document.

Recovery time = `first_success_after_restore_at_s - service_restored_at_s`.
Run each fault **5 times** (fresh row each time, `trial_id` like
`RT01-1`..`RT01-5`).

Keep the WebSocket listener pattern from `docs/manual-test-guide.md` 0.4
running throughout — it's how you'll see `ai_start`/`ai_done`/`error`
live and notice the first successful output after a fault clears:

```bash
cd ~/F1-simulator && source .venv/bin/activate
python - <<'EOF'
import json, time, websocket
ws = websocket.create_connection("ws://127.0.0.1:8880/ws")
print("connected, watching...")
while True:
    m = json.loads(ws.recv())
    if m.get("type") == "telemetry_update":
        continue
    print(f"{time.time():.3f} [{m.get('type')}] source={m.get('source')} "
          f"{str(m.get('content') or m.get('message') or '')[:100]}")
EOF
```

---

## RT-01: Granite unavailable

**Inject**: point midware at a dead endpoint so the model call fails.

```bash
curl -s -X POST 127.0.0.1:8880/api/config/api -H 'Content-Type: application/json' \
  -d '{"base_url":"http://127.0.0.1:1/v1"}'   # nothing listens on port 1
```

Trigger commentary (drive, or `POST /api/commentary/manual`) and confirm
the WebSocket listener shows a controlled `error` message, **not** a
midware crash — `curl 127.0.0.1:8880/api/health` should still return 200.

**Restore** → RT-02.

## RT-02: Granite restored

**Inject** (immediately follows RT-01 in the same trial): point the
config back at the real LM Studio address, without restarting midware.

```bash
curl -s -X POST 127.0.0.1:8880/api/config/api -H 'Content-Type: application/json' \
  -d '{"base_url":"http://<LM_STUDIO_ADDRESS>:1234/v1","model":"<MODEL_ID>"}'
```

Note the time. Trigger commentary again; note the time of the next
`ai_done` in the listener — that's `first_success_after_restore_at_s`.
Confirm: **no midware restart was needed.**

## RT-03: WebSocket/browser-dashboard disconnect

**Inject**: with the dashboard open in a browser (`http://127.0.0.1:8880/`),
close the tab (or kill the WebSocket listener script if you're using that
as the "client"). Confirm `curl 127.0.0.1:8880/api/health` still returns
200 and `ws_clients` count drops — midware must not crash from a client
disconnect.

## RT-04: Dashboard reconnect

**Inject**: reopen the dashboard tab (or reconnect the listener script).
Trigger a new event and confirm the new client receives fresh
`ai_start`/`ai_done` messages without any manual reset.

## RT-05: Telemetry/UDP interruption

**Inject**: stop TORCS mid-session (kill the process, or just quit to
menu) so UDP packets to port 3101 stop arriving.

```bash
pkill -f torcs   # or quit to menu manually
```

Watch `/api/health`'s `telemetry.status.is_stale` flip to `true`. **Confirm
no new commentary events appear during the interruption** — the system
must not invent events from stale/frozen data.

```bash
watch -n1 "curl -s 127.0.0.1:8880/api/health | python3 -c \"import json,sys; print(json.load(sys.stdin)['telemetry']['status'])\""
```

## RT-06: Telemetry restored

**Inject**: relaunch TORCS / resume driving. Confirm `is_stale` returns to
`false` and new events start being detected again (`/api/events/recent`
shows fresh `sim_time` values) without restarting midware.

## RT-07: Invalid telemetry

No real-driving equivalent — push malformed data directly via the debug
endpoint:

```bash
curl -s -X POST 127.0.0.1:8880/api/telemetry/push -H 'Content-Type: application/json' \
  -d '{"telemetry": {"sim_time": "not-a-number", "speedX": "NaN", "damage": null}, "rankings": []}'
curl -s 127.0.0.1:8880/api/health -o /dev/null -w "health after bad frame: %{http_code}\n"
```

Confirm: `/api/health` still 200 (no crash), and either the frame is
safely dropped/defaulted or a clear error is returned — check midware's
stdout log for an unhandled traceback (there must be none).
No restoration step — leave `service_restored_at_s` /
`first_success_after_restore_at_s` blank, `crashed=false` if the process
survived.

## RT-08: Duplicate frames

```bash
FRAME='{"telemetry": {"sim_time": 500.0, "lap": 3, "speedX": 120.0, "damage": 0, "racePos": 2}, "rankings": []}'
for i in 1 2 3 4 5; do
  curl -s -X POST 127.0.0.1:8880/api/telemetry/push -H 'Content-Type: application/json' -d "$FRAME" > /dev/null
done
```

Confirm the WebSocket listener shows **no duplicate user-visible
commentary** from resending the identical frame 5 times (the same event
shouldn't re-fire once already reported and cooled down). No restoration
step, same blank-field rule as RT-07.

## RT-09: High event frequency

```bash
for lap in $(seq 10 30); do
  curl -s -X POST 127.0.0.1:8880/api/telemetry/push -H 'Content-Type: application/json' \
    -d "{\"telemetry\": {\"sim_time\": $((600+lap)), \"lap\": $lap, \"speedX\": 150.0, \"damage\": 0, \"racePos\": 2}, \"rankings\": []}" \
    > /dev/null
  sleep 0.1
done
```

This fires a `lap_complete` candidate on almost every push. Confirm via
`/api/health`'s `model.scheduler` field that `queued` stays bounded (not
growing without limit) and no unhandled exceptions appear in the midware
log — the priority/cooldown/single-task-slot machinery
(`_auto_commentary_loop`) should supersede rather than queue unboundedly.
No restoration step.

## RT-10: TTS failure

Requires Kokoro set up per `docs/commentary_test_handoff.md` §5 and TTS
enabled (`POST /api/config/tts` with `enabled: true`).

**Inject**: stop the `tts_server.py` process.

```bash
pkill -f tts_server.py
```

Trigger commentary. Confirm captions still display (WebSocket `ai_done`
still arrives with real text) even though `tts_audio` never comes —
the caption path must not be blocked by the audio failure.

**Restore**: `python tts_server.py &` again; confirm TTS resumes on the
next trigger without restarting midware.

If TTS isn't set up on this machine, skip RT-10 and say so explicitly in
the final write-up rather than fabricating a trial.

## RT-11: UDP port occupied

**Inject**: occupy port 3101 *before* starting midware, then try to start it.

```bash
python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('0.0.0.0', 3101)); import time; time.sleep(30)" &
OCCUPY_PID=$!
.venv/bin/python -m midware.app   # run in foreground, don't backgrounds this one
```

Confirm the startup failure message is **specific and actionable**
(names the port, doesn't silently hang or print a bare stack trace with
no explanation), then:

```bash
kill $OCCUPY_PID
```

and start midware normally. No `recovered`/`crashed` fields in the usual
sense here — record whether the failure message was clear in `notes`.

## RT-12: Commentary disabled

**Inject**:

```bash
curl -s -X POST 127.0.0.1:8880/api/features/enabled -H 'Content-Type: application/json' \
  -d '{"enabled":["engineer","coach","bot"]}'   # commentary omitted
```

Drive / trigger events. Confirm `/api/commentary/manual` returns 409 and
no new `ai_start`/`ai_done` for commentary appears even though
`/api/events/recent` may still show detections happening internally.

**Restore**:

```bash
curl -s -X POST 127.0.0.1:8880/api/features/enabled -H 'Content-Type: application/json' \
  -d '{"enabled":["commentary","engineer","coach","bot"]}'
```

No timed recovery in the usual sense — record in `notes` whether new
commentary resumed immediately after re-enabling.

---

## After all 60 trials (12 faults × 5)

```bash
.venv/bin/python evaluation/commentary/scripts/validate_experiment_data.py \
  --kind fault_recovery --file evaluation/commentary/results/real_experiment_fault_recovery_<DATE>.csv
.venv/bin/python evaluation/commentary/scripts/analyse_stability.py faults \
  --file evaluation/commentary/results/real_experiment_fault_recovery_<DATE>.csv \
  --out-dir evaluation/commentary/results/
```

This prints/writes the "Fault condition / Trials / Successful recovery /
Median recovery time / Crashes / Result" table used in the paper. Keep
whatever it produces even if some faults show `FAIL` or `PARTIAL` — see
`docs/AI Live Commentary test contract_8_5.md`'s explicit rule against
adjusting results after seeing them.
