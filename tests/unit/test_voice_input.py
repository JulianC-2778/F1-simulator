#!/usr/bin/env python3
"""Unit tests for voice_input.py (Direction 1 / Feature 1's speech-to-text input).

None of these need a real microphone, PulseAudio, or faster-whisper
installed -- subprocess and the whisper model are mocked out, matching the
module's own design goal of degrading gracefully rather than needing that
hardware/dependency to be present.
"""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

import voice_input


class MicAvailableTests(unittest.TestCase):
    def test_returns_false_when_parecord_is_not_installed(self):
        with patch("voice_input.shutil.which", return_value=None):
            self.assertFalse(voice_input.mic_available())

    def test_returns_true_when_parecord_present_and_source_listed(self):
        fake_result = MagicMock(stdout=f"0\t{voice_input.PULSE_SOURCE}\tmodule-rdp-source\n")
        with patch("voice_input.shutil.which", return_value="/usr/bin/parecord"), \
             patch("voice_input.subprocess.run", return_value=fake_result):
            self.assertTrue(voice_input.mic_available())

    def test_returns_false_when_parecord_present_but_source_not_listed(self):
        fake_result = MagicMock(stdout="0\tsome-other-source\tmodule-foo\n")
        with patch("voice_input.shutil.which", return_value="/usr/bin/parecord"), \
             patch("voice_input.subprocess.run", return_value=fake_result):
            self.assertFalse(voice_input.mic_available())

    def test_does_not_block_usage_when_pactl_check_itself_fails(self):
        # "can't check; don't block usage on this alone" -- see mic_available()'s
        # own comment for why this deliberately fails open, not closed.
        with patch("voice_input.shutil.which", return_value="/usr/bin/parecord"), \
             patch("voice_input.subprocess.run", side_effect=OSError("pactl missing")):
            self.assertTrue(voice_input.mic_available())


class RecorderTests(unittest.TestCase):
    def test_start_launches_parecord_against_the_configured_source(self):
        fake_process = MagicMock()
        with patch("voice_input.subprocess.Popen", return_value=fake_process) as popen:
            recorder = voice_input.Recorder()
            recorder.start()
        args = popen.call_args.args[0]
        self.assertEqual(args[0], "parecord")
        self.assertIn(f"--device={voice_input.PULSE_SOURCE}", args)
        self.assertTrue(recorder._path.endswith(".wav"))

    def test_stop_terminates_the_process_and_returns_the_wav_path(self):
        fake_process = MagicMock()
        with patch("voice_input.subprocess.Popen", return_value=fake_process):
            recorder = voice_input.Recorder()
            recorder.start()
        path = recorder.stop(min_seconds=0)
        self.assertTrue(path)
        fake_process.terminate.assert_called_once()
        fake_process.wait.assert_called_once()
        # Internal state must reset so a second start()/stop() cycle works.
        self.assertIsNone(recorder._process)
        self.assertIsNone(recorder._path)

    def test_stop_kills_the_process_if_it_does_not_exit_in_time(self):
        fake_process = MagicMock()
        fake_process.wait.side_effect = subprocess.TimeoutExpired(cmd="parecord", timeout=3)
        with patch("voice_input.subprocess.Popen", return_value=fake_process):
            recorder = voice_input.Recorder()
            recorder.start()
        recorder.stop(min_seconds=0)
        fake_process.kill.assert_called_once()

    def test_stop_without_a_prior_start_returns_none(self):
        recorder = voice_input.Recorder()
        self.assertIsNone(recorder.stop(min_seconds=0))

    def test_cancel_kills_the_process_and_removes_the_temp_file(self):
        fake_process = MagicMock()
        with patch("voice_input.subprocess.Popen", return_value=fake_process):
            recorder = voice_input.Recorder()
            recorder.start()
        path = recorder._path
        with patch("voice_input.os.path.exists", return_value=True), \
             patch("voice_input.os.remove") as remove:
            recorder.cancel()
        fake_process.kill.assert_called_once()
        remove.assert_called_once_with(path)
        self.assertIsNone(recorder._process)
        self.assertIsNone(recorder._path)

    def test_cancel_without_a_prior_start_does_not_raise(self):
        recorder = voice_input.Recorder()
        recorder.cancel()  # must be a harmless no-op


class TranscribeTests(unittest.TestCase):
    def _fake_model(self, texts: list[str]):
        segments = [MagicMock(text=t) for t in texts]
        model = MagicMock()
        model.transcribe.return_value = (segments, MagicMock())
        return model

    def test_joins_and_strips_segment_text_then_removes_the_file(self):
        model = self._fake_model([" should I push ", "now? "])
        with patch("voice_input._get_model", return_value=model), \
             patch("voice_input.os.remove") as remove:
            text = voice_input.transcribe("/tmp/fake.wav")
        self.assertEqual(text, "should I push now?")
        remove.assert_called_once_with("/tmp/fake.wav")

    def test_keeps_the_file_when_no_speech_is_recognized(self):
        model = self._fake_model([])
        with patch("voice_input._get_model", return_value=model), \
             patch("voice_input.os.remove") as remove:
            text = voice_input.transcribe("/tmp/fake.wav")
        self.assertEqual(text, "")
        remove.assert_not_called()

    def test_never_raises_when_the_model_fails_to_load(self):
        with patch("voice_input._get_model", side_effect=RuntimeError("faster-whisper is not installed")), \
             patch("voice_input.os.remove") as remove:
            text = voice_input.transcribe("/tmp/fake.wav")
        self.assertEqual(text, "")
        remove.assert_not_called()

    def test_never_raises_when_transcription_itself_throws(self):
        model = MagicMock()
        model.transcribe.side_effect = RuntimeError("decoder error")
        with patch("voice_input._get_model", return_value=model):
            text = voice_input.transcribe("/tmp/fake.wav")
        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main()
