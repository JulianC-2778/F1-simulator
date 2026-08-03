#!/usr/bin/env python3
"""
Integration launcher: one desktop window to start/stop/monitor all four
TORCS x Granite feature modules, instead of juggling 4-5 manual terminals.

This is a CONTROL PANEL, not a reimplementation of any feature. It only:
  1. spawns/kills the existing entry-point scripts as subprocesses
     (chat_engineer_gui.py, midware/commentary.py, midware/feature2_service.py,
     overlay-app via npm, ai_bot.py, tts_server.py, TORCS itself), and
  2. polls the REST API midware/commentary.py already exposes
     (/api/health, /api/features/status, /api/bot/status) plus plain TCP
     port probes, to show a live status panel.

It never touches another module's own logic/config files.

Known limits (by design, not bugs):
  - distFromStart telemetry (UDP 3101) and the SCR bot protocol (UDP 3001)
    are UDP, so there is no reliable "is anyone listening" TCP probe for
    them here. Telemetry health is instead read from midware's own
    /api/health (telemetry.ok); the AI bot's connection/strategy is read
    from midware's /api/bot/status (ai_bot.py's BotStatusReporter POSTs
    there once a second while running) -- its own process log tail is
    still useful as a secondary source (it prints "Identified! Entering
    drive loop" on a successful SCR handshake).
  - Selecting Quick Race / human driver / scr_server inside TORCS' own
    menus cannot be automated from here -- this launcher can start the
    TORCS binary, but the in-game race setup is still a manual step.
  - TORCS only really drives one race session at a time, so the human
    driver instance ("TORCS 人类驾驶") and the SCR/bot instance
    ("TORCS SCR Bot 模式") are mutually exclusive -- stop one before
    starting the other.

Run (same environment as the other scripts in this repo):
    cd ~/F1-simulator
    source .venv/bin/activate
    python launcher_gui.py
"""

from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from tkinter import messagebox, scrolledtext
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config  # noqa: E402  -- shared ports/hosts, single source of truth

OVERLAY_DIR = ROOT_DIR / "overlay-app"
LOG_DIR = ROOT_DIR / "logs"
PYTHON = sys.executable  # run child python scripts with THIS interpreter/venv

FONT = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_MONO = ("Consolas", 9)

STATUS_POLL_MS = 2000     # how often the background status thread re-checks everything
UI_REFRESH_MS = 400       # how often the UI drains the status queue / redraws log tail


# ---------------------------------------------------------------------------
# Small standalone helpers (no Tk dependency, easy to reason about in isolation)
# ---------------------------------------------------------------------------

