# Kokoro TTS Server Setup

Local text-to-speech service for the F1 simulator commentary system.
Runs as a standalone FastAPI server on port 8881, returning WAV audio from text input.

---

## Prerequisites

- **Python 3.10–3.12.** `kokoro` declares `Requires-Python <3.13`, so it
  cannot be installed into a 3.13+ interpreter at all — pip will report
  "No matching distribution found for kokoro" and list only the ancient
  0.7.x releases as candidates.
- Internet access for the first-time model download (~350 MB)

---

## Setup Steps

### 1. Get a Python 3.10–3.12 environment

The TTS server is a standalone process on port 8881 — it does **not** need
to share midware's virtualenv, which is convenient because midware may be
running on a newer Python than kokoro supports.

If a suitable interpreter already exists, a plain `python3.12 -m venv
.venv-tts` is fine. If the machine only has 3.13+ (the WSL box used for
work package C only had python3.14, with no `sudo` and no `python3.12` in
apt), use `uv`, which installs a standalone CPython without root:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # installs to ~/.local/bin
cd ~/F1-simulator
~/.local/bin/uv venv --python 3.12 .venv-tts
```

`.venv-tts/` is already covered by git (uv writes its own `.gitignore`).

### 2. Install dependencies

Install torch first, choosing the build to match your hardware — see
"GPU acceleration" below before picking, since GPU is ~10× faster and
`tts_server.py` uses it automatically when torch can see it:

```bash
UV="$HOME/.local/bin/uv"          # or just use .venv-tts/bin/pip

# With an NVIDIA GPU (see the driver caveat below for why cu118/2.7.1):
$UV pip install --python .venv-tts/bin/python \
  --index-url https://download.pytorch.org/whl/cu118 "torch==2.7.1+cu118"

# No GPU — CPU-only build, avoids pulling several GB of unused CUDA libs:
$UV pip install --python .venv-tts/bin/python \
  --index-url https://download.pytorch.org/whl/cpu torch

# Then, either way:
$UV pip install --python .venv-tts/bin/python \
  kokoro soundfile huggingface_hub numpy fastapi uvicorn pydantic
```

### 2b. Install the spaCy English model (easy to miss)

kokoro's G2P frontend (`misaki`) calls `spacy.load("en_core_web_sm")`,
which is **not** a dependency of any of the packages above. Without it the
server dies during startup with:

```
OSError: [E050] Can't find model 'en_core_web_sm'.
ERROR:    Application startup failed. Exiting.
```

misaki tries to self-install it on that first failure and prints
"Download and installation successful" — **do not trust that message**. If
`uv` is on PATH, spaCy's downloader shells out to `uv pip install` without
a `--python` flag and lands the model in a different environment; the next
startup fails identically. Install the wheel explicitly instead:

```bash
$UV pip install --python .venv-tts/bin/python \
  "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
.venv-tts/bin/python -c "import spacy; spacy.load('en_core_web_sm'); print('spacy model OK')"
```

(Match the wheel's major.minor to the installed spaCy — 3.8.x model for
spaCy 3.8.x.)

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
.venv-tts/bin/python tts_server.py
```

Expected output (model load takes ~10 s on CPU):
```
[INFO] Kokoro model loaded on cpu.
[INFO] TTS server ready → http://127.0.0.1:8881
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8881 (Press CTRL+C to quit)
```

To run it detached from a WSL shell that will be closed, `nohup … &` is
**not** enough — WSL tears the whole session down and takes the server with
it. Use `setsid`:

```bash
cd ~/F1-simulator
setsid nohup .venv-tts/bin/python tts_server.py > /tmp/tts_server.log 2>&1 < /dev/null &
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

For race commentary, `bm_lewis` is recommended (broadcaster-style delivery).

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
| 1 — Midware | `cd ~/F1-simulator && source .venv/bin/activate && python3 -m midware.app` | 8880 |
| 2 — TTS | `cd ~/F1-simulator && .venv-tts/bin/python tts_server.py` | 8881 |
| 3 — TORCS | `~/F1-simulator/BUILD/bin/torcs` | — |

Open `http://127.0.0.1:8880` in a browser to see commentary captions and hear
the synthesized audio play back -- the Electron overlay is not needed for
this. It only renders Feature 1's (AI Racing Engineer) floating caption
window, and is started separately with `cd overlay-app && npm start` if you
want that too.

