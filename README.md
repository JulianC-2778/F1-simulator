# TORCS × IBM Granite: Student AI Racing Project

This project integrates **IBM Granite** models with the open-source racing simulator **TORCS**. It demonstrates four AI racing features and provides a practical walkthrough that students can follow to run the project, understand each feature, and create their own version.

The project was designed for hands-on learning in generative AI, data science, real-time systems, and game development. It can also be used as an IBM SkillsBuild demonstration of how Granite can turn live data into useful decisions and natural-language experiences.

## The Four Features

| Feature | What the student creates | How IBM Granite is used |
| --- | --- | --- |
| **1. AI Race Engineer** | A chat assistant that answers questions such as “Should I pit?” | Interprets live telemetry and returns a short race-engineering answer |
| **2. Telemetry Analysis & Coaching** | A dashboard that explains pace, braking, steering, fuel, and track position | Adds a model-generated pre-race briefing and coaching context |
| **3. Procedural Commentary** | Live commentary for overtakes, crashes, lap changes, battles, and pace events | Converts structured race events into natural commentary |
| **4. AI Race Bot** | An autonomous TORCS driver with strategy and safety logic | Selects high-level strategies such as attack, defend, save fuel, or pit |

All four features are coordinated through the same middleware, model connection, and dashboard. Features 1–3 share the human-driver telemetry store; Feature 4 sends its own SCR race snapshots for strategy decisions. Students can reproduce the complete project or choose one feature to extend.

## System Architecture

```text
                              IBM Granite through an LM API
                                         ^
                                         |
                                Model Broker / Middleware
                              /          |          |       \
                    Race Engineer      Coach    Commentary   Bot Strategy
                         |                |          |             |
                         `---------- Browser Dashboard ------------'
                                         ^
                                         |
Human driver telemetry -- UDP :3101 -----+----- UDP :3001 -- AI Race Bot
          ^                                                   |
          |                                                   v
          `-------------------------- TORCS <------------------'
```

Granite is used for language and high-level reasoning. Time-critical tasks such as telemetry collection, event detection, steering, braking, and safety fallbacks remain local. This makes the system responsive even when model generation is slow or temporarily unavailable.

## Main Project Files

| File or directory | Purpose |
| --- | --- |
| `midware/app.py` | Main backend entry point for all four features |
| `midware/static/dashboard.html` | Browser interface for commentary, engineer, coach, and bot status |
| `midware/context_manager.py` | Engineer/commentary personas, prompt context, and history limits |
| `midware/feature2_core.py` | Telemetry analysis and coaching rules |
| `midware/commentary_engine.py` | Race-event detection, priorities, cooldowns, and deduplication |
| `midware/bot_strategy.py` | Granite prompts and response validation for the AI bot |
| `ai_bot.py` | SCR client, local driving controller, strategy handling, and safety filters |
| `overlay-app/` | Optional Electron floating caption HUD for Feature 1 engineer replies |
| `config.json` | Shared hosts and ports |

## Prerequisites

The recommended environment is Ubuntu 22.04/24.04 or Ubuntu under WSL2. You will need:

- Python 3.10 or newer;
- Git and a C/C++ build toolchain;
- the Linux development libraries required by TORCS;
- access to an OpenAI-compatible LM API;
- an API key if your LM API provider requires one; and
- the Base URL and model ID for an IBM Granite instruct/chat model.

The middleware sends chat-completion requests to `<BASE_URL>/chat/completions`. A local model server is not required; an optional local setup is described in the appendix.

## Part A — Shared Setup

Complete these steps once before trying any of the four features.

### 1. Clone the repository

```bash
git clone https://github.com/JulianC-2778/F1-simulator.git
cd F1-simulator
```

### 2. Create the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-core.txt
```

Use `requirements.txt` instead if you also want the optional voice-input and local TTS features.

### 3. Build TORCS

After installing the TORCS development libraries for your Linux distribution:

```bash
export CFLAGS="-fPIC"
export CPPFLAGS="$CFLAGS"
export CXXFLAGS="$CFLAGS"

