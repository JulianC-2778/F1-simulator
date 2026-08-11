# AI 驾驶 Bot（方向四）测试实施任务书

> 本文件的结构和方法论直接复用 已经跑通并产出真实数据的测试体系 —— 四个工作包（功能正确性 / 真实场景准确率 /
> 端到端延迟 / 稳定性与故障恢复）、traceability matrix、real-experiment protocol、fault-injection
> protocol 的分工方式原样保留，只把评测对象从"解说"换成"驾驶"。建议交给 Codex / Claude Code
> 之类的编程 AI 时，先让它读一遍 `docs/commentary_test_matrix.md` 和
> `docs/commentary_fault_injection_protocol.md` 作为范例，再执行本文件。

## 0. 为什么可以照搬 commentary 的方法论，而不是从零设计

两个方向的被测系统形状几乎一样：一个后台循环持续消费 telemetry → 一套规则/阈值把 telemetry
变成决策 → 决策经过一次可选的 Granite 调用被精炼 → 结果通过网络送到另一个进程。区别只是：

| | Commentary（方向三） | AI 驾驶 Bot（方向四） |
|---|---|---|
| 输入 | UDP 3101 telemetry（TelemetryService 采集） | UDP 3001 SCR 状态包（`ScrClient.receive_state`，直连 TORCS，**不经过 midware**） |
| 规则层 | `commentary_engine.py::detect_event` | `ai_bot.py::compute_control` + `safety_filter` |
| 模型调用 | 每次事件触发一次，走 midware `/api/commentary/*` | 每 5 秒一次（`_STRATEGY_INTERVAL`），走 midware `/api/bot/strategy` |
| 输出 | WebSocket 广播文本 + TTS | SCR 控制包（accel/brake/gear/steer）经 UDP 直接回给 TORCS |
| 安全网 | 无（文本无所谓"安全"） | `safety_filter` 硬编码优先级规则，**必须**能在模型出错/超时/瞎说时兜底，否则车会撞墙 |
| 心跳/状态 | 无独立心跳 | `BotStatusReporter` → `/api/bot/status`，`bot_status_service` 判定 healthy/stale/disconnected |

因此工作包 A–D 的定义原样迁移；工作包 B（commentary 是"事件检测准确率"）在这里变成
"真实驾驶表现与安全策略准确率"，指标从 P/R/F1 换成圈完成率/出界恢复率/`safety_filter`
介入正确率；工作包 C 多一层 commentary 没有的东西——**控制环本身的实时性**（每 20ms 一帧，
比模型调用严格得多）；工作包 D 的故障目录里 UDP 中断换成"TORCS/SCR 连接中断"，Granite
故障的预期行为从"暂停解说"换成"必须继续用兜底策略开车，不能因为模型挂了就停车或撞车"。

## 1. 必须遵守的工作原则（与 commentary 相同，不重复发明）

- 先读当前代码、测试、配置和文档，再决定修改方案；不要凭本任务书猜测函数名或接口——本文件
  第 2 节的组件映射表已经是读代码的结果，若与实际代码不符，**以代码为准**并更新本文件。
- 以当前检出的 commit 为唯一实现依据，报告中记录 commit hash。
- 只修改与 Bot 测试、必要可测试性改造、日志和实验脚本直接相关的内容，不做无关重构。
- 外部依赖（TORCS/`scr_server`、Granite/LM Studio、midware HTTP）通过 mock/fake 或依赖注入隔离；
  自动化测试必须确定性运行，不依赖真实 TORCS 或真实模型服务。
- 不得伪造真实驾驶、延迟或故障恢复结果；结果未达标就如实记录，不倒推调整判定阈值或匹配窗口。
- 若发现实现缺陷，先写一个能复现缺陷的失败测试，再做最小修复，再跑完整回归——`safety_filter`
  这种安全网代码尤其不允许"顺手改过去"。
- 所有生成的 CSV/JSON/报告区分 `sample/demo`、`automated test`、`real experiment`。

## 2. 组件映射表（Requirement / Code / Existing test）

> 基于当前仓库读码结果；`ai_bot.py` 行号会随改动漂移，实施前用 `grep -n` 复核一遍。

