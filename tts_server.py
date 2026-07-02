"""
Kokoro TTS Server — local text-to-speech via kokoro (PyTorch)

Setup:
    pip install kokoro soundfile numpy fastapi uvicorn
    # Download model + voices (run once):
    python -c "
    from huggingface_hub import hf_hub_download
    hf_hub_download('hexgrad/Kokoro-82M', 'kokoro-v1_0.pth', local_dir='.')
    hf_hub_download('hexgrad/Kokoro-82M', 'voices/af_heart.pt', local_dir='.')
    hf_hub_download('hexgrad/Kokoro-82M', 'voices/bm_lewis.pt', local_dir='.')
    hf_hub_download('hexgrad/Kokoro-82M', 'voices/bm_george.pt', local_dir='.')
    "

Start:
    python tts_server.py
    # Listens on http://0.0.0.0:8881

Endpoints:
    POST /tts     — generate speech, returns WAV audio
    GET  /voices  — list available voices
    GET  /health  — health check
"""

import io
import logging
from pathlib import Path

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL_PATH   = Path(__file__).parent / "kokoro-v1_0.pth"
VOICES_DIR   = Path(__file__).parent / "voices"

DEFAULT_VOICE  = "af_heart"
DEFAULT_SPEED  = 1.2
SAMPLE_RATE    = 24000

# lang_code: 'a' = American English, 'b' = British English
LANG_MAP = {
    "en-us": "a",
    "en-gb": "b",
}

# ---------------------------------------------------------------------------
# Load model
# ---------------------------------------------------------------------------

pipeline = None

def load_model():
    global pipeline
    from kokoro import KPipeline
    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"Model not found: {MODEL_PATH}\n"
            "Run: python -c \"from huggingface_hub import hf_hub_download; "
            "hf_hub_download('hexgrad/Kokoro-82M', 'kokoro-v1_0.pth', local_dir='.')\""
        )
    log.info(f"Loading Kokoro model from {MODEL_PATH} ...")
    pipeline = KPipeline(lang_code="a", model=str(MODEL_PATH))
    log.info("Kokoro model loaded.")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Kokoro TTS Server")


@app.on_event("startup")
def startup():
    load_model()
    log.info("TTS server ready → http://localhost:8881")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TTSRequest(BaseModel):
    text:  str
    voice: str   = DEFAULT_VOICE
    speed: float = DEFAULT_SPEED
    lang:  str   = "en-us"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True, "model_loaded": pipeline is not None}


@app.get("/voices")
def list_voices():
    available = []
    if VOICES_DIR.exists():
        available = [p.stem for p in sorted(VOICES_DIR.glob("*.pt"))]
    voices = [
        {"id": "af_heart",   "lang": "en-us", "desc": "American female (warm)"},
        {"id": "af_bella",   "lang": "en-us", "desc": "American female (bright)"},
        {"id": "af_sarah",   "lang": "en-us", "desc": "American female (clear)"},
        {"id": "am_adam",    "lang": "en-us", "desc": "American male"},
        {"id": "am_michael", "lang": "en-us", "desc": "American male (deep)"},
        {"id": "bf_emma",    "lang": "en-gb", "desc": "British female"},
        {"id": "bm_george",  "lang": "en-gb", "desc": "British male"},
        {"id": "bm_lewis",   "lang": "en-gb", "desc": "British male (broadcaster)"},
    ]
    return {"voices": voices, "downloaded": available, "default": DEFAULT_VOICE}


@app.post("/tts")
def synthesize(req: TTSRequest):
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text is empty")

    voice_path = VOICES_DIR / f"{req.voice}.pt"
    if not voice_path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Voice '{req.voice}' not downloaded. File expected at: {voice_path}"
        )

    log.info(f"TTS: voice={req.voice} speed={req.speed} chars={len(req.text)}")

    try:
        lang_code = LANG_MAP.get(req.lang, "a")
        audio_chunks = []
        for _, _, audio in pipeline(req.text, voice=str(voice_path), speed=req.speed, lang=lang_code):
            audio_chunks.append(audio)

        if not audio_chunks:
            raise RuntimeError("No audio generated")

        samples = np.concatenate(audio_chunks)
    except Exception as e:
        log.error(f"Synthesis failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    buf = io.BytesIO()
    sf.write(buf, samples, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    buf.seek(0)

    return Response(
        content=buf.read(),
        media_type="audio/wav",
        headers={"X-Sample-Rate": str(SAMPLE_RATE)},
    )


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8881, reload=False)