./configure --prefix="$(pwd)/BUILD"
make -j"$(nproc)"
make install
make datainstall
```

The simulator will be available at `BUILD/bin/torcs`. See [the full startup guide](docs/full-stack-e2e-startup.md) for platform-specific setup.

### 4. Prepare the Granite LM API

Obtain these three values from your LM API provider or project instructor:

1. **Base URL** — the OpenAI-compatible API root, normally ending in `/v1`;
2. **API key** — the credential used as a Bearer token, if your provider requires one; and
3. **Model ID** — the exact IBM Granite model name accepted by the API.

Do not commit API keys to this repository or include them in screenshots and reports.

### 5. Start the shared middleware

```bash
source .venv/bin/activate
python3 -m midware.app
```

Open the project dashboard:

```text
http://127.0.0.1:8880/static/dashboard.html
```

Enter the LM API Base URL, API key if required, and Granite model ID in the dashboard settings, or configure them through the middleware API:

```bash
curl -X POST http://127.0.0.1:8880/api/config/api \
  -H "Content-Type: application/json" \
  -d '{
    "base_url":"YOUR_LM_API_BASE_URL",
    "api_key":"YOUR_LM_API_KEY",
    "model":"YOUR_GRANITE_MODEL_ID"
  }'
```

Check the complete `middleware → LM API → Granite` connection with:

```bash
curl -X POST http://127.0.0.1:8880/api/engineer/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"Reply with: Granite is ready"}'
```

## Part B — Start the Telemetry Feed

Features 1, 2, and 3 use telemetry from a human-driven TORCS car. In a second terminal:

```bash
mkdir -p logs
export TORCS_PLAYER_LOG_DIR="$PWD/logs"
export TORCS_PLAYER_LOG_HZ=20
export TORCS_PLAYER_UDP_HOST=127.0.0.1
export TORCS_PLAYER_UDP_PORT=3101
bash torcs_launcher.sh
```

In TORCS, open **Race → Quick Race**, select a human driver, choose a track, and start driving. The dashboard should begin showing changing speed, fuel, lap, and position values.

Feature 4 uses a different two-way SCR control connection; its TORCS steps are included in its own walkthrough below.

---

## Feature 1 Walkthrough — AI Race Engineer

### Goal

Create a Granite-powered assistant that combines a driver's question with live telemetry and returns a concise, race-relevant answer.

### Run it

1. Complete the shared setup and start the human-driver telemetry feed.
2. Open the **Engineer** tab in the dashboard.
3. Ask questions such as:
   - “What is my current fuel level?”
   - “Should I pit now?”
   - “Why am I losing pace?”
4. Compare the answer with the live telemetry displayed on screen.

For an optional always-on-top engineer caption window, install the overlay dependencies once and start the Electron overlay:

```bash
cd overlay-app
npm install
npm start
```

The overlay renders Feature 1 engineer messages only. Commentary and coaching output stay in the browser dashboard.

The same feature can be tested without the browser:

```bash
curl -X POST http://127.0.0.1:8880/api/engineer/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"Should I pit now?"}'
```

### Create your own version

1. Edit the professional or concise engineer persona in `midware/context_manager.py`.
2. Choose which telemetry fields should be included in the question context.
3. Add a new supported question, such as a fuel target or damage warning.
4. Keep the rule that the model must not invent unavailable telemetry.

### Verify it

Ask the same question under two different car states. A successful engineer should use the supplied values, change its advice when the situation changes, stay focused on racing, and clearly admit when TORCS does not provide the requested data.

---

## Feature 2 Walkthrough — Telemetry Analysis & Coaching

### Goal

Create a coaching dashboard that turns raw lap data into clear feedback about speed, braking, throttle, steering, track position, fuel use, and areas for improvement.

### Run it

1. Complete the shared setup and start the human-driver telemetry feed.
2. Open the **Coach** tab in the dashboard.
3. Use **Drive now** for live guidance and **Plan and review** for track preparation and session feedback.
4. Drive several laps so the system has enough history to compare behaviour.

The live coaching payload is also available at:

```bash
curl http://127.0.0.1:8880/api/coach/dashboard
```

Request a pre-race briefing with a Granite supplement:

```bash
curl -X POST http://127.0.0.1:8880/api/coach/prebrief \
  -H "Content-Type: application/json" \
  -d '{"driver_style":"auto","road_condition":"dry","use_model":true}'
