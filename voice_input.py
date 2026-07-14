#!/usr/bin/env python3
"""
voice_input.py — Feature 1 (AI Racing Engineer Chatbot) speech-to-text input.

Records microphone audio via PulseAudio's `parecord` and transcribes it
locally with faster-whisper. Under WSL/WSLg, the Windows host's microphone
is exposed to Pulse as a source named "RDPSource" (module-rdp-source) --
confirmed working in this project's environment by recording a real signal
(rms ~8000+ on speech vs a near-zero noise floor). See
docs/voice-input-setup.md for the full setup/verification steps.

English-only by design: chat_engineer.py's ENGINEER_PERSONA always answers
in English, so an English-only Whisper model (e.g. "base.en") is used --
it's a bit faster and a bit more accurate than the multilingual equivalent
for this use case. Chinese or other non-English speech is not supported
here; TORCS_ENGINEER_VOICE_MODEL can be overridden to a multilingual model
name if that changes later, but the persona/prompt would need updating too.

Design goals (mirrors overlay_broadcast.py's conventions in this project):
  - Never raise out of transcribe()/record_and_transcribe_blocking() --
    on any failure (no mic, parecord missing, empty recording, model
    load failure, ...), return "" so callers can treat it the same as
    "no question asked" instead of crashing the chat loop.
  - No hard dependency: if `faster-whisper` is not installed, voice input
    is simply unavailable (raises a clear, caught error with instructions)
    rather than breaking typed-question usage, which must keep working
    exactly as before regardless of whether voice input is set up.

Env vars:
    TORCS_ENGINEER_VOICE_MODEL   - faster-whisper model name (default: "base.en")
    TORCS_ENGINEER_VOICE_DEVICE  - PulseAudio source name to record from
                                    (default: "RDPSource", WSLg's mic bridge)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time

MODEL_NAME = os.getenv("TORCS_ENGINEER_VOICE_MODEL", "base.en")
PULSE_SOURCE = os.getenv("TORCS_ENGINEER_VOICE_DEVICE", "RDPSource")

_model = None
_model_load_error: str | None = None


def _get_model():
    """Lazily load the faster-whisper model (first call may download weights)."""
    global _model, _model_load_error
    if _model is not None:
        return _model
    if _model_load_error is not None:
        raise RuntimeError(_model_load_error)
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        _model_load_error = (
            "faster-whisper is not installed. Run: "
            "pip3 install faster-whisper --break-system-packages"
        )
        raise RuntimeError(_model_load_error) from exc

    try:
        _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
    except Exception as exc:
        _model_load_error = f"Failed to load Whisper model '{MODEL_NAME}': {exc}"
        raise RuntimeError(_model_load_error) from exc
    return _model


def mic_available() -> bool:
    """Best-effort check that the tools this module needs are present.

    Does not guarantee the microphone itself works (only a real recording
    can confirm that) -- just that `parecord` exists and the configured
    Pulse source is visible, so callers can give a clear error message
    up front instead of failing deep inside a recording attempt.
    """
    if shutil.which("parecord") is None:
        return False
    try:
        result = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except Exception:
        return True  # can't check; don't block usage on this alone
    return PULSE_SOURCE in result.stdout


class Recorder:
    """Wraps a `parecord` subprocess recording to a temp WAV file.

    Usage:
        rec = Recorder()
        rec.start()
        ... wait for the user's stop signal ...
        wav_path = rec.stop()
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._path: str | None = None

    def start(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="engineer_voice_")
        os.close(fd)
        self._path = path
        self._process = subprocess.Popen(
            ["parecord", f"--device={PULSE_SOURCE}", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self, min_seconds: float = 0.3) -> str | None:
        """Stop recording and return the WAV file path, or None on failure."""
        if self._process is None or self._path is None:
            return None
        # Give parecord a brief moment to have actually captured something
        # before asking it to stop, so a near-instant stop doesn't produce
        # a truncated/unreadable file.
        time.sleep(min_seconds)
        self._process.terminate()
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.kill()
        path = self._path
        self._process = None
        self._path = None
        return path

    def cancel(self) -> None:
        """Abort a recording in progress without transcribing it."""
        if self._process is not None:
            self._process.kill()
        if self._path and os.path.exists(self._path):
            try:
                os.remove(self._path)
            except OSError:
                pass
        self._process = None
        self._path = None


def transcribe(wav_path: str) -> str:
    """Transcribe a WAV file to English text using faster-whisper.

    Returns "" (never raises) if transcription fails or the recording had
    no detectable speech -- callers should treat that the same as "no
    question asked".
    """
    try:
        model = _get_model()
        segments, _info = model.transcribe(wav_path, language="en")
        text = " ".join(segment.text.strip() for segment in segments).strip()
    except Exception as exc:
        print(f"[VoiceInput] Transcription failed: {exc}")
        text = ""

    if text:
        try:
            os.remove(wav_path)
        except OSError:
            pass
    else:
        # Keep the file when nothing was recognized -- otherwise there's no
        # way to tell "recording was silent/too short" apart from "Whisper
        # just failed on real speech" after the fact.
        print(f"[VoiceInput] No speech recognized. Recording kept at: {wav_path}")

    return text


def record_and_transcribe_blocking(stop_prompt: str = "") -> str:
    """CLI helper: start recording, block until Enter is pressed, then transcribe.

    Returns the recognized text (may be "" if nothing was understood or
    voice input isn't set up -- prints a clear reason either way).
    """
    if not mic_available():
        print(
            "[VoiceInput] Microphone not available (parecord missing or "
            f"'{PULSE_SOURCE}' source not found). See docs/voice-input-setup.md."
        )
        return ""

    recorder = Recorder()
    recorder.start()
    print("[VoiceInput] Recording... press Enter to stop.")
    try:
        input(stop_prompt)
    except (KeyboardInterrupt, EOFError):
        pass
    wav_path = recorder.stop()
    if not wav_path:
        return ""
    print("[VoiceInput] Transcribing...")
    return transcribe(wav_path)
