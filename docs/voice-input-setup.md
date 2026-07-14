# Engineer Chatbot Voice Input Setup

Speech-to-text question input for Feature 1 (AI Racing Engineer Chatbot).
Lets you ask the engineer a question by voice instead of typing, in both
`chat_engineer.py` (CLI) and `chat_engineer_gui.py` (desktop GUI). Runs
entirely locally (no cloud API, no account needed) via
[faster-whisper](https://github.com/SYSTRAN/faster-whisper).

Voice **output** (the engineer's answer being read aloud) is a separate,
already-existing feature — the shared overlay voice settings / TTS pipeline.
This document only covers the **input** side: turning your spoken question
into text.

English-only by design: `ENGINEER_PERSONA` always answers in English, so
this uses an English-only Whisper model (`base.en` by default). Chinese or
other languages are not supported for voice input.

---

## Prerequisites

- Python 3.10+
- Internet access for the first-time model download (tens of MB, cached
  locally afterward — works offline after that)
- WSL2 users: microphone access depends on WSLg's PulseAudio bridge (see
  "How microphone capture works under WSL" below) — this has been flaky in
  practice, see Troubleshooting.

---

## Setup Steps

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y ffmpeg alsa-utils pulseaudio-utils
```

- `ffmpeg` — required by faster-whisper to decode audio.
- `pulseaudio-utils` — provides `parecord`/`pactl`, used to record from the
  microphone.
- `alsa-utils` — optional, only useful for diagnosing audio devices
  (`arecord -l`); not part of the actual recording path.

### 2. Install the Python package

```bash
pip3 install faster-whisper --break-system-packages
```

### 3. Verify the microphone is reachable (WSL2 only)

```bash
pactl list sources short
```

You should see a source named `RDPSource` (this is WSLg's bridge to the
Windows host's default microphone). If it's missing, or a real recording
test below produces 0 frames, see Troubleshooting.

Record a 3-second test clip and confirm it actually captured sound:

```bash
timeout 3 parecord --device=RDPSource /tmp/mic_test.wav
python3 -c "
import wave, audioop
w = wave.open('/tmp/mic_test.wav', 'rb')
frames = w.readframes(w.getnframes())
print('length_sec:', w.getnframes() / w.getframerate())
print('rms:', audioop.rms(frames, w.getsampwidth()))
"
```

Speak during the 3 seconds. `rms` in the low hundreds/thousands (not near
0) with `length_sec` around 3 confirms the microphone path works.

### 4. Run it

CLI:

```bash
cd ~/F1-simulator
python3 chat_engineer.py
```

At the question prompt, type `v` and press Enter to start recording, speak
your question in English, then press Enter again to stop. The recognized
text is shown and fed into the same question pipeline as typed input.

GUI:

```bash
cd ~/F1-simulator
python3 chat_engineer_gui.py
```

Click the 🎤 button to start recording, click it again to stop. The
recognized text is filled into the input box (not sent automatically) so
you can review or edit it before pressing Enter / clicking "发送".

The first time either script actually transcribes something, faster-whisper
downloads the `base.en` model from Hugging Face automatically (needs
internet once; cached afterward).

---

## How microphone capture works under WSL

There's no direct ALSA hardware device inside WSL2 (`arecord -l` reports
"no soundcards found" — that's expected, not an error). Instead, WSLg
bridges the Windows host's microphone into WSL as a PulseAudio source
named `RDPSource` (`module-rdp-source`). `voice_input.py` records from this
source via `parecord`, which is the same mechanism used to verify things
in Step 3 above.

---

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `TORCS_ENGINEER_VOICE_MODEL` | `base.en` | faster-whisper model name. Larger models (`small.en`, `medium.en`) are more accurate but slower — try one of these if `base.en`'s accuracy isn't good enough. |
| `TORCS_ENGINEER_VOICE_DEVICE` | `RDPSource` | PulseAudio source name to record from. Only needs changing on a non-WSL setup with a different microphone source name. |

---

## Troubleshooting

**Recognized text is wrong/garbled**
→ `base.en` is a small model; speaking slower and more clearly helps. If
accuracy is still not good enough, try a bigger model:
```bash
export TORCS_ENGINEER_VOICE_MODEL=small.en
```

**"没有识别到内容" / no speech recognized, every time**
→ Check `pactl list sources short` still lists `RDPSource`, and re-run the
Step 3 recording test. If that test now also produces `length_sec: 0.0` /
`rms: 0`, this isn't a code problem — see the next item.

**Recording worked once, then stopped working (0 frames every time after)**
→ Observed in practice: WSLg's RDP audio bridge can get into a bad state
(the same general flakiness documented elsewhere for WSLg's GPU/audio
playback issues). The fix that worked: fully restart WSL, not just the
script.
1. In a **Windows** PowerShell/cmd window (not inside WSL): `wsl.exe --shutdown`
2. Reopen your WSL terminal (this kills every other WSL process too —
   TORCS, midware, overlay-app, etc. all need restarting afterward).
3. Re-run the Step 3 recording test to confirm it's fixed before moving on.

Note: this also drops VS Code's Remote-WSL connection if you're using it —
click "Reload Window" (more reliable than "Reconnect Now") to get it back.

**`ModuleNotFoundError: No module named 'faster_whisper'`**
→ Re-run Step 2. If using a pip that's "externally managed", make sure to
include `--break-system-packages`.

**Slow first response / "unauthenticated requests to the HF Hub" warning**
→ Harmless — just means the one-time model download wasn't using a
Hugging Face account token. Ignore unless downloads are failing outright.

**`RDPSource` not listed in `pactl list sources short` at all**
→ This is specific to WSLg's audio bridge; if it's never present, WSLg's
audio passthrough may not be set up correctly for this Windows/WSL version
combination. Restarting WSL (see above) is the first thing to try.
