"""Generic named-subprocess manager for the unified web dashboard.

The dashboard's "AI Driver Bot" card needs a way to start/stop the
`ai_bot.py` client process from a browser button instead of a terminal.
This is intentionally a small, generic registry (not hard-coded to bot
alone) so future dashboard cards can reuse it without a redesign.

This is a trimmed port of `launcher_gui.py`'s `ManagedProcess`: same
process-group ownership (`setsid`) so `stop()` can signal the whole group,
same bounded log tail fed by a background reader thread. `launcher_gui.py`
itself is untouched -- it remains a fully independent desktop control
panel; this module is a separate, HTTP-reachable copy of the same pattern
for `midware` to own.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


class ManagedProcess:
    """One subprocess this service owns: start / stop / tail its output."""

    def __init__(
        self,
        key: str,
        label: str,
        cmd: list[str],
        cwd: Path,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.cmd = cmd
        self.cwd = cwd
        self.extra_env = extra_env or {}
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.log: deque[str] = deque(maxlen=400)
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> str | None:
        """Returns an error message, or None on success. No-op if already running."""
        with self._lock:
            if self.is_running():
                return None
            if not self.cwd.exists():
                return f"working directory does not exist: {self.cwd}"
            env = dict(os.environ)
            env.update(self.extra_env)
            try:
                self.proc = subprocess.Popen(
                    self.cmd,
                    cwd=str(self.cwd),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None,
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                return f"failed to start: {exc}"
            self.started_at = time.time()
            self.log.clear()
            self.log.append(
                f"[process_manager] started pid={self.proc.pid}: "
                f"{' '.join(self.cmd)} (cwd={self.cwd})"
            )
            self._reader_thread = threading.Thread(target=self._pump_output, daemon=True)
            self._reader_thread.start()
            return None

    def _pump_output(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self.log.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        code = proc.poll()
        self.log.append(f"[process_manager] exited (code={code})")

    def stop(self) -> None:
        with self._lock:
            if not self.is_running():
                return
            proc = self.proc
            assert proc is not None
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                except (ProcessLookupError, PermissionError):
                    pass
            self.log.append("[process_manager] stopped")
            self.started_at = None

    def tail(self, n: int = 50) -> list[str]:
        return list(self.log)[-n:]

    def status(self) -> dict[str, Any]:
        running = self.is_running()
        return {
            "key": self.key,
            "label": self.label,
            "running": running,
            "pid": self.proc.pid if running and self.proc else None,
            "started_at": self.started_at,
            "log_tail": self.tail(50),
        }


class ProcessRegistry:
    """Small registry of named managed processes, keyed by a short id."""

    def __init__(self) -> None:
        self._processes: dict[str, ManagedProcess] = {}

    def register(
        self,
        key: str,
        label: str,
        cmd: list[str],
        cwd: Path,
        extra_env: dict[str, str] | None = None,
    ) -> ManagedProcess:
        proc = ManagedProcess(key, label, cmd, cwd, extra_env)
        self._processes[key] = proc
        return proc

    def get(self, key: str) -> ManagedProcess | None:
        return self._processes.get(key)
