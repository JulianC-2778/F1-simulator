"""Unit tests for the opt-in t0-t5 latency logger (midware/latency_log.py).
Confirms: disabled by default (zero writes, zero behaviour change), and
when enabled, writes exactly one t2_first_token row per request even if
record() is called on every streamed token (see runtime.py::call_ai's
on_token, which calls this unconditionally)."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from midware.latency_log import LatencyLog


class TestLatencyLogDisabledByDefault(unittest.TestCase):
    def test_disabled_instance_writes_nothing(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "latency.jsonl"
            log = LatencyLog(path=path, enabled=False)
            log.record("req-1", "t1_event_detected")
            log.record("req-1", "t2_first_token")
            log.record("req-1", "t3_ai_done")
            self.assertFalse(path.exists())

    def test_default_constructor_is_disabled_without_env_var(self):
        log = LatencyLog(path="/tmp/should-not-be-created-by-default.jsonl")
        self.assertFalse(log.enabled)


class TestLatencyLogEnabled(unittest.TestCase):
    def test_enabled_instance_writes_one_row_per_record_call(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "latency.jsonl"
            log = LatencyLog(path=path, enabled=True)
            log.record("req-1", "t1_event_detected")
            log.record("req-1", "t3_ai_done")
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["stage"], "t1_event_detected")
        self.assertEqual(rows[1]["stage"], "t3_ai_done")
        self.assertEqual(rows[0]["request_id"], "req-1")

    def test_first_token_is_deduplicated_across_the_whole_stream(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "latency.jsonl"
            log = LatencyLog(path=path, enabled=True)
            for _ in range(20):  # simulate 20 streamed tokens
                log.record("req-1", "t2_first_token")
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)

    def test_forget_allows_a_reused_request_id_to_log_first_token_again(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "latency.jsonl"
            log = LatencyLog(path=path, enabled=True)
            log.record("req-1", "t2_first_token")
            log.forget("req-1")
            log.record("req-1", "t2_first_token")
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 2)

    def test_empty_request_id_is_ignored(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "latency.jsonl"
            log = LatencyLog(path=path, enabled=True)
            log.record("", "t1_event_detected")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