| 概念 | 真实代码 | 现有测试 |
|---|---|---|
| SCR 协议解析/编码 | `ai_bot.py::parse_scr_state`（L140）/ `format_scr_control`（L181） | 无 pytest；仅 `ai_bot.py` 内置 `_run_tests()`（L2566 起，`python ai_bot.py` 手动跑） |
| UDP 客户端/握手/收发 | `ai_bot.py::ScrClient`（L237，`connect`/`receive_state`/`send_control`） | 无（内置自测只测实例化+close，不连真实 socket） |
| 换挡/ABS/TCL/刹车距离 | `_auto_gear`/`_gear_from_speed`/`_gear_shift`（L382-461）、`_apply_abs`/`_apply_tcl`（L463-511）、`_brake_dist`（L512） | 无 pytest；内置自测覆盖部分场景 |
| 主控制决策 | `compute_control`（L1439）、`_DriveParams`（L576） | 无 |
| 出界恢复 | `_recovery_control`/`_recovery_steer`/`_stabilize_action`（L1288-1421）、`_pursuit_target`（L1422） | 无 |
| 赛道模型 | `track_model.py`（内置自测，仅覆盖 unit-test 轨和 g-track-2，见 `docs/testing-plan.md` §8 P2） | 内置自测 |
| 安全策略兜底 | `ai_bot.py::safety_filter`（L1961-2033）+ 阈值常量 `_FUEL_PIT=5.0`/`_FUEL_CAUTION=15.0`/`_DMG_NO_ATTACK=8000`/`_DMG_DEFEND=9500`（L1944-1947）、`_BLOCK_TRIGGER_GAP=20.0`（L1036）、`_START_CAUTION_DIST=150.0`（L914） | 无 pytest（**P0 缺口，见 §5.3**） |
| Granite prompt 构建/解析 | `_build_strategy_prompt`（L2085）/`_parse_strategy_response`（L2108） | 无 pytest；内置自测部分覆盖 |
| 策略去抖动 | `_next_debounced_strategy`（L2124，纯函数，标注"safe to unit test without a Granite connection"） | 无 pytest（明确写了适合测但没人写） |
| Granite 异步调用/回退 | `GraniteStrategist`（L2151，`.tick`/`._debounce`/`._call_granite`，基于 `telemetry_common.LatestTaskRunner`） | 无 |
| 心跳上报（客户端） | `BotStatusReporter`（L2261，独立线程，`urlopen(timeout=0.4)`，异常吞掉） | 无 |
| 心跳接收/健康判定（服务端） | `midware/runtime.py::bot_status_service`（`BotStatusService`），`GET/POST /api/bot/status`（L1406-1429） | `tests/integration/test_bot_heartbeat.py`、`tests/unit/test_bot_status_service.py`、`tests/unit/test_bot_clients.py` — **done，但只测服务端**，不测 `ai_bot.py` 客户端 |
| 策略请求（服务端） | `POST /api/bot/strategy`（`runtime.py::request_bot_strategy`，L1433） | `tests/unit/test_strategy_decision.py` — **done，但只测服务端 endpoint**，不测 `ai_bot.py` 里发起请求/解析响应/降级的那一半 |
| 主循环编排 | `run_bot`（L2317） | 无（需要真实/伪造 `scr_server` 才能跑） |

**结论**：方向四在"服务端"（midware 里的 `/api/bot/status`、`/api/bot/strategy`）测试覆盖已经和
方向三差不多完整；但方向四特有的、真正决定车会不会开出赛道或撞墙的那部分——`ai_bot.py` 本体
（SCR 协议、底层控制、`safety_filter`、`GraniteStrategist`、`run_bot`）——**目前零 pytest 覆盖**，
只有一个跑起来会打印 `ALL TESTS PASSED` 但不接入 CI、不产出 JUnit、不算通过率的内置自测脚本。
这是工作包 A 要补的最大缺口，也是和 commentary 当年"74/74 all done"起点最大的不同。

## 3. 建议交付目录

```text
tests/
  bot/
    test_scr_protocol.py          # parse_scr_state / format_scr_control 边界与容错
    test_control_logic.py         # 换挡 / ABS / TCL / 刹车距离 / compute_control 决策边界
    test_recovery.py              # 出界恢复 / 卡死自救
    test_safety_filter.py         # 优先级链 + 阈值边界（T-ε/T/T+ε）
    test_granite_strategy.py      # prompt 构建 / 响应解析 / 去抖动（纯函数，无需真实 Granite）
    test_granite_strategist_runtime.py  # GraniteStrategist：mock LatestTaskRunner / HTTP，覆盖超时/异常/空响应/回退
    test_status_reporter.py       # BotStatusReporter：mock urlopen，覆盖网络失败不阻塞主循环
    test_run_bot_integration.py   # 用 fake ScrClient（本地 UDP echo 或直接注入 state 序列）跑一小段 run_bot
    fixtures/

evaluation/
  bot/
    README.md
    config.example.yaml
    schemas/
    scripts/
      match_strategy_decisions.py   # 对应 commentary 的 match_events.py
      analyse_control_latency.py    # 对应 analyse_latency.py
      analyse_stability.py
      validate_experiment_data.py
      generate_report_tables.py
    templates/
      ground_truth_strategy_template.csv
      detected_strategy_template.csv
      lap_performance_template.csv
      control_latency_template.csv
      stability_run_template.csv
      fault_recovery_template.csv
    sample_data/
    results/

docs/
  bot_test_matrix.md            # commentary_test_matrix.md 的对应物，实施后再生成（记录代码行号/术语映射/已修缺陷）
  bot_experiment_protocol.md    # commentary_experiment_protocol.md 的对应物
  bot_fault_injection_protocol.md
```

不要提交大型录像、真实模型文件或个人信息；`evaluation/bot/` 与 `evaluation/commentary/` 同级复用
现有 `.gitignore` 规则——commentary 那边曾经因为一条 `*.csv` 的兜底规则把真实实验数据吞掉过
（见 `docs/commentary_test_handoff_2.md` §0），先确认 `evaluation/bot/results/*.csv` 不会被同样
规则误伤。

## 4. 工作包 A：功能正确性自动化测试

### 4.1 SCR 协议层（`test_scr_protocol.py`）

- `parse_scr_state`：完整合法包；空字符串/不完整包返回 `None`；`opponents` 少于 36 个时按
  L2586 附近内置自测已验证的规则补齐（padding=200.0）；非数字/越界字段。
- `format_scr_control`：正常范围；`accel`/`brake`/`steer` 越界时的 clamp 行为（内置自测已给出
  `accel=2.0→1.000`、`brake=-1.0→0.000`、`steer=5.0→1.000`、`focus=200→90` 的具体断言，直接照抄
  成 pytest 参数化用例）。
