# Kokoro TTS Server Setup

Local text-to-speech service for the F1 simulator commentary system.
Runs as a standalone FastAPI server on port 8881, returning WAV audio from text input.

---

## Prerequisites

- Python 3.10+
- The midware virtual environment at `~/F1-simulator/midware/.venv`
- Internet access for the first-time model download (~350 MB)

---

## Setup Steps

### 1. Activate the virtual environment

```bash
source ~/F1-simulator/midware/.venv/bin/activate
```

### 2. Install dependencies

```bash
pip install kokoro soundfile huggingface_hub
```

`fastapi`, `uvicorn`, `numpy` are already installed from the midware setup.

### 3. Download the model and voice files

Run from the project root (`~/F1-simulator`):

```bash
python -c "
from huggingface_hub import hf_hub_download
hf_hub_download('hexgrad/Kokoro-82M', 'kokoro-v1_0.pth', local_dir='.')
hf_hub_download('hexgrad/Kokoro-82M', 'voices/af_heart.pt', local_dir='.')
hf_hub_download('hexgrad/Kokoro-82M', 'voices/bm_lewis.pt', local_dir='.')
hf_hub_download('hexgrad/Kokoro-82M', 'voices/bm_george.pt', local_dir='.')
"
```

Files will be placed at:
```
~/F1-simulator/
├── kokoro-v1_0.pth       ← model weights (~350 MB)
└── voices/
    ├── af_heart.pt        ← American female (warm)
    ├── bm_lewis.pt        ← British male (broadcaster)
    └── bm_george.pt       ← British male
```

To download more voices, add lines with other voice names. Full voice list: `GET /voices`.

### 4. Start the server

```bash
cd ~/F1-simulator
python tts_server.py
```

Expected output:
```
[INFO] Kokoro model loaded.
[INFO] TTS server ready → http://localhost:8881
INFO:     Uvicorn running on http://0.0.0.0:8881 (Press CTRL+C to quit)
```

---

## API Reference

### POST /tts

Generate speech and return WAV audio.

**Request body (JSON):**

| Field   | Type   | Default      | Description                        |
|---------|--------|--------------|------------------------------------|
| `text`  | string | required     | Text to synthesize                 |
| `voice` | string | `af_heart`   | Voice ID (must be downloaded)      |
| `speed` | float  | `1.2`        | Speech rate (0.5 = slow, 2.0 = fast) |
| `lang`  | string | `en-us`      | `en-us` or `en-gb`                |

**Response:** `audio/wav` binary

**Example:**
```bash
curl -X POST http://localhost:8881/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Hamilton takes the lead!", "voice": "bm_lewis", "speed": 1.2}' \
  --output output.wav
```

---

### GET /voices

List all supported voices and which ones are downloaded.

```bash
curl http://localhost:8881/voices
```

Available voices:

| ID           | Language | Description              |
|--------------|----------|--------------------------|
| `af_heart`   | en-us    | American female (warm)   |
| `af_bella`   | en-us    | American female (bright) |
| `af_sarah`   | en-us    | American female (clear)  |
| `am_adam`    | en-us    | American male            |
| `am_michael` | en-us    | American male (deep)     |
| `bf_emma`    | en-gb    | British female           |
| `bm_george`  | en-gb    | British male             |
| `bm_lewis`   | en-gb    | British male (broadcaster) |

For F1 commentary, `bm_lewis` is recommended (closest to Sky Sports style).

---

### GET /health

```bash
curl http://localhost:8881/health
```

```json
{"ok": true, "model_loaded": true}
```

---

## Play Audio (WSL2)

```bash
# Option 1
aplay output.wav

# Option 2
ffplay output.wav

# One-liner: generate and play immediately
curl -s -X POST http://localhost:8881/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "What a move from the championship leader!", "voice": "bm_lewis"}' \
  --output - | aplay
```

---

## Starting the Full System

Open three terminals:

| Terminal | Command | Port |
|----------|---------|------|
| 1 — Midware | `cd ~/F1-simulator/midware && source .venv/bin/activate && python commentary.py` | 8880 |
| 2 — TTS | `cd ~/F1-simulator && source midware/.venv/bin/activate && python tts_server.py` | 8881 |
| 3 — TORCS | `~/F1-simulator/BUILD/bin/torcs` | — |

The Electron overlay is started separately with `cd overlay-app && npm start`.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'kokoro'`**
→ venv not activated. Run `source ~/F1-simulator/midware/.venv/bin/activate` first.

**`FileNotFoundError: kokoro-v1_0.pth`**
→ Model not downloaded. Re-run Step 3.

**`Voice 'xxx' not downloaded`**
→ The requested voice `.pt` file is missing from `~/F1-simulator/voices/`. Download it via `hf_hub_download('hexgrad/Kokoro-82M', 'voices/xxx.pt', local_dir='.')`.

**`aplay: no soundcards found`** (WSL2)
→ Use `ffplay output.wav` instead, or configure WSLg audio.