def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    """TCP-only liveness probe. Do NOT use this for UDP ports (3101, 3001) --
    a UDP 'connect' never actually verifies a listener and would be misleading."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_get_json(url: str, timeout: float = 1.5) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

class ManagedProcess:
    """One subprocess this launcher owns: start / stop / tail its output.

    Each process is launched in its own POSIX process group (setsid) so stop()
    can signal the whole group -- important for `npm run start:commentary`,
    where npm itself would otherwise exit and leave the spawned Electron
    window running as an orphan.
    """

    def __init__(
        self,
        key: str,
        label: str,
        cmd: list[str],
        cwd: Path,
        extra_env: dict[str, str] | None = None,
        note: str = "",
    ) -> None:
        self.key = key
        self.label = label
        self.cmd = cmd
        self.cwd = cwd
        self.extra_env = extra_env or {}
        self.note = note
        self.proc: subprocess.Popen | None = None
        self.log: deque[str] = deque(maxlen=400)
        self._reader_thread: threading.Thread | None = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> str | None:
        """Returns an error message, or None on success."""
        if self.is_running():
            return None
        if not self.cwd.exists():
            return f"目录不存在：{self.cwd}"
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
            return f"启动失败：{exc}"
        self.log.clear()
        self.log.append(f"[launcher] 已启动 pid={self.proc.pid}：{' '.join(self.cmd)}  (cwd={self.cwd})")
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
        self.log.append(f"[launcher] 进程已退出（退出码={code}）")

    def stop(self) -> None:
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
        self.log.append("[launcher] 已停止")

    def tail(self, n: int = 200) -> list[str]:
        return list(self.log)[-n:]


def build_registry() -> dict[str, ManagedProcess]:
    """Every module this launcher knows how to start, keyed by a short id."""
    LOG_DIR.mkdir(exist_ok=True)

    torcs_human_env = {
        "TORCS_PLAYER_LOG_DIR": str(LOG_DIR),
        "TORCS_PLAYER_LOG_HZ": "20",
        "TORCS_PLAYER_UDP_HOST": "127.0.0.1",
        "TORCS_PLAYER_UDP_PORT": str(config.TELEMETRY_UDP_PORT),
    }

    modules = [
        ManagedProcess(
            "midware", "共享中枢 (python -m midware.app) -- Feature 3 依赖此进程",
            [PYTHON, "-m", "midware.app"], ROOT_DIR,
        ),
        ManagedProcess(
            "engineer_gui", "Feature 1 -- 工程师聊天窗口 (chat_engineer_gui.py)",
            [PYTHON, "chat_engineer_gui.py"], ROOT_DIR,
        ),
        ManagedProcess(
            "overlay_engineer", "工程师字幕 Overlay (Electron，可选)",
            ["npm", "start"], OVERLAY_DIR,
        ),
        ManagedProcess(
            "feature2", "Feature 2 -- 遥测看板 (midware/feature2_service.py)",
            [PYTHON, "midware/feature2_service.py"], ROOT_DIR,
            note="需要共享中枢(midware)先启动，否则拿不到遥测历史",
        ),
        ManagedProcess(
            "tts", "可选 -- 本地 TTS 服务 (tts_server.py)",
            [PYTHON, "tts_server.py"], ROOT_DIR,
        ),
        ManagedProcess(
            "torcs_human", "TORCS -- 人类驾驶 / 遥测模式 (torcs_launcher.sh)",
            ["bash", "torcs_launcher.sh"], ROOT_DIR, extra_env=torcs_human_env,
            note="Feature 1/2/3 的演示都要用这个模式；跟下面的 SCR 模式互斥",
        ),
        ManagedProcess(
            "ai_bot", "Feature 4 -- AI 驾驶 Bot (ai_bot.py --bot --granite)",
            [PYTHON, "ai_bot.py", "--bot", "--granite"], ROOT_DIR,
            note="需要 TORCS 已用 SCR 模式打开，并在 Quick Race 里手动选 scr_server",
        ),
        ManagedProcess(
            "torcs_scr", "TORCS -- SCR / Bot 模式 (BUILD/bin/torcs -ver 2013)",
            ["./BUILD/bin/torcs", "-ver", "2013"], ROOT_DIR,
            note="启动后需手动在 Quick Race 里选 scr_server 1；跟人类驾驶模式互斥",
        ),
    ]
    return {m.key: m for m in modules}


# Grouping only affects layout, not behaviour.
GROUPS: list[tuple[str, list[str]]] = [
    ("TORCS 本体（两种模式互斥，一次只开一个）", ["torcs_human", "torcs_scr"]),
    ("Feature 3 -- 实时解说 / 共享中枢", ["midware"]),
    ("Feature 1 -- AI 赛车工程师", ["engineer_gui", "overlay_engineer"]),
    ("Feature 2 -- 遥测看板", ["feature2"]),
    ("Feature 4 -- AI 驾驶 Bot", ["ai_bot"]),
    ("可选", ["tts"]),
]

# ---------------------------------------------------------------------------
# Status polling (runs on a background thread; UI only reads results off a queue)
# ---------------------------------------------------------------------------

class StatusSnapshot:
    """Everything the background thread learned in one polling pass."""

    def __init__(self) -> None:
        self.midware_up = False
        self.feature2_up = False
        self.tts_up = False
        self.health: dict[str, Any] | None = None
        self.bot_status: dict[str, Any] | None = None


def _poll_status_once() -> StatusSnapshot:
    snap = StatusSnapshot()
    snap.midware_up = _port_open(config.MIDWARE_HOST, config.MIDWARE_PORT)
    snap.feature2_up = _port_open(config.MIDWARE_HOST, config.FEATURE2_PORT)
    snap.tts_up = _port_open(config.MIDWARE_HOST, config.TTS_PORT)
    if snap.midware_up:
        snap.health = _http_get_json(f"{config.MIDWARE_BASE_URL}/api/health")
        snap.bot_status = _http_get_json(f"{config.MIDWARE_BASE_URL}/api/bot/status")
    return snap


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class LauncherApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.modules = build_registry()
        self.status_queue: "queue.Queue[StatusSnapshot]" = queue.Queue()
        self.selected_key: str | None = None
        self.row_widgets: dict[str, dict[str, Any]] = {}

        root.title("TORCS x Granite 集成控制台")
        root.geometry("980x760")
        root.minsize(860, 640)

        self._build_module_panel()
        self._build_health_panel()
        self._build_log_panel()

        self._stop_status_thread = threading.Event()
        threading.Thread(target=self._status_worker, daemon=True).start()

        self.root.after(200, self._refresh_rows)
        self.root.after(200, self._poll_status_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _build_module_panel(self) -> None:
        outer = tk.LabelFrame(self.root, text="模块启动 / 停止", padx=8, pady=6, font=FONT_BOLD)
        outer.pack(fill="x", padx=8, pady=4)

        for group_title, keys in GROUPS:
            group_frame = tk.LabelFrame(outer, text=group_title, padx=6, pady=4, font=FONT)
            group_frame.pack(fill="x", pady=3)
            for key in keys:
                self._build_module_row(group_frame, key)

    def _build_module_row(self, parent: tk.Widget, key: str) -> None:
        mod = self.modules[key]
        row = tk.Frame(parent)
        row.pack(fill="x", pady=2)

        status_dot = tk.Label(row, text="○", font=FONT_BOLD, fg="#888", width=2)
        status_dot.pack(side="left")

        label = tk.Label(row, text=mod.label, font=FONT, anchor="w")
        label.pack(side="left", padx=(4, 8))

        start_btn = tk.Button(row, text="启动", width=8, command=lambda k=key: self._on_start(k))
        start_btn.pack(side="right", padx=2)
        stop_btn = tk.Button(row, text="停止", width=8, command=lambda k=key: self._on_stop(k), state="disabled")
        stop_btn.pack(side="right", padx=2)
        log_btn = tk.Button(row, text="查看日志", width=8, command=lambda k=key: self._on_view_log(k))
        log_btn.pack(side="right", padx=2)

        if mod.note:
            tk.Label(row, text=mod.note, font=("Microsoft YaHei UI", 8), fg="#aa6600").pack(side="right", padx=(0, 10))

        self.row_widgets[key] = {"dot": status_dot, "start": start_btn, "stop": stop_btn}

    def _build_health_panel(self) -> None:
        frame = tk.LabelFrame(self.root, text="系统健康（来自 midware /api/health，每 2 秒刷新）", padx=8, pady=6, font=FONT_BOLD)
        frame.pack(fill="x", padx=8, pady=4)
        self.health_var = tk.StringVar(value="midware 尚未运行 / 不可达")
        tk.Label(frame, textvariable=self.health_var, font=FONT_MONO, justify="left", anchor="w").pack(fill="x")

    def _build_log_panel(self) -> None:
        frame = tk.LabelFrame(self.root, text="日志（点击某个模块的“查看日志”切换）", padx=8, pady=6, font=FONT_BOLD)
        frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log_title_var = tk.StringVar(value="(未选择模块)")
        tk.Label(frame, textvariable=self.log_title_var, font=FONT_BOLD).pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(frame, wrap="word", state="disabled", font=FONT_MONO, height=14)
        self.log_box.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ #
    # Button handlers
    # ------------------------------------------------------------------ #

    def _on_start(self, key: str) -> None:
        mod = self.modules[key]
        err = mod.start()
        if err:
            messagebox.showerror(mod.label, err)
        self._on_view_log(key)

    def _on_stop(self, key: str) -> None:
        self.modules[key].stop()

    def _on_view_log(self, key: str) -> None:
        self.selected_key = key
        self.log_title_var.set(self.modules[key].label)

    # ------------------------------------------------------------------ #
    # Background status polling + UI refresh
    # ------------------------------------------------------------------ #

    def _status_worker(self) -> None:
        while not self._stop_status_thread.is_set():
            snap = _poll_status_once()
            self.status_queue.put(snap)
            time.sleep(STATUS_POLL_MS / 1000.0)

    def _poll_status_queue(self) -> None:
        latest: StatusSnapshot | None = None
        try:
            while True:
                latest = self.status_queue.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            self._render_health(latest)
        self.root.after(UI_REFRESH_MS, self._poll_status_queue)

    def _render_health(self, snap: StatusSnapshot) -> None:
        if not snap.midware_up:
            self.health_var.set("midware(:%d) 不可达 -- 尚未启动，或还在起动中" % config.MIDWARE_PORT)
            return
        if snap.health is None:
            self.health_var.set("midware(:%d) 端口已开，但 /api/health 没有正常返回（可能还在初始化）" % config.MIDWARE_PORT)
            return
        h = snap.health
        telemetry = h.get("telemetry", {})
        model = h.get("model", {})
        scheduler = model.get("scheduler", {})
        tts = h.get("tts", {})
        overlay = h.get("overlay", {})
        features = h.get("features", [])
        feat_line = "  ".join(f"{f.get('name')}={f.get('lifecycle')}" for f in features) or "(无)"

        bot_line = "未上报"
        if snap.bot_status and snap.bot_status.get("status"):
            bs = snap.bot_status["status"]
            bot_line = f"connected={bs.get('connected')}  strategy={bs.get('strategy')}  error={bs.get('error') or '-'}"

        lines = [
            f"整体: {'OK' if h.get('ok') else '有问题'}    feature2(:{config.FEATURE2_PORT}): {'UP' if snap.feature2_up else 'down'}"
            f"    tts(:{config.TTS_PORT}): {'UP' if snap.tts_up else 'down'}",
            f"遥测(UDP:{config.TELEMETRY_UDP_PORT}): {'有数据' if telemetry.get('ok') else '无数据'}   "
            f"Granite: {model.get('base_url')} / {model.get('model')}  (失败次数={scheduler.get('failed', '?')})",
            f"TTS 已启用: {tts.get('enabled')}    Overlay 连接数: {overlay.get('ws_clients', 0)}",
            f"Feature 状态: {feat_line}",
            f"Feature 4 Bot 状态 (来自 /api/bot/status，ai_bot.py 每秒自动上报一次): {bot_line}",
        ]
        self.health_var.set("\n".join(lines))

    def _refresh_rows(self) -> None:
        for key, mod in self.modules.items():
            widgets = self.row_widgets[key]
            running = mod.is_running()
            widgets["dot"].configure(text="●" if running else "○", fg="#0a7d3a" if running else "#888")
            widgets["start"].configure(state="disabled" if running else "normal")
            widgets["stop"].configure(state="normal" if running else "disabled")

        if self.selected_key is not None:
            lines = self.modules[self.selected_key].tail(200)
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.insert("end", "\n".join(lines))
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        self.root.after(UI_REFRESH_MS, self._refresh_rows)

    # ------------------------------------------------------------------ #

    def _on_close(self) -> None:
        running = [m.label for m in self.modules.values() if m.is_running()]
        if running:
            proceed = messagebox.askyesno(
                "还有模块在运行",
                "以下模块仍在运行，关闭控制台不会停止它们（进程独立于本窗口）：\n\n"
                + "\n".join(f"- {name}" for name in running)
                + "\n\n是否现在一并停止后再关闭？",
            )
            if proceed:
                for mod in self.modules.values():
                    mod.stop()
        self._stop_status_thread.set()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