- `ScrClient`：不连接时的实例化/`close()`；`connect()` 握手失败（服务器不可达）的行为；
  `receive_state()` 超时返回 `{}`（区别于连接结束返回 `None`，见 `run_bot` 里对这两种返回值的
  不同处理，L2388-2399）——这个区分本身就该有专门测试，因为代码注释里明确写了"错误地对超时
  重发控制包会导致车在起步阶段执行过期指令"这个真实 bug 教训。

### 4.2 底层控制逻辑（`test_control_logic.py`）

- 换挡：`_auto_gear`/`_gear_from_speed`/`_gear_shift` 在换挡转速边界附近的 `T-ε/T/T+ε` 三点测试。
- ABS/TCL：`_apply_abs`/`_apply_tcl` 在轮速突变边界的介入/不介入。
- `_brake_dist`：给定已知物理参数，验证刹车距离公式的输出量级合理（回归测试，锁定当前实现，
  防止后续调参误改物理常数）。
- `compute_control`：针对每种 `strategy`（ATTACK/NORMAL/DEFEND/SAVE_FUEL/PIT/BLOCK）分别构造
  典型 state，断言输出的 accel/brake/gear/steer 落在该策略应有的行为范围内（例如 PIT 策略下
  `accel` 应明显低于 ATTACK 策略下的 `accel`，不要求逐 bit 精确匹配，避免测试和实现耦合过死）。

### 4.3 出界恢复（`test_recovery.py`）

- `_recovery_control` 在 `track_pos` 超出边界时被触发的条件；`_recovery_steer`/`_stabilize_action`
  的转向方向是否指向赛道内侧（用几组对称的越界方向输入，断言转向符号相反）。
- `_pursuit_target`：赛道数组为空/单值/正常时的返回值。

### 4.4 安全策略 `safety_filter`（`test_safety_filter.py`，**P0，本工作包重点**）

`safety_filter` 是一个纯函数（无 I/O），文档字符串本身就写了"优先级从高到低，第一条匹配即返回"
——这是最适合、也最需要写详尽边界测试的地方，因为它是 Granite 出错/瞎说时车不会真的开进墙里
的唯一保障。按代码里的优先级链（L1978-2032）逐条覆盖，每条阈值做三点边界：

| 优先级 | 规则 | 阈值 | 边界测试点 |
|---|---:|---|---|
| 1 | 策略不在 `_GRANITE_STRATEGIES` 内（含 Granite 直接返回 `"BLOCK"` 的情况，验证系统专用策略不能被模型触发）→ `NORMAL` | — | `None`、空串、`"BLOCK"`、`"TURBO"`（未知值） |
| 2 | 燃油过低 → `PIT`，且**无视 Granite 说了什么** | `fuel < 5.0` | `fuel=5.0-ε/5.0/5.0+ε`，同时固定 strategy 为 ATTACK 验证仍被压成 PIT |
| 3 | 严重损伤 → `DEFEND` | `damage >= 9500` | `9500-ε/9500/9500+ε` |
| 4 | 损伤较高时禁止 ATTACK → `NORMAL` | `damage >= 8000 and strategy == ATTACK` | `8000-ε/8000/8000+ε`；且验证非 ATTACK 策略（如 DEFEND）在同一损伤区间不受此条影响 |
| 5 | 燃油偏低时禁止 ATTACK → `NORMAL` | `fuel < 15.0 and strategy == ATTACK` | `15.0-ε/15.0/15.0+ε` |
| 6 | 后方来车过近 → `BLOCK`，但仅当车况健康且**不在起步阶段** | `bgap < 20.0`（`_rear_gap`），且 `damage < 8000 and fuel >= 15.0`，且 `dist_raced >= 150.0` | `bgap` 三点边界 × 三个门控条件各自的开/关组合（起步阶段 `dist_raced < 150.0` 时即使 `bgap` 很小也不能触发 BLOCK——这是代码注释里明确提到的真实 bug 修复，2026-08-07 发现"起步阶段两排并列发车导致每个邻车都在触发距离内"，必须有回归测试锁死） |
| — | 优先级互斥：同时满足燃油过低和损伤过高时，`PIT`（优先级 2）必须赢过 `DEFEND`（优先级 3） | — | 构造同时触发多条规则的 state，断言只返回优先级最高的那个结果 |

### 4.5 Granite 策略层

- `_build_strategy_prompt`：给定 state，断言输出的 payload JSON 包含预期字段且做了合理舍入/压缩
  （不测 prompt 文案本身，只测数据结构不丢字段）。
- `_parse_strategy_response`：合法 JSON；缺 `strategy` 字段；`strategy` 值不在
  `_GRANITE_STRATEGIES` 内（含模型自己编造出 `"BLOCK"` 的情况——必须被拒绝，回落到 `NORMAL`，
  这条和 §4.4 优先级 1 是同一个安全约束的两处实现，都要测）；非 JSON 文本；空响应。
- `_next_debounced_strategy`：这是代码注释里明确写"纯函数，可以在没有 Granite 连接的情况下安全
  单测"的函数，却是当前完全没人测的——覆盖：候选未达到 `_STRATEGY_CONFIRM` 次数前不切换；候选
  和上次不同时计数器重置为 1 而不是累加；候选达到确认次数后切换且状态清零；`proposed == active`
  时直接短路返回不切换。
- `GraniteStrategist`（mock 掉 `LatestTaskRunner`/HTTP 调用）：
  - `tick()` 在 `_interval` 未到时不提交新请求；到时后提交，且提交是非阻塞的（不等待结果）；
  - Granite 调用抛异常/超时 → `fallback=True`，`last_error` 有值，**且继续返回上一次的 `_last_strategy`
    而不是抛出异常中断主循环**（这是"模型挂了车不能停"的核心保证，必须有测试）；
  - Granite 恢复正常响应后 `fallback` 复位。
