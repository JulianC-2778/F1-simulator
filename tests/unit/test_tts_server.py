#!/usr/bin/env python3
"""
Unit tests for tts_server.py (Direction 1's Kokoro TTS integration).

The real Kokoro/torch model is never loaded here: load_model() is stubbed
out and tts_server.pipelines is populated with fake pipeline callables, so
these tests run without GPU/model files and exercise the FastAPI route
logic (validation, error codes, WAV encoding, volume clamping) directly.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

import tts_server


class TtsServerTests(unittest.TestCase):
    def setUp(self):
        load_model_patch = patch.object(tts_server, "load_model", lambda: None)
        load_model_patch.start()
        self.addCleanup(load_model_patch.stop)

        pipelines_patch = patch.object(tts_server, "pipelines", {})
        pipelines_patch.start()
        self.addCleanup(pipelines_patch.stop)

        context = TestClient(tts_server.app)
        self.client = context.__enter__()
        self.addCleanup(lambda: context.__exit__(None, None, None))

    def test_health_reflects_whether_a_model_is_loaded(self):
        self.assertEqual(self.client.get("/health").json(), {"ok": True, "model_loaded": False})
        tts_server.pipelines["a"] = object()
        self.assertEqual(self.client.get("/health").json(), {"ok": True, "model_loaded": True})

    def test_voices_lists_full_catalog_with_default(self):
        payload = self.client.get("/voices").json()
        self.assertEqual(payload["default"], "af_heart")
        self.assertEqual(
            {v["id"] for v in payload["voices"]},
            {
                "af_heart", "af_bella", "af_sarah", "am_adam", "am_michael",
                "bf_emma", "bm_george", "bm_lewis",
            },
        )
        self.assertIsInstance(payload["downloaded"], list)

    def test_synthesize_rejects_when_model_not_loaded(self):
        response = self.client.post("/tts", json={"text": "hello"})
        self.assertEqual(response.status_code, 503)

    def test_synthesize_rejects_empty_text(self):
        tts_server.pipelines["a"] = object()
        response = self.client.post("/tts", json={"text": "   "})
        self.assertEqual(response.status_code, 400)

    def test_synthesize_rejects_voice_not_downloaded(self):
        tts_server.pipelines["a"] = object()
        response = self.client.post("/tts", json={"text": "hello", "voice": "nonexistent_voice_xyz"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("not downloaded", response.json()["detail"])

    def test_synthesize_returns_wav_audio_and_clamps_volume(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            voices_dir = Path(tmp_dir)
            (voices_dir / "test_voice.pt").write_bytes(b"stub")
            fake_audio = np.array([2.0, -2.0, 0.5], dtype=np.float32)

            def fake_pipeline(text, voice, speed):
                self.assertEqual(text, "hello there")
                yield (None, None, fake_audio)

            with patch.object(tts_server, "VOICES_DIR", voices_dir):
                tts_server.pipelines["a"] = fake_pipeline
                response = self.client.post(
                    "/tts",
                    json={"text": "hello there", "voice": "test_voice", "volume": 3.0},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-sample-rate"], "24000")
        samples, sample_rate = sf.read(io.BytesIO(response.content))
        self.assertEqual(sample_rate, 24000)
        # volume=3.0 is clamped to 2.0 server-side, then samples are clipped to [-1, 1].
        np.testing.assert_allclose(samples, [1.0, -1.0, 1.0], atol=2e-3)


if __name__ == "__main__":
    unittest.main()
