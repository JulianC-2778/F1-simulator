"""Unit tests for midware/services/process_manager.py -- the generic
subprocess registry backing the unified web dashboard's "start/stop the AI
bot" card (built together on kenny's `sq` branch, commit df6fea1).

Unlike car_state_source.py/voice_input.py, there's no external service to
mock here -- ManagedProcess's whole job *is* running a real local
subprocess, so these tests spawn small, fast, self-contained `python -c`
scripts (via sys.executable) rather than TORCS/ai_bot.py/etc. Nothing here
touches the network or any project-specific process.
"""

import sys
import time
import unittest
from pathlib import Path

from midware.services.process_manager import ManagedProcess, ProcessRegistry

TESTS_DIR = Path(__file__).resolve().parent


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class ManagedProcessTests(unittest.TestCase):
    def test_start_reports_running_then_stop_terminates_it_quickly(self):
        proc = ManagedProcess(
            key="sleepy",
            label="sleepy test process",
            cmd=[sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=TESTS_DIR,
        )
        self.assertIsNone(proc.start())
        self.assertTrue(proc.is_running())

        started = time.monotonic()
        proc.stop()
        elapsed = time.monotonic() - started

        self.assertFalse(proc.is_running())
        # It was killed, not waited out -- well under the 5s sleep duration.
        self.assertLess(elapsed, 3.0)

    def test_start_is_a_no_op_when_already_running(self):
        proc = ManagedProcess(
            key="sleepy",
            label="sleepy test process",
            cmd=[sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=TESTS_DIR,
        )
        proc.start()
        first_pid = proc.proc.pid
        self.assertIsNone(proc.start())  # no error, and no second process spawned
        self.assertEqual(proc.proc.pid, first_pid)
        proc.stop()

    def test_start_returns_an_error_message_for_a_missing_cwd(self):
        proc = ManagedProcess(
            key="bad-cwd",
            label="bad cwd test process",
            cmd=[sys.executable, "-c", "print('hi')"],
            cwd=TESTS_DIR / "this_directory_does_not_exist",
        )
        error = proc.start()
        self.assertIsNotNone(error)
        self.assertIn("does not exist", error)
        self.assertFalse(proc.is_running())

    def test_tail_captures_stdout_lines_and_the_exit_marker(self):
        proc = ManagedProcess(
            key="printer",
            label="printer test process",
            cmd=[sys.executable, "-c", "print('line one'); print('line two')"],
            cwd=TESTS_DIR,
        )
        proc.start()
        # Wait for the reader thread's own "exited" marker, not just
        # is_running() -- the background thread can still be draining
        # stdout for a moment after the OS reports the process as gone.
        self.assertTrue(_wait_until(lambda: any("exited" in line for line in proc.tail(50))))

        tail = proc.tail(50)
        joined = "\n".join(tail)
        self.assertIn("line one", joined)
        self.assertIn("line two", joined)

    def test_status_reflects_running_state_and_pid(self):
        proc = ManagedProcess(
            key="sleepy",
            label="sleepy test process",
            cmd=[sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=TESTS_DIR,
        )
        proc.start()
        status = proc.status()
        self.assertTrue(status["running"])
        self.assertEqual(status["pid"], proc.proc.pid)
        proc.stop()

        status_after_stop = proc.status()
        self.assertFalse(status_after_stop["running"])
        self.assertIsNone(status_after_stop["pid"])


class ProcessRegistryTests(unittest.TestCase):
    def test_register_then_get_returns_the_same_managed_process(self):
        registry = ProcessRegistry()
        registered = registry.register(
            "ai_bot",
            "AI Driver Bot",
            [sys.executable, "-c", "pass"],
            TESTS_DIR,
        )
        self.assertIs(registry.get("ai_bot"), registered)

    def test_get_returns_none_for_an_unknown_key(self):
        registry = ProcessRegistry()
        self.assertIsNone(registry.get("does_not_exist"))


if __name__ == "__main__":
    unittest.main()