- `_call_granite`：mock `urllib.request.urlopen`，覆盖连接失败/超时（验证 `_GRANITE_TIMEOUT=30.0`
  确实大于 midware 自身 30s 模型超时预算之和，否则会出现"bot 先放弃、日志却显示 midware 还在处理"
  的假故障——这条本身更适合作为一条集成假设的文档化测试，断言两个常量的大小关系而不是行为）。

### 4.6 Runtime 集成/故障处理

- `BotStatusReporter`：mock `urlopen`，验证网络异常被吞掉不传播（`except Exception: pass`，
  L2299 附近）、`close()` 时最终发送一次 `connected: False` 且不无限阻塞主线程。
- `POST /api/bot/status` / `POST /api/bot/strategy` 在 `bot` feature 被禁用时返回 409（已有测试，
  复核即可，不必重写）。
- 非法 `/api/bot/status` body（现有 `test_bot_heartbeat.py` 已覆盖部分，检查是否需要补充
  `speed` vs `speed_kmh` 字段兼容性分支的测试，见 `runtime.py:1414-1415`）。

### 4.7 两个必须重点验证的问题（对应 commentary §5.5 的强制回归测试）

#### `safety_filter` 必须是压在 Granite 输出之上的最后一道关卡，不能被绕过

1. 构造一个 `GraniteStrategist`，mock 其网络层使其固定返回 `"ATTACK"`；
2. 构造一个 `damage=9999`（远超 `_DMG_DEFEND`）的 state；
3. 依次调用 `strategist.tick(state)` 拿到 raw strategy，再喂给 `safety_filter(raw, state)`；
4. 断言最终结果是 `DEFEND`，**不是** `ATTACK`。

只检查 `safety_filter` 单独调用时的行为不算通过——必须像这样把 `GraniteStrategist.tick()` 的
输出接到 `safety_filter` 的输入，验证两者串联后的真实调用路径（`run_bot` 主循环里就是这么串的，
见 docstring 里的 usage 示例），因为如果有人以后重构成"Granite 直接返回终态、`safety_filter`
只在部分分支调用"，这条测试才能抓到。

#### `BLOCK` 策略必须是 system-only，无法被 Granite 的文本输出直接触发

1. mock Granite 返回 `'{"strategy":"BLOCK","reason":"..."}'`；
2. 经 `_parse_strategy_response` 解析；
3. 断言解析结果被强制回落为 `NORMAL`，**不是** `BLOCK`；
4. 再单独验证 `BLOCK` 只能通过 §4.4 优先级 6 的 `_rear_gap` 规则由 `safety_filter` 自己产生。

代码注释里三处（L1980、L2027、L2119）反复强调这个约束，说明这是一个曾经或容易踩的坑，值得
两条独立测试（parse 层 + filter 层）而不是一条，防止将来只改了其中一处校验。

### 4.8 自动化结果输出

复用 commentary 的做法：`pytest tests/bot --junitxml=...`，写一个 `tools/bot_test_report.py`
（可直接照抄 `tools/commentary_test_report.py` 的汇总逻辑）产出：

| Test category | Tests | Passed | Failed | Skipped | Pass rate |
|---|---:|---:|---:|---:|---:|
| SCR protocol | | | | | |
| Control logic | | | | | |
| Recovery | | | | | |
| Safety filter | | | | | |
| Granite strategy | | | | | |
| Runtime integration | | | | | |
| Total | | | | | |

## 5. 工作包 B：真实驾驶表现与安全策略准确率

单元测试只能证明代码按规则运行，不能证明这些规则在真实赛道上真的让车跑得又快又安全——这一点
和 commentary 完全一样，只是评测对象从"文字解说准不准"换成"车开得对不对"。

### 5.1 设计

- 2 条不同赛道 × 每条 3 个 session = 6 个，每次 8–15 分钟（比 commentary 略长，因为要跑完整圈才
  能统计圈速/完赛率），人工驾驶 **或** bot 自驾均可，但两类场景要分开报告：
  - **人工驾驶场景**（复用 commentary 工作包 B 的驾驶方式）：主动制造低燃油、高损伤、被追尾等
    情况，人工标注"此时刻理想策略应为 X"，与 `safety_filter`/`GraniteStrategist` 实际给出的策略
    比较——这是本工作包的核心，对应 commentary 的事件检测 P/R/F1。
  - **bot 自驾场景**（`python ai_bot.py --bot [--granite]`）：不需要人工标注策略，改为统计客观
    驾驶质量指标（见 5.3）。

### 5.2 CSV schema（策略准确率部分，对应 commentary 的 ground_truth/detected_events）

```csv
session,timestamp_s,fuel_L,damage,rear_gap_m,expected_strategy,annotator
SA1,42.3,12.1,0,35.0,NORMAL,A1
SA1,58.7,3.8,0,35.0,PIT,A1
```

```csv
session,timestamp_s,raw_granite_strategy,filtered_strategy,source
SA1,42.5,ATTACK,NORMAL,safety_filter
SA1,58.9,ATTACK,PIT,safety_filter
```

