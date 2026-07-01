# Feature 2 Standalone Service

This branch keeps the latest `origin/main` commentary stack intact and moves the
Feature 2 telemetry dashboard into a separate service.

## Why this structure

- `midware/commentary.py` stays aligned with the remote mainline.
- Feature 2 lives in new files, so future merges are less likely to conflict.
- The dashboard reads telemetry from the existing commentary service instead of
  opening a second UDP listener on port `3101`.

## Files

- `midware/feature2_service.py`
  Standalone FastAPI app for the Feature 2 web UI and dashboard API.
- `midware/feature2_core.py`
  Shared dashboard and rule-engine logic used by the standalone service.
- `midware/static/feature2.html`
  Dedicated Feature 2 frontend.
- `midware/requirements-feature2.txt`
  Feature 2 runtime requirements, including `openai`.

## Runtime model

1. Start the existing commentary service first on port `8765`.
2. Start the standalone Feature 2 service on port `8766`.
3. Open `http://127.0.0.1:8766/feature2`.

The Feature 2 service calls:

- `http://127.0.0.1:8765/api/telemetry/history`

to read recent telemetry frames from the main commentary service.

## Commands

Commentary service:

```bash
cd /home/yejian/torcs/midware
python commentary.py
```

Feature 2 service:

```bash
cd /home/yejian/torcs
python -m pip install -r midware/requirements-feature2.txt
python midware/feature2_service.py
```

## Ports

- Commentary service: `8765`
- Feature 2 service: `8766`

You can override the Feature 2 port with:

```bash
export TORCS_FEATURE2_PORT=9000
python midware/feature2_service.py
```

## Upstream dependency

If the Feature 2 page loads but reports no telemetry, confirm that:

1. `commentary.py` is already running.
2. TORCS is in a driving or racing state.
3. `http://127.0.0.1:8765/api/telemetry/history` responds.