---

## GPU acceleration (worth doing — ~10× faster)

`tts_server.py` already picks the device automatically
([tts_server.py:77](../tts_server.py#L77)) — there is nothing to configure.
The only question is whether the installed torch build can see the GPU.

Measured on the WSL box used for work package C (GTX 1650, 4 GB), same
66-character commentary line producing 4.2 s of audio:

| torch build | synthesis wall time | note |
|---|---:|---|
| `2.13.0+cpu` | **2.07 s** | RTF ≈ 0.5 — audible lag |
| `2.7.1+cu118` | **0.21–0.27 s** | RTF ≈ 0.05 |

### Old-driver caveat (how the cu118 pin came about)

That box's NVIDIA driver is **517.00 (2022), which caps out at CUDA 11.7**.
Current torch releases only ship cu126/cu128+ wheels, all of which need a
525+ driver and fail with "CUDA driver version is insufficient". Two ways
out:

1. **Pin to CUDA 11.8 torch** (what was done — no driver change, no admin
   rights). CUDA 11.x minor-version compatibility lets a cu118 build run on
   an 11.7 driver, verified working. cu118 wheels for cp312 stop at
   **torch 2.7.1**, so that is the ceiling:
   ```bash
   $UV pip install --python .venv-tts/bin/python \
     --index-url https://download.pytorch.org/whl/cu118 "torch==2.7.1+cu118"
   .venv-tts/bin/python -c "import torch; print(torch.cuda.is_available())"
   ```
2. **Update the Windows NVIDIA driver** (WSL uses the host driver, not a
   Linux one — do *not* install a Linux driver inside WSL). Turing cards
   like the 1650 are still supported by current branches; afterwards plain
   `pip install torch` works and the version pin can be dropped.

### Two things this changes for the latency test

- **The first synthesis after startup costs ~8.9 s** (CUDA context + kernel
  warmup), against ~0.22 s for every call after it. `tts_server.py` does not
  warm up at startup, so a work-package-C run must either fire one throwaway
  `POST /tts` before collecting data, or expect the first t5 sample to be a
  ~9 s outlier. Do not silently drop it — warm up beforehand instead.
- **VRAM sits at ~3.1 GB of 4 GB** with the model resident (~2.1 GB above
  the idle desktop baseline). That leaves under 1 GB of headroom, so if
  TORCS is ever given real GPU acceleration on the same card, check for
  contention.

`tts_server.py` returns the whole WAV in one response, so playback cannot
start until synthesis finishes: t5 ≈ t3 + synthesis time. On GPU that is a
~0.2 s offset rather than the ~2 s it would be on CPU.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'kokoro'`**
→ Wrong interpreter. Use `.venv-tts/bin/python`, not midware's venv or the
system python.

**`ERROR: Could not find a version that satisfies the requirement kokoro`**
→ Your Python is 3.13 or newer. See Prerequisites — kokoro requires <3.13.

**`OSError: [E050] Can't find model 'en_core_web_sm'`**
→ See step 2b; install the model wheel explicitly with an explicit
`--python` pointing at `.venv-tts`.

**`FileNotFoundError: kokoro-v1_0.pth`**
→ Model not downloaded. Re-run Step 3.

**`Voice 'xxx' not downloaded`**
→ The requested voice `.pt` file is missing from `~/F1-simulator/voices/`. Download it via `hf_hub_download('hexgrad/Kokoro-82M', 'voices/xxx.pt', local_dir='.')`.

**`aplay: no soundcards found`** (WSL2)
→ Use `ffplay output.wav` instead, or configure WSLg audio.