匹配规则：同一 session、时间戳落在人工标注窗口 ±2 秒内（比 commentary 的 ±1 秒宽，因为策略
是"状态"不是"瞬时事件"，容忍一点延迟），比较 `filtered_strategy` 与 `expected_strategy` 是否一致。
按策略类型（ATTACK/NORMAL/DEFEND/SAVE_FUEL/PIT/BLOCK）分别算 Precision/Recall/F1，Overall 用
micro-average——公式和 commentary 一模一样，`evaluation/bot/scripts/match_strategy_decisions.py`
可以直接从 `evaluation/commentary/scripts/match_events.py` 改字段名得到。

### 5.3 客观驾驶质量指标（bot 自驾场景，commentary 没有对应物，是方向四特有的）

- **完赛率**：session 内是否正常完成设定圈数而非中途卡死/被判 DNF。
- **出界恢复成功率**：`track_pos` 越界后，在 N 秒内回到赛道内的比例（`_recovery_control` 的
  真实效果指标）。
- **碰撞频率**：`damage` 单调增量事件次数 / 每公里（越低越好，可与不同 strategy 分布做交叉分析）。
- **圈速一致性**：同一赛道多个 session 的圈速方差，用于判断 Granite 策略切换是否引入了不必要的
  速度波动。
- **策略切换频率**：`GraniteStrategist._debounce` 触发的切换次数/每分钟，过高说明 `_STRATEGY_CONFIRM=1`
  在当前赛道下"抖动"（flapping，代码注释里已经承认这是已知的 trade-off），该指标把这个已知
  trade-off 变成可测量的数字而不是主观印象。

### 5.4 验收目标（讨论用，不是倒推结果的门槛）

- Overall strategy Precision/Recall/F1 ≥ 0.80，与 commentary 一致的量级；
- `PIT`/`DEFEND` 这类安全相关策略的 Recall ≥ 0.90（比 commentary 的"关键事件 ≥0.70"更严格，
  因为漏判这里的后果是车撞墙而不是解说少一句话）；
- 出界恢复成功率 ≥ 90%；
- 碰撞频率相比"无 `safety_filter`"的对照组（若条件允许跑一组关闭安全网的对照实验）应显著更低。

## 6. 工作包 C：端到端延迟

这里比 commentary 多一层——**控制环本身的实时性**，commentary 完全没有这个约束（文字解说慢一秒
用户感知不强），但 bot 如果控制环跟不上 `scr_server` 的 tick 就会真的开不好车，代码注释里已经
记录过一次真实教训（错误地对超时重发导致"车在起步阶段执行了~30秒的过期指令"，`run_bot` L2393-2398）。

### 6.1 埋点定义（两条独立链路）

**链路一：控制环（每帧，硬实时）**

- `u0_scr_state_received`：`ScrClient.receive_state()` 返回非空 state 的时刻；
- `u1_control_computed`：`compute_control`（含 `safety_filter`）返回控制字符串的时刻；
- `u2_control_sent`：`ScrClient.send_control()` 调用完成的时刻。

```text
compute_latency = u1 - u0
send_latency    = u2 - u1
frame_latency   = u2 - u0
```

TORCS `scr_server` 的仿真步长是 **0.02 秒**（代码里里程计校准逻辑直接写死了这个值，
`travel = speed_x / 3.6 * 0.02`，见 L2405-2410 附近），这是本链路延迟目标的硬约束依据，不是
凭空定的：`frame_latency` 的 P99 必须明显小于 20ms，否则每帧都会退化成"复用上一帧控制"，
在弯道/避让场景下是安全隐患。

**链路二：Granite 策略调用（每 5 秒一次，软实时，与 commentary 的 t0-t3 完全对应）**

- `g0_state_snapshot`：`GraniteStrategist.tick()` 决定提交请求的时刻；
- `g1_first_byte`：midware `/api/bot/strategy` HTTP 响应开始接收的时刻（若走流式可细分到 first token，
  当前 `request_bot_strategy` 是同步一次性返回，暂无 first-token 概念，除非改造成流式，见 §6.3 备注）；
- `g2_response_complete`：完整响应解析完成的时刻；
- `g3_strategy_applied`：`_debounce` 决定是否切换 `_last_strategy` 的时刻。

```text
granite_rtt        = g2 - g0
debounce_overhead   = g3 - g2   # 通常接近 0，除非卡在候选计数阶段
```

### 6.2 执行方法

- 控制环延迟：**不需要真实 TORCS**——可以用工作包 A 里已经搭好的 fake `ScrClient`/直接函数调用
  循环跑 1000+ 帧，测量纯计算耗时分布（`u1-u0`），这是纯 CPU 开销，不受网络影响，适合放进
  L2 自动化集成测试而不是"人工实验"，与 commentary 的延迟测试（必须真实驱动 Granite）性质不同；
  真实 UDP 收发的 `u2-u1`/`frame_latency` 仍需要一次真实/仿真的 `scr_server` 环境验证。
- Granite RTT：30 次独立策略请求，固定配置（模型、量化、`temperature`、`max_tokens=80`），
  与 commentary 工作包 C 相同的统计口径（count/min/mean/median/stdev/P95/max/failure count）。

### 6.3 论文结果表

| Stage | N | Median | P95 | Maximum | Failures |
|---|---:|---:|---:|---:|---:|
| Control loop (compute) | | | | | |
| Control loop (frame, UDP) | | | | | |
| Granite strategy RTT | | | | | |

### 6.4 验收目标（讨论用）

- Control frame median ≤ 5ms，P95 ≤ 15ms（留出对 20ms 步长的安全边际，不是卡在 20ms 上）；
- Granite RTT median ≤ `_STRATEGY_INTERVAL`（5.0s）的一半，否则请求会在下一个 tick 到来前排不完队；
- Granite 请求失败率 < 5%，且失败时必须落到 `fallback=True` 分支而不是让主循环崩溃
  （这条和工作包 D 的 RT 故障注入是同一件事的两种验证方式：这里测延迟分布里的失败比例，
  工作包 D 测失败发生后的具体行为）。

