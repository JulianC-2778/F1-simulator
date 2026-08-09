"""
Context Manager — SillyTavern 风格上下文窗口控制

SillyTavern 的核心思路：
  1. 把所有消息分层：System Prompt → Persona → 历史对话 → 当前用户消息
  2. 给每层分配 token 预算；超出时从历史对话最旧的一端裁剪
  3. 格式化时按选定的 Chat Template（ChatML / Instruct / Raw）拼装
"""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Token 估算（无需 tiktoken 依赖）
# 1 token ≈ 4 个 ASCII 字符 / 2 个 CJK 字符 —— 足够做预算控制
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    ascii_chars = len(re.sub(r'[^\x00-\x7F]', '', text))
    cjk_chars   = len(re.findall(r'[一-鿿぀-ヿ]', text))
    other_chars = len(text) - ascii_chars - cjk_chars
    return max(1, ascii_chars // 4 + cjk_chars // 2 + other_chars // 3)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Message:
    role: Literal["system", "user", "assistant"]
    content: str
    pinned: bool = False          # True = 不会被裁剪（对应 ST 的 "Keep in Context"）
    tokens: int = field(init=False)

    def __post_init__(self):
        self.tokens = estimate_tokens(self.content)


# ---------------------------------------------------------------------------
# 工程师人设（Feature 1: chat_engineer.py / chat_engineer_gui.py 使用）
# 复用 ContextConfig.commentator_persona 字段承载，而不是新开一个字段名，
# 因为该字段名已经是浏览器 dashboard 配置面板（dashboard.html 的
# `/api/config/context` 调用）的 API 契约的一部分，改名会牵连前端。
# ---------------------------------------------------------------------------

# Shared across both engineer answer styles below: identity, no invented
# data, off-topic handling, and the closing no-rambling/English-only rules.
# Keeping this in one place means a future prompt tweak only has to happen
# once instead of drifting between the two variants.
_ENGINEER_PERSONA_BASE = (
    "You are a professional, direct-talking AI racing engineer on the radio with a TORCS driver. "
    "Answer only using the live telemetry and detected issues provided below -- never invent numbers "
    "or facts that were not given. "
)

_ENGINEER_PERSONA_TAIL = (
    "Only recommend a pit stop for fuel or damage issues. If the only detected issue is about track "
    "position, speed, rpm, or gear, that is a driving correction, not something a pit stop fixes -- say "
    "so plainly (e.g. 'get back on track') instead of recommending a pit. "
    "If a question has nothing to do with the car, the race, or the data provided, deprioritize it "
    "-- answer it last and briefly, or skip it if it doesn't matter. Never pad, ramble, or repeat yourself. "
    "Always answer in English."
)

# "Professional" style: scales to what was actually asked, can include
# numbers and a bit of reasoning. This is the default.
ENGINEER_PERSONA = _ENGINEER_PERSONA_BASE + (
    "Scale your answer to what was actually asked: a single question gets up to three or four "
    "sentences, including relevant numbers (track position, speed, fuel, lap time, etc.) and a bit of "
    "reasoning when it's useful -- not just a bare fact. If the driver asks about several things at "
    "once, keep each part brief so the whole answer doesn't balloon, and answer them in order of "
    "importance to the race, not necessarily the order they were asked. "
) + _ENGINEER_PERSONA_TAIL

# "Concise" style: every part of the question still gets answered, but each
# part is compressed to the bare answer/fact only, no elaboration.
ENGINEER_PERSONA_CONCISE = _ENGINEER_PERSONA_BASE + (
    "Give only the bare answer or fact for each thing asked -- no elaboration, no reasoning, no extra "
    "detail. If the driver asks about several things at once, answer every part, but keep each part to "
    "the bare minimum (a few words or one short clause) rather than skipping any of them. "
    "If the question is a yes/no question, still include the single key reason in that same short answer "
    "(e.g. 'Yes, fuel low.' or 'No, car is fine.') -- never answer with just 'Yes' or 'No' alone. "
) + _ENGINEER_PERSONA_TAIL


# ---------------------------------------------------------------------------
# 事件解说：字段展示标签（把 payload 渲染成 '### Field: Value' 而不是 JSON）
# ---------------------------------------------------------------------------

_EVENT_FIELD_LABELS: dict[str, str] = {
    "event_type": "Event",
    "event_reason": "Reason",
    "event_time": "Time",
    "race_pos": "Position",
    "lap": "Lap",
    "gear": "Gear",
    "track_pos": "Track offset",
    "fuel_remaining": "Fuel remaining",
    "total_damage": "Total damage",
    "damage_delta": "Damage this event",
    "speed_delta": "Speed change",
    "last_lap_time": "Last lap time",
    "front_gap": "Gap ahead",
    "rear_gap": "Gap behind",
    "nearest_gap": "Nearest gap",
    "direction": "Direction",
    "new_pos": "New position",
    "side": "Side",
    "completed_lap": "Completed lap",
    "collision_direction": "Collision direction",
    "collision_partner": "Collision partner",
    "rankings": "Rankings",
}

# 内部记账字段，不是"事件数据"，不渲染进 prompt 正文
_EVENT_FIELD_SKIP = {"task", "style"}


def format_event_fields(payload: dict) -> str:
    """
    把事件 payload 渲染成 '### Field: Value' 逐行文本，取代原始 json.dumps。
    避免大括号/引号/缩进这类 JSON 语法符号浪费 token，也避免本地小模型把
    JSON 语法本身当成要复述的内容。
    """
    lines = []
    for key, value in payload.items():
        if key in _EVENT_FIELD_SKIP:
            continue
        label = _EVENT_FIELD_LABELS.get(key, key.replace("_", " ").title())
        if key == "rankings" and isinstance(value, list):
            value = ", ".join(
                f"P{r.get('race_pos', '?')} {r.get('car_name', '?')}" for r in value
            ) or "n/a"
        elif key == "collision_partner":
            value = (
                f"{value.get('car_name', '?')} (P{value.get('race_pos', '?')})"
                if isinstance(value, dict) else "unknown"
            )
        lines.append(f"### {label}: {value}")
    return "\n".join(lines)


def _safe_number(value: Any, default: float = 0.0) -> float:
    """Coerce a car_state field to a float for display formatting.

    A caller-supplied car_state (e.g. a test harness or a malformed request)
    can put the wrong type in a numeric field -- e.g. fuel as the string
    "low" instead of a number. Without this, an f-string format spec like
    f"{value:.1f}" raises an uncaught TypeError, and since format_car_state()
    runs before ask_engineer()'s try/except block starts, that crash isn't
    handled gracefully at all. Falls back to `default` for anything that
    can't be interpreted as a number, instead of raising.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_car_state(car_state: dict) -> str:
    """Render a car_state dict (see car_state_source.py) as a short status block."""
    problems = car_state.get("problems") or []
    # race_analyzer.analyze_car_state() returns ["normal"] (not an empty
    # list) when nothing is wrong -- spell that out explicitly instead of
    # handing the model the bare word "normal", which it can misread as an
    # issue name rather than "no issues, car is fine".
    if not problems or problems == ["normal"]:
        problem_text = "None -- car is on track and under control."
    else:
        problem_text = ", ".join(problems)
    lines = [
        f"Speed: {_safe_number(car_state.get('speed')):.0f} km/h",
        f"RPM: {_safe_number(car_state.get('rpm')):.0f}",
        f"Gear: {car_state.get('gear', 0)}",
        f"Track position: {_safe_number(car_state.get('track_pos')):.2f} (0 = center line, closer to +/-1 = closer to the track edge)",
        f"Damage: {_safe_number(car_state.get('damage')):.0f}",
        f"Fuel remaining: {_safe_number(car_state.get('fuel')):.1f} L",
        f"Current lap time: {_safe_number(car_state.get('lap_time')):.1f} s",
        f"Detected issues: {problem_text}",
    ]

    # tire_wear_pct / pit_window are optional -- only present when
    # runtime.py's ask_engineer has run them through tire_strategy.py's
    # RaceStrategyTracker first. TORCS itself never reports tire wear (see
    # tire_strategy.py's module docstring), so this is an estimate, and is
    # labelled as one so the model doesn't present it as a measured value.
    if "tire_wear_pct" in car_state:
        lines.append(
            f"Estimated tire wear: {_safe_number(car_state.get('tire_wear_pct')):.0f}% "
            "(0 = fresh, 100 = worn out; heuristic estimate, TORCS does not report this directly)"
        )
    if "pit_window" in car_state:
        pit = car_state["pit_window"]
        laps_left = pit.get("laps_of_fuel_left")
        laps_left_text = "unknown" if laps_left is None else f"{laps_left:.1f}"
        reasons_text = ", ".join(pit.get("reasons") or []) or "none"
        lines.append(
            f"Pit window analysis: recommend pit = {'yes' if pit.get('recommend_pit') else 'no'}, "
            f"urgency = {pit.get('urgency', 'low')}, reasons = {reasons_text}, "
            f"estimated laps of fuel left = {laps_left_text}"
        )

    return "\n".join(lines)


@dataclass
class ContextConfig:
    # ---- 窗口大小 ----
    max_context_tokens: int = 4096   # 发送给 AI 的总 token 上限
    max_response_tokens: int = 512   # 留给 AI 回复的 token 数
    # estimate_tokens() 是启发式估算（非真实分词器），长会话历史攒够大时，
    # 哪怕只低估 1 个 token 也会让实际 prompt 超出模型真实上限，导致请求
    # 直接被拒（真实复现过，见 docs/commentary_test_handoff_2.md 第 3 节）。
    # 预留这部分余量，不参与裁剪，专门用来吸收估算误差。
    safety_margin_tokens: int = 256

    # ---- 裁剪策略（对应 ST 的 "Context Trim Strategy"）----
    trim_strategy: Literal["oldest_first", "newest_first"] = "oldest_first"

    # ---- 聊天模板 ----
    chat_template: Literal["chatml", "instruct", "raw"] = "chatml"

    # ---- 解说员人设（对应 ST 的 "Character Card / System Prompt"）----
    commentator_persona: str = (
        "You are a live commentator for this racing simulator. "
        "React to race events — battles, overtakes, mistakes, dramatic moments — in one short, punchy sentence. "
        "Refer to cars only by their given names: the player's car is \"player\"; other cars use the exact name in the data (e.g. car1, car2). Never invent human driver names. "
        "ALL CAPS only for shock moments. Never mention raw numbers like speed or RPM. "
        "Do not repeat the previous line."
    )

    # ---- 数据字段过滤（选择哪些 TORCS 字段进入 prompt）----
    included_fields: list = field(default_factory=lambda: [
        "sim_time", "racePos", "lap",
        "gear", "damage", "fuel",
        "lastLapTime", "curLapTime", "trackPos",
    ])

    # ---- 排名摘要是否附加 ----
    include_rankings: bool = True


# ---------------------------------------------------------------------------
# 核心：Context Manager
# ---------------------------------------------------------------------------

class ContextManager:
    """
    管理对话历史、System Prompt 拼装、上下文裁剪。
    设计对应 SillyTavern 的 Context Template + Chat History 模块。
    """

    def __init__(self, config: ContextConfig | None = None):
        self.config  = config or ContextConfig()
        self.history: list[Message] = []   # 解说历史（user=遥测数据, assistant=解说）

    # ------------------------------------------------------------------
    # 添加消息
    # ------------------------------------------------------------------

    def add_user(self, content: str, pinned: bool = False) -> Message:
        msg = Message(role="user", content=content, pinned=pinned)
        self.history.append(msg)
        return msg

    def add_assistant(self, content: str) -> Message:
        msg = Message(role="assistant", content=content)
        self.history.append(msg)
        return msg

    def clear_history(self):
        self.history = [m for m in self.history if m.pinned]

    # ------------------------------------------------------------------
    # 构建发送给 AI 的 messages 列表（已裁剪到 token 预算内）
    # ------------------------------------------------------------------

    def build_messages(self) -> list[dict]:
        """
        返回符合 OpenAI Chat Completions 格式的 messages 列表。
        对应 ST 的 "Build Prompt" 步骤。
        """
        budget = (
            self.config.max_context_tokens
            - self.config.max_response_tokens
            - self.config.safety_margin_tokens
        )

        # 1. System prompt（固定占用，不裁剪）
        system_msg = Message(role="system", content=self.config.commentator_persona)
        budget -= system_msg.tokens

        # 2. 历史消息：先收集 pinned，再从新到旧填充剩余预算
        pinned   = [m for m in self.history if m.pinned]
        unpinned = [m for m in self.history if not m.pinned]

        for m in pinned:
            budget -= m.tokens

        # 按裁剪策略排序 unpinned
        if self.config.trim_strategy == "oldest_first":
            candidates = list(reversed(unpinned))   # 从最新往旧选，直到预算耗尽
        else:
            candidates = list(unpinned)             # 从最旧往新选

        selected_unpinned: list[Message] = []
        for m in candidates:
            if budget - m.tokens < 0:
                break
            budget -= m.tokens
            selected_unpinned.append(m)

        if self.config.trim_strategy == "oldest_first":
            selected_unpinned.reverse()   # 恢复时间顺序

        final_history = sorted(
            pinned + selected_unpinned,
            key=lambda m: self.history.index(m)
        )

        # 3. 组装
        messages = [{"role": "system", "content": system_msg.content}]
        for m in final_history:
            messages.append({"role": m.role, "content": m.content})

        return messages

    # ------------------------------------------------------------------
    # 格式化遥测数据 → user message（对应 ST 的 "World Info / Author's Note"）
    # ------------------------------------------------------------------

    def format_telemetry(
        self,
        telemetry: dict,
        rankings: list[dict] | None = None,
    ) -> str:
        """
        把 TORCS CSV 行转成自然语言描述，塞进 user message。
        只包含 config.included_fields 中指定的字段。
        """
        lines = ["【实时遥测数据】"]
        field_labels = {
            "sim_time":      ("比赛时间",    "s"),
            "racePos":       ("当前名次",    "名"),
            "lap":           ("当前圈数",    "圈"),
            "speedX":        ("纵向车速",    "km/h"),
            "rpm":           ("发动机转速",  "rpm"),
            "gear":          ("挡位",        ""),
            "throttle":      ("油门",        "%"),
            "brake":         ("制动",        "%"),
            "steer":         ("转向",        ""),
            "damage":        ("车辆损伤",    ""),
            "fuel":          ("剩余油量",    "L"),
            "lastLapTime":   ("上圈用时",    "s"),
            "curLapTime":    ("本圈用时",    "s"),
            "trackPos":      ("赛道位置",    ""),
            "distFromStart": ("距起点距离",  "m"),
        }

        for key in self.config.included_fields:
            if key not in telemetry:
                continue
            val = telemetry[key]
            label, unit = field_labels.get(key, (key, ""))
            # 数值格式化
            if key in ("throttle", "brake"):
                val = f"{float(val)*100:.0f}"
            elif isinstance(val, float):
                val = f"{val:.2f}"
            lines.append(f"  {label}: {val}{unit}")

        if self.config.include_rankings and rankings:
            lines.append("\n【全场排名】")
            for r in rankings:
                lines.append(
                    f"  P{r.get('race_pos','?')} {r.get('car_name','?')} "
                    f"— 圈数 {r.get('laps','?')} / 距起点 {r.get('dist_from_start',0):.0f}m"
                )

        lines.append("\nDeliver one short commentary line. Focus on position and drama, not numbers.")
        return "\n".join(lines)

    def format_event_prompt(self, payload: dict) -> str:
        """
        把事件检测引擎生成的结构化 payload 转成 user message。
        字段用 '### Field: Value' 逐行渲染（见 format_event_fields），
        而不是原始 json.dumps。
        """
        return (
            "React to this race event in one short sentence. "
            "Use \"player\" for the player's car and the given name (e.g. car1) for others — never invent driver names. "
            "Short punchy phrases, ALL CAPS only for shock moments, no raw numbers.\n\n"
            f"{format_event_fields(payload)}"
        )

    def format_engineer_prompt(self, car_state: dict, user_question: str) -> str:
        """
        把 car_state（见 car_state_source.py 契约）+ 玩家问题格式化成
        engineer persona 场景下的 user message 内容。
        """
        return (
            f"Current car data:\n{format_car_state(car_state)}\n\n"
            f"Driver's question:\n{user_question}"
        )

    def format_event_history_entry(self, payload: dict) -> str:
        event_type = payload.get("event_type", "unknown")
        reason = payload.get("event_reason", "")
        event_time = payload.get("event_time", "?")
        return (
            f"事件摘要: {event_type} @ {event_time}s; {reason}; "
            f"P{payload.get('race_pos', '?')}, lap {payload.get('lap', '?')}, "
            f"speed {payload.get('speed', '?')} km/h."
        )

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        total = sum(m.tokens for m in self.history)
        system_tokens = estimate_tokens(self.config.commentator_persona)
        return {
            "history_messages": len(self.history),
            "history_tokens": total,
            "system_tokens": system_tokens,
            "budget_remaining": (
                self.config.max_context_tokens
                - self.config.max_response_tokens
                - self.config.safety_margin_tokens
                - system_tokens
                - total
            ),
            "max_context_tokens": self.config.max_context_tokens,
            "max_response_tokens": self.config.max_response_tokens,
            "safety_margin_tokens": self.config.safety_margin_tokens,
        }