```

### Create your own version

1. Select one coaching target, such as late braking, unstable steering, or poor corner exits.
2. Add or tune its measurable rule in `midware/feature2_core.py`.
3. Improve the Granite pre-brief prompt so it explains the most important finding clearly.
4. Add track-specific context in `midware/shared/track_profiles.py` if needed.

### Verify it

Record a baseline lap, follow the coaching instruction, and drive another lap. Compare lap time, braking behaviour, track position, and consistency. The feedback should cite observed data and provide one practical action rather than a vague motivational message.

---

## Feature 3 Walkthrough — Procedural Commentary

### Goal

Create live race commentary that reacts to structured events instead of sending every telemetry frame directly to the language model.

### Run it

1. Complete the shared setup and start the human-driver telemetry feed.
2. Open the **Commentary** tab in the dashboard.
3. Select event or hybrid commentary mode in the page, or configure hybrid mode through the API:

```bash
curl -X POST http://127.0.0.1:8880/api/commentary/config \
  -H "Content-Type: application/json" \
  -d '{"mode":"hybrid","max_words":45}'
```

4. Drive a lap and create events naturally: complete a lap, change position, battle another car, leave the track, or make contact.
5. Watch Granite commentary arrive in the dashboard through WebSocket streaming.

You can trigger a manual line for a quick test:

```bash
curl -X POST http://127.0.0.1:8880/api/commentary/manual \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Give one short commentary line about the current race."}'
```

### Create your own version

1. Add or tune an event in `midware/commentary_engine.py`.
2. Select the event data sent to Granite in `midware/event_payload_config.py`.
3. Adjust commentary tone, length, or persona in `midware/context_manager.py`.
4. Set event priority, cooldown, and deduplication so commentary remains timely without repeating itself.

### Verify it

Trigger each chosen event more than once. Confirm that the correct event produces commentary, repeated events are rate-limited, higher-priority moments are handled first, and the model does not claim race facts missing from the event payload.

---

## Feature 4 Walkthrough — AI Race Bot

### Goal

Create an autonomous race bot in which Granite makes high-level strategy decisions while a fast local controller handles steering, throttle, braking, gears, recovery, and safety.

### Run it

Keep Granite and the middleware running. Start TORCS in SCR 2013 mode:

```bash
./BUILD/bin/torcs -ver 2013
```

In TORCS:

1. Open **Race → Quick Race**.
2. Choose a track.
3. Select **scr_server 1** as the driver.
4. Start the race and wait at `Initializing Driver scr_server 1...`.

In another terminal, launch the bot:

```bash
source .venv/bin/activate
python3 ai_bot.py --bot --granite
```

The bot connects to UDP port `3001`. Granite chooses from strategies such as `ATTACK`, `NORMAL`, `DEFEND`, `SAVE_FUEL`, and `PIT`; the local controller converts the active strategy into driving commands.

Test the controller without Granite first if necessary:

```bash
python3 ai_bot.py --bot --strategy NORMAL
```

### Create your own version

1. Edit the strategy descriptions or prompt in `midware/bot_strategy.py`.
2. Add useful telemetry to the race snapshot supplied to Granite.
3. Tune how each strategy changes speed, throttle, braking, or racing line in `ai_bot.py`.
4. To add a strategy, update the allowed values, prompt, controller behaviour, response validation, and safety fallback.

Keep model output structured:

```json
{
  "strategy": "SAVE_FUEL",
  "reason": "Fuel is low for the remaining race distance."
}
```

### Verify it

Run the same track with a fixed `NORMAL` strategy and with Granite enabled. Compare lap time, fuel, damage, strategy changes, and recovery behaviour. Temporarily make the LM API unavailable during a test run and confirm that the local fallback keeps the car under control.

Save a detailed trace with:

```bash
TORCS_BOT_TRACE=bot_run.jsonl python3 ai_bot.py --bot --granite
```

See [the bot evaluation guide](evaluation/bot/README.md) for more test templates.

## Testing the Project

Run the offline tests before a live demonstration:

```bash
source .venv/bin/activate
bash tools/run_tests.sh
```

Add `--service` when you also want the runtime smoke checks with a temporary middleware process:

```bash
bash tools/run_tests.sh --service
```

For focused checks, run the main pieces directly:

```bash
source .venv/bin/activate