## 7. 工作包 D：稳定性与故障恢复

### 7.1 持续运行实验

3 × 20 分钟（比 commentary 的 30 分钟略短，因为一场赛程本身有限，可用多个 session 拼够时长）
bot 自驾（`--bot --granite`），每次包含：

- 正常持续驾驶；
- 一次 midware 重启（验证 `ScrClient` 与 TORCS 的连接本身不依赖 midware，重启 midware 不应该
  让车立刻失控，只是 Granite 策略请求会失败几次直到 midware 起来）；
- 一次 Granite/LM Studio 中断与恢复；
- 一次人为让 TORCS 侧短暂无响应（模拟 `receive_state()` 连续超时）。

记录：duration、control frames processed、strategy requests、granite failures、
safety_filter interventions（按类型计数，例如 PIT 强制触发了几次）、collisions、
off-track excursions、recoveries、unhandled exceptions/crashes、CPU/内存峰值。

### 7.2 故障注入（对应 commentary 的 RT-01..RT-12，改造成 bot 语境）

| ID | Fault | 预期行为 |
|---|---|---|
| RB-01 | Granite/midware 不可达 | `GraniteStrategist.fallback=True`，`ai_bot.py` 继续用上一次已确认的 `_last_strategy` 开车，**不停车、不崩溃** |
| RB-02 | Granite 恢复 | 无需重启 `ai_bot.py`，下一个 5s tick 自动恢复正常调用 |
| RB-03 | midware 完全下线（不只是 Granite） | `/api/bot/status`/`/api/bot/strategy` 均失败；`BotStatusReporter` 的异常被吞掉（L2299）不影响主循环；`GraniteStrategist` 同 RB-01 走 fallback |
| RB-04 | midware 恢复 | 心跳/策略请求自动恢复，`bot_status_service` 端从 `disconnected` 回到 `healthy`（对应 `tests/integration/test_bot_heartbeat.py` 里已验证的服务端超时判定，此处验证的是客户端真的会在 midware 恢复后立刻重新发送） |
| RB-05 | TORCS/`scr_server` 连接中断（`receive_state()` 返回 `None`） | `run_bot` 打印 "Race ended" 并干净退出主循环，不是卡死或抛未捕获异常 |
| RB-06 | `receive_state()` 持续超时（返回 `{}`，服务器仍在但数据没来） | **不重发上一次控制包**（这是 L2393-2398 注释里明确记录过的真实 bug 教训，必须有回归测试锁死），继续等待而不是崩溃 |
| RB-07 | 非法/畸形 SCR 状态包 | `parse_scr_state` 返回 `None` 或安全丢弃，不让异常向上传播炸穿 `run_bot` 主循环 |
| RB-08 | Granite 返回畸形 JSON / 编造出 `_ALL_STRATEGIES` 之外的策略名 | `_parse_strategy_response` 回落 `NORMAL`；即便回落，`safety_filter` 仍是最后一道关卡（§4.7 已覆盖，此处是端到端真实调用路径的复核） |
| RB-09 | 高频策略切换（构造持续在阈值边界震荡的 state） | `_next_debounced_strategy` 按 `_STRATEGY_CONFIRM` 门槛工作，不会每帧都切换导致车辆行为抖动；`LatestTaskRunner` 保证不会堆积未完成的 Granite 请求 |
| RB-10 | `bot` feature 被禁用 | `/api/bot/status`/`/api/bot/strategy` 返回 409；`ai_bot.py` 侧应能识别这个响应并合理降级（若当前代码没有对 409 的专门处理，记为已确认缺口而不是编造通过） |

恢复时间定义、"故障不适用时如实说明"的规则、5 次重复/故障的要求，均与
`docs/commentary_fault_injection_protocol.md` 相同，照抄格式即可。

## 8. 配置、隐私与可复现性

复用 commentary 的 `evaluation/commentary/config.example.yaml` 结构，`evaluation/bot/config.example.yaml`
至少记录：`git_commit`、`torcs_version`、`middleware_version`、`granite_model`/`quantisation`、
`lm_studio_version`、`operating_system`/`cpu`/`gpu`、`bot_strategy_interval_s`（`_STRATEGY_INTERVAL`）、
`strategy_confirm`（`_STRATEGY_CONFIRM`）、`track`、`opponents`、`granite_enabled`（bool，区分
纯规则驾驶 vs. Granite 策略场景，两者的驾驶质量指标不能混在一起统计）。

## 9. 必须执行的验证

1. 新增 Bot 自动化测试全部跑绿；
2. 仓库已有相关测试（`test_bot_heartbeat.py`/`test_bot_clients.py`/`test_bot_status_service.py`/
   `test_strategy_decision.py`）不因本次改动回归；
3. `ai_bot.py` 内置 `_run_tests()` 仍然全绿（不要在迁移到 pytest 的过程中让它被悄悄删掉——在
   两者稳定共存一段时间、确认 pytest 覆盖等价后再考虑精简）；
4. `python -m compileall` / `tools/run_tests.sh` 的 L0-L2 保持通过；
5. `evaluation/bot/sample_data/` 验证所有分析脚本可跑通；
6. 检查真实 `evaluation/bot/results/` 不含伪造数据，且没有被 `.gitignore` 误伤（参考
   `docs/commentary_test_handoff_2.md` §0 的前车之鉴）。

