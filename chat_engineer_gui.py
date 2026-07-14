#!/usr/bin/env python3
"""
Feature 1 (AI Racing Engineer Chatbot): desktop GUI version.

Same car_state -> midware.context_manager -> Granite pipeline as
chat_engineer.py, but with a Tkinter chat window (live status panel +
scrollable chat history + input box) instead of a raw terminal loop. Meant
to run as a floating window next to (or on top of) the TORCS game window
during a demo, instead of alt-tabbing to a terminal.

Run:
    python3 chat_engineer_gui.py

Same midware-first, UDP-fallback car_state source selection as chat_engineer.py.

Env vars: same as chat_engineer.py --
    TORCS_ENGINEER_BASE_URL          - Granite/LM Studio endpoint
    TORCS_ENGINEER_MODEL             - model id override
    TORCS_ENGINEER_USE_FAKE_DATA     - "true" to force demo data instead of live telemetry
    TORCS_ENGINEER_UDP_PORT          - live telemetry UDP port, used only as a fallback
                                          when midware isn't reachable (default 3101)
    TORCS_ENGINEER_MIDWARE_URL       - midware base URL to probe first (default http://127.0.0.1:8880)
plus one new one:
    TORCS_ENGINEER_REFRESH_MS        - status panel refresh interval in ms (default 500)
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import granite_client
import overlay_broadcast
import voice_input
from car_state_source import (
    CarStateSource,
    FakeCarStateSource,
    HttpCarStateSource,
    LiveCarStateSource,
    wait_for_live_state,
)
from midware.context_manager import ContextConfig, ContextManager, ENGINEER_PERSONA
from telemetry_common import env_flag


USE_FAKE_DATA = env_flag("TORCS_ENGINEER_USE_FAKE_DATA", False)
MIDWARE_BASE_URL = os.getenv("TORCS_ENGINEER_MIDWARE_URL", config.MIDWARE_BASE_URL)
UDP_PORT = int(os.getenv("TORCS_ENGINEER_UDP_PORT", config.TELEMETRY_UDP_PORT))
REFRESH_MS = int(os.getenv("TORCS_ENGINEER_REFRESH_MS", "500"))

# (car_state key, display label, format string)
STATUS_FIELDS = (
    ("speed", "速度 (km/h)", "{:.0f}"),
    ("rpm", "转速 (rpm)", "{:.0f}"),
    ("gear", "挡位", "{}"),
    ("track_pos", "赛道位置", "{:.2f}"),
    ("damage", "车辆损伤", "{:.0f}"),
    ("fuel", "剩余油量 (L)", "{:.1f}"),
    ("lap_time", "当前圈速 (s)", "{:.1f}"),
)


def choose_car_state_source() -> CarStateSource:
    """Identical selection logic to chat_engineer.py's CLI version."""
    if USE_FAKE_DATA:
        print("[ChatEngineerGUI] TORCS_ENGINEER_USE_FAKE_DATA=true -> using demo car_state data.")
        return FakeCarStateSource()

    # Probe midware's REST API first -- avoids the "Address already in use"
    # port conflict when midware (needed for the overlay window) is already
    # running. Only falls back to binding UDP directly if midware isn't
    # reachable, so this script still works standalone.
    midware_source = HttpCarStateSource(base_url=MIDWARE_BASE_URL)
    print(f"[ChatEngineerGUI] Probing midware at {MIDWARE_BASE_URL} for live telemetry (no UDP bind)...")
    if wait_for_live_state(midware_source, timeout=2.0):
        print("[ChatEngineerGUI] Live telemetry detected via midware, using real car_state data.")
        return midware_source

    print(f"[ChatEngineerGUI] midware not reachable at {MIDWARE_BASE_URL} (or no telemetry yet); falling back to UDP.")
    live = LiveCarStateSource(udp_port=UDP_PORT)
    print(f"[ChatEngineerGUI] Waiting up to 5s for live telemetry on UDP:{UDP_PORT} ...")
    if wait_for_live_state(live, timeout=5.0):
        print("[ChatEngineerGUI] Live telemetry detected, using real car_state data.")
        return live

    print(
        "[ChatEngineerGUI] No live telemetry yet. Falling back to demo data. "
        "Start TORCS with the human driver telemetry export enabled (see README), "
        "or set TORCS_ENGINEER_USE_FAKE_DATA=true to silence this message."
    )
    return FakeCarStateSource()