# Core protocol and controller checks
python3 ai_bot.py

# Unit and integration tests for all features
python -m pytest tests/unit tests/integration -q

# Dedicated bot tests
python -m pytest tests/bot -q
```

End-to-end testing still requires a real TORCS session and a running Granite model.

## Suggested Student Project Process

For any of the four features:

1. Run the existing feature and save a baseline result.
2. Choose one clear problem or improvement.
3. Change one prompt, rule, telemetry field, or control parameter.
4. Repeat the same race scenario.
5. Compare results and explain what changed.
6. Document limitations, especially model latency and missing telemetry.

A short final demo should show the live TORCS data, Granite input and output, the student's modification, evidence of improvement, and safe behaviour when the model is unavailable.

## Troubleshooting

| Problem | Check |
| --- | --- |
| Dashboard values do not change | Confirm TORCS is racing and UDP telemetry is sent to port `3101` |
| Granite requests fail | Check the LM API Base URL, API key, model ID, and account access |
| Commentary never appears | Enable event/hybrid mode and confirm telemetry events are being detected |
| Coach page has no history | Drive for several seconds and confirm `/api/telemetry/history` contains frames |
| TORCS stays on `Initializing Driver...` | Start `ai_bot.py --bot`; check that `scr_server 1` uses port `3001` |
| `ai_bot.py` prints tests and exits | Add the required `--bot` option |
| TORCS has sound but no window in WSL2 | See [WSLg black-screen recovery](docs/wslg-black-screen-recovery.md) |

More detailed TORCS and dashboard instructions are available in [the full-stack startup guide](docs/full-stack-e2e-startup.md).

## Appendix — Optional Local Granite with LM Studio

[LM Studio](https://lmstudio.ai/) can replace the remote LM API when students want to run Granite locally and have suitable hardware. It is optional and is not required by the project architecture.

1. Download and load an IBM Granite instruct/chat model in LM Studio.
2. Open **Developer / Local Server** and start the OpenAI-compatible server.
3. Copy the model ID displayed by LM Studio.
4. Configure the middleware with the local endpoint:

```bash
curl -X POST http://127.0.0.1:8880/api/config/api \
  -H "Content-Type: application/json" \
  -d '{
    "base_url":"http://127.0.0.1:1234/v1",
    "api_key":"",
    "model":"YOUR_LOCAL_GRANITE_MODEL_ID"
  }'
```

If LM Studio runs on Windows while the project runs in WSL2, replace `127.0.0.1` with the Windows host IP reported by `ip route | grep default` and allow local-network access in LM Studio.

The local connection can also be checked with:

```bash
python lmstudio_smoke_test.py
```

See [the local TORCS + Granite quickstart](docs/torcs-granite-quickstart.md) for detailed LM Studio and WSL2 instructions.

## License

TORCS and the project code use the licenses included in this repository. Some car assets under `data/cars/models/` have separate terms; review their local license or readme files before redistribution.