## 10. 最终交付物

- Bot 自动化测试套件（`tests/bot/`）；
- 必要且最小的可测试性改造（例如给 `run_bot` 加可注入的 fake `ScrClient`，给延迟埋点加 opt-in
  logging hook，参照 `midware/latency_log.py` 的做法）；
- `evaluation/bot/` 下的 schema、模板、脚本、单元测试；
- `docs/bot_test_matrix.md`（实施后生成，记录真实代码行号、术语映射、发现并修复的缺陷——按
  `docs/commentary_test_matrix.md` 的格式）；
- `docs/bot_experiment_protocol.md` / `docs/bot_fault_injection_protocol.md`（人工实验操作手册）；
- 示例数据（`SAMPLE_*.csv`）与实际执行结果摘要，明确区分两者。

## 11. 推荐执行顺序

- [x] 审计仓库，复核本文件第 2 节的代码行号（迁移过程中行号大概率已变）
- [x] 运行 `python ai_bot.py` 内置自测，记录基线
- [x] 补齐 §4.1-4.3：SCR 协议 + 底层控制 + 出界恢复
- [x] 补齐 §4.4：`safety_filter` 全部优先级/阈值边界（P0，优先级最高）
- [x] 补齐 §4.5-4.6：Granite 策略层 + Runtime 集成
- [x] 完成 §4.7 两个强制回归测试（safety_filter 压制 Granite；BLOCK 不可被模型直接触发）
- [ ] 建立 `evaluation/bot/` 目录、CSV schema、匹配脚本
- [ ] 实现控制环延迟埋点（无需真实 TORCS 即可测的部分先做）
- [ ] 人工执行工作包 B（真实驾驶场景，策略准确率）
- [ ] 人工执行工作包 C 的 Granite RTT 部分（需要真实 LM Studio）
- [ ] 人工执行工作包 D（endurance + RB-01..RB-10 故障注入）
- [ ] 生成 `docs/bot_test_matrix.md`，回填第 2 节代码行号并记录实际状态
- [ ] 更新 `docs/testing-plan.md`：把 Bot 的新自动化测试计入 L1/L2，把人工实验计入 L4/L5 索引

## 12. 实施状态（2026-08-12，第二轮更新）

工作包 A 已全部实施完成，**186/186 通过**（`.venv/bin/python -m pytest tests/bot -q`，约 8 秒，
无需真实 TORCS/Granite/网络）：

| 文件 | 对应本文件章节 | 用例数 | 说明 |
|---|---|---:|---|
| `tests/bot/test_scr_protocol.py` | §4.1 | 12 | 移植自 `ai_bot.py` 内置自测，新增非数字字段/`ScrClient` 未连接时的报错路径 |
| `tests/bot/test_control_logic.py` | §4.2 | 32 | 换挡/ABS/TCL/`_brake_dist`/直道与弯道 `compute_control` 回归，移植自内置自测 |
| `tests/bot/test_recovery.py` | §4.3 | 26 | 出界重入/滞回/撞击后稳定化/掉头/卡死自救/纯追踪转向，移植自内置自测 |
| `tests/bot/test_safety_filter.py` | §4.4（P0） | 31 | 6 条优先级规则逐条 T-ε/T/T+ε 边界 + 优先级互斥，**新增测试，非内置自测移植** |
| `tests/bot/test_granite_strategy.py` | §4.5 | 15 | prompt 构建/响应解析/去抖动纯函数，移植 + 补充空输入/缺字段用例 |
| `tests/bot/test_granite_strategist_runtime.py` | §4.5 | 9 | `GraniteStrategist.tick()` 的成功/失败/回退/去抖动集成，**新增**，用直接注入 `WorkerResult` 到 `LatestTaskRunner` 队列的方式避免真实线程时序竞争 |
| `tests/bot/test_status_reporter.py` | §4.6 | 5 | `BotStatusReporter` 网络异常吞掉不传播、`close()` 发送最终下线状态，**新增** |
| `tests/bot/test_safety_integration.py` | §4.7 | 3 | 两个强制回归测试：`safety_filter` 压制 Granite 输出；`BLOCK` 全链路不可被模型触发 |
| `tests/bot/test_run_bot_integration.py` | §4（原缺口） | 9 | `run_bot()` 主循环编排：握手/逐帧发控制包/超时帧不重发/`None` 帧干净退出/`KeyboardInterrupt` 处理/`reporter` 资源释放。**新增**，给 `run_bot()` 加了一个最小可测试性改造——见下方"生产代码改动" |
| `tests/bot/test_track_map_lookahead.py` | §4（原 P1 缺口） | 10 | 赛道地图前瞻：mapped 弯前刹车、entry-line 偏置、brake-point 模式、5 个 trust 门控。移植自内置自测，需要 `track_model.py` 可用（否则整个类 `@skipUnless`） |
| `tests/bot/test_traffic_and_launch.py` | §4（原缺口） | 25 | 侧向来车避让（含收敛门控/边缘 room taper/standoff breaker）、起步阶段谨慎+离合渐进、前车跟随超车偏置（含转角跳变拒绝、下一弯方向 tiebreak）、`BLOCK` 转向偏置、boxed-in 跟车刹车上限。移植自内置自测 |