class ChatEngineerApp:
    """Tkinter chat window: status panel + chat log + input bar.

    Granite calls run on a background thread so the window never freezes
    while waiting for a reply; results come back through a thread-safe
    queue that the Tkinter main loop polls via `root.after`.
    """

    def __init__(self, root: tk.Tk, connection: Any, car_state_source: CarStateSource) -> None:
        self.root = root
        self.connection = connection
        self.car_state_source = car_state_source
        self.ctx_mgr = ContextManager(ContextConfig(commentator_persona=ENGINEER_PERSONA))
        self.latest_car_state: dict[str, Any] = {}
        self.result_queue: "queue.Queue[str]" = queue.Queue()
        self.pending = False

        # Voice input (English-only, see voice_input.py). `self.recorder` is
        # non-None exactly while a recording is in progress -- the mic
        # button toggles between start/stop based on that.
        self.recorder: voice_input.Recorder | None = None
        self.voice_queue: "queue.Queue[str]" = queue.Queue()

        root.title("TORCS AI 赛车工程师")
        root.geometry("760x600")

        self._build_status_panel()
        self._build_chat_area()
        self._build_input_bar()

        self.root.after(0, self._refresh_status)
        self.root.after(100, self._poll_results)
        self.root.after(150, self._poll_voice_results)

    # ---- layout ----
    def _build_status_panel(self) -> None:
        frame = tk.LabelFrame(self.root, text="实时车辆状态", padx=8, pady=6)
        frame.pack(fill="x", padx=8, pady=(8, 4))

        self.status_vars: dict[str, tk.StringVar] = {}
        grid = tk.Frame(frame)
        grid.pack(fill="x")
        for index, (key, label, _fmt) in enumerate(STATUS_FIELDS):
            row, col = divmod(index, 4)
            var = tk.StringVar(value="--")
            self.status_vars[key] = var
            tk.Label(grid, text=f"{label}：", anchor="w").grid(
                row=row, column=col * 2, sticky="w", padx=(0, 2), pady=2
            )
            tk.Label(grid, textvariable=var, anchor="w", fg="#0a7d3a").grid(
                row=row, column=col * 2 + 1, sticky="w", padx=(0, 16), pady=2
            )

        self.problems_var = tk.StringVar(value="暂无明显异常")
        tk.Label(frame, text="检测到的问题：", anchor="w").pack(anchor="w", pady=(4, 0))
        tk.Label(
            frame, textvariable=self.problems_var, anchor="w", fg="#c0392b", wraplength=700, justify="left"
        ).pack(anchor="w")

    def _build_chat_area(self) -> None:
        frame = tk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        self.chat_box = scrolledtext.ScrolledText(
            frame, wrap="word", state="disabled", font=("Microsoft YaHei UI", 10)
        )
        self.chat_box.pack(fill="both", expand=True)
        self.chat_box.tag_config("user", foreground="#0d6efd")
        self.chat_box.tag_config("assistant", foreground="#212529")
        self.chat_box.tag_config("system", foreground="#888888")

    def _build_input_bar(self) -> None:
        frame = tk.Frame(self.root)
        frame.pack(fill="x", padx=8, pady=(0, 8))
        self.entry = tk.Entry(frame, font=("Microsoft YaHei UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.entry.bind("<Return>", lambda _event: self._on_send())
        self.mic_button = tk.Button(frame, text="🎤", width=4, command=self._on_mic_click)
        self.mic_button.pack(side="left", padx=(0, 8))
        self.send_button = tk.Button(frame, text="发送", width=8, command=self._on_send)
        self.send_button.pack(side="left")

    # ---- status refresh (runs on the main/UI thread via `after`) ----
    def _refresh_status(self) -> None:
        try:
            self.latest_car_state = self.car_state_source.get_state()
        except Exception as exc:
            self._append_chat("system", f"[读取车辆数据失败：{exc}]")
        else:
            for key, _label, fmt in STATUS_FIELDS:
                value = self.latest_car_state.get(key, 0)
                self.status_vars[key].set(fmt.format(value))
            problems = self.latest_car_state.get("problems") or []
            self.problems_var.set("、".join(problems) if problems else "暂无明显异常")
        self.root.after(REFRESH_MS, self._refresh_status)

    # ---- chat log ----
    def _append_chat(self, tag: str, text: str) -> None:
        self.chat_box.configure(state="normal")
        self.chat_box.insert("end", text + "\n\n", tag)
        self.chat_box.configure(state="disabled")
        self.chat_box.see("end")

    def _on_send(self) -> None:
        if self.pending:
            return
        question = self.entry.get().strip()
        if not question:
            return
        self.entry.delete(0, "end")
        self._append_chat("user", f"玩家：{question}")

        car_state = dict(self.latest_car_state) if self.latest_car_state else self.car_state_source.get_state()
        self.ctx_mgr.add_user(self.ctx_mgr.format_engineer_prompt(car_state, question))
        messages = self.ctx_mgr.build_messages()

        self.pending = True
        self.send_button.configure(state="disabled", text="等待回答...")
        threading.Thread(target=self._ask_worker, args=(messages,), daemon=True).start()

    def _ask_worker(self, messages: list[dict[str, str]]) -> None:
        """Runs on a background thread. Only touches the thread-safe queue,
        never the Tkinter widgets directly. Also best-effort broadcasts the
        reply to the shared overlay display layer (see overlay_broadcast.py
        / docs/display-layer-contract.md) -- that call never raises and
        never blocks long, even if midware/overlay-app are not running."""
        overlay_broadcast.broadcast_engineer_start()
        try:
            answer = granite_client.ask_engineer(self.connection, messages)
        except Exception as exc:
            answer = f"[Granite 请求失败：{exc}]"
            overlay_broadcast.broadcast_engineer_error(str(exc))
        else:
            overlay_broadcast.broadcast_engineer_reply(answer)
        self.result_queue.put(answer)

    def _poll_results(self) -> None:
        try:
            while True:
                answer = self.result_queue.get_nowait()
                self._append_chat("assistant", f"AI工程师：{answer}")
                self.ctx_mgr.add_assistant(answer)
                self.pending = False
                self.send_button.configure(state="normal", text="发送")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_results)

    # ---- voice input (English-only, see voice_input.py) ----
    def _on_mic_click(self) -> None:
        if self.recorder is None:
            if self.pending:
                return  # don't start recording while waiting on an answer
            self.recorder = voice_input.Recorder()
            self.recorder.start()
            self.mic_button.configure(text="■", fg="#c0392b")
            self._append_chat("system", "[录音中...再次点击麦克风按钮结束（英文提问）]")
            return

        # Recording in progress -- stop it and transcribe on a background
        # thread (stopping parecord + running Whisper can both take a
        # moment; never touch Tkinter widgets off the main thread, mirrors
        # _ask_worker's pattern via the thread-safe voice_queue).
        recorder = self.recorder
        self.recorder = None
        self.mic_button.configure(text="…", state="disabled", fg="black")
        threading.Thread(target=self._voice_worker, args=(recorder,), daemon=True).start()

    def _voice_worker(self, recorder: "voice_input.Recorder") -> None:
        wav_path = recorder.stop()
        text = voice_input.transcribe(wav_path) if wav_path else ""
        self.voice_queue.put(text)

    def _poll_voice_results(self) -> None:
        try:
            while True:
                text = self.voice_queue.get_nowait()
                self.mic_button.configure(text="🎤", state="normal")
                if text:
                    self.entry.delete(0, "end")
                    self.entry.insert(0, text)
                    self._append_chat("system", f"[语音识别结果：{text}（可编辑后按回车/发送）]")
                else:
                    self._append_chat("system", "[未识别到语音内容，请重试或直接打字提问]")
        except queue.Empty:
            pass
        self.root.after(150, self._poll_voice_results)


def main() -> None:
    connection = granite_client.connect()
    granite_client.print_banner(connection)

    car_state_source = choose_car_state_source()

    root = tk.Tk()
    app = ChatEngineerApp(root, connection, car_state_source)
    app._append_chat(
        "system",
        "已连接 Granite。输入问题后按回车或点击发送，例如：我的轮胎状态怎么样？/ 现在该不该进站？",
    )
    root.mainloop()


if __name__ == "__main__":
    main()