**生产代码改动**（唯一一处，最小化）：`ai_bot.py::run_bot()` 新增关键字参数
`client: ScrClient | None = None`——传入时直接使用该对象而不是内部
`ScrClient(host, port)`，`with (client if client is not None else ScrClient(host, port)) as client:`
一行改动，循环体后面的代码路径与真实驾驶完全一致。这正是 §10 最终交付物里
预先写好要做的"给 `run_bot` 加可注入的 fake `ScrClient`"。`python ai_bot.py` 内置自测
（不调用 `run_bot`）改动前后均确认全绿，未受影响。

复核过程中发现一处需要记录的隐藏耦合（不是缺陷，是文档化说明）：`compute_control` 的
"滞回"（hysteresis）行为依赖模块级 `_recovering` 标志跨帧持续——`ai_bot.py` 内置自测里
两个相邻断言共享同一段未重置的状态，写成独立 pytest 用例时必须显式重放"先出界一帧、
再回到边缘"两步序列，否则断言会失败（已在 `test_recovery.py` 的
`OffTrackReentryTests.test_hysteresis_holds_recovery_pace_just_back_over_the_edge` 里
写清楚原因）。

**运行环境说明**：这台机器上找不到可用的 Windows 侧 Python（`python`/`py` 均未安装
真实解释器），而 `tools/testing-plan.md` 假设的 `~/summer-project/F1-simulator`
在 WSL 里也不存在——WSL 里实际的仓库在 `~/F1-simulator`，且是另一个独立、带大量未提交
TORCS 构建改动的检出，与本次编辑的 `/mnt/c/Users/abcdz/Desktop/ibm/F1-simulator`
不是同一份工作区，未触碰。测试改为在 WSL 里对 `/mnt/c/.../F1-simulator`
新建一个 `.venv_wsl`（`python3 -m venv .venv_wsl && pip install -r requirements-core.txt`）
执行——后续会话如果要复现，认准这个路径组合，不要误用 `~/F1-simulator`。

**跑全量 `tests/unit` + `tests/integration` 时观察到的、与本次改动无关的既有问题**（未修复，
超出本次工作包 A 范围，供后续处理）：

1. `tests/integration/*` 43 个用例报 `ERROR at setup`：`OSError: [Errno 98] Address already
   in use`，端口 3101。原因是这台机器上已经有一个独立的 `midware/.venv/bin/python -m
   midware.app`（pid 会变）在真实占用 3101/8880——与本次新增的 `tests/bot/*` 完全无关
   （那些用例不碰网络/端口），是环境里已经在跑的另一个进程。按
   `docs/testing-plan.md` §1 的指引，正常做法是先 `pkill -f midware.app` 确认端口空闲，
   但这次没有主动结束它，因为不确定是不是别人正在用的会话——建议先确认再清理。
2. `tests/unit/test_model_broker.py::ModelBrokerTests::test_latest_stale_key_supersedes_queued_job`
   单独跑也失败（`asyncio.exceptions.CancelledError`），与 `ai_bot.py`/bot 测试无关，是一个
   依赖 `await asyncio.sleep(0)` 做同步点的时序敏感测试，在这台机器上偶发。未修改、未修复。

第二轮补测过程中还发现一个环境耦合，已在 `test_run_bot_integration.py` 里处理：
`run_bot()` 默认会调用 `load_track_model("auto")` 读取这台机器 `~/.torcs` 下的
raceman 配置——如果不显式传 `track="off"`，测试结果会依赖宿主机是否残留了某条赛道的
配置（这台机器上确实读到了一个真实的 "CG track 2"）。所有 `test_run_bot_integration.py`
里的 `run_bot()` 调用现在都固定传 `track="off"`，避免这个不确定性。

**第三轮补测（同一天）已经把上面剩下的两项也做完了**：

- 新增 `tests/bot/test_scr_client_network.py`（9 条）：用一个本地假 `scr_server`
  （纯 Python UDP 线程，不是真 TORCS）跑 `ScrClient` 真实的握手/`receive_state()`/
  `send_control()`——握手成功、握手无人应答时清晰失败（临时把
  `_HANDSHAKE_TIMEOUT`/`_HANDSHAKE_RETRIES` patch 小以免测试变慢）、本地 guard port
  被占用时报出"另一个 bot 实例"的明确错误、真实包解析、超时返回 `{}` 而不是
  `None`、多帧积压只取最新一帧、`***shutdown***`/`***restart***` 两种信号的不同
  处理、控制包原样送达。
- 新增 `tools/smoke_test_bot_status.py`：仿照 commentary 的
  `tools/smoke_test_commentary_queue.py`，真起一个 `python -m midware.app` 子进程 +
  一个自建的假 OpenAI 兼容模型端点，让真实的 `BotStatusReporter`/`GraniteStrategist`
  （不 mock 网络层）跟它对话——心跳状态真实落地、`close()` 真的发出下线状态、
  Granite 成功/真实 500 故障（服务端会转成 502 返回，是 midware 网关层的真实行为，
  记在 `bot_test_matrix.md` §5.4）、`bot` feature 禁用时两个客户端都优雅失败不崩溃。
  8/8 通过，多次运行稳定。不算进 `tests/bot/` 的 pytest 计数（跟它的 commentary 对应物
  一样，因为要起真实子进程/绑真实端口，手动跑：
  `.venv/bin/python tools/smoke_test_bot_status.py -v`）。

至此**工作包 A 的自动化/黑盒测试部分已经全部做完**（`tests/bot/` 186/186 +
`tools/smoke_test_bot_status.py` 8/8）。唯一还没做的是需要真实 TORCS + LM Studio
硬件的工作包 B/C/D（真实驾驶实验、延迟测量、稳定性与故障注入），以及前面提到的
端口 3101 占用问题（不影响本次交付，等确认后再清）。
