# AI Live Commentary 测试实施任务书

> 本文件用于直接交给 Codex、Claude Code 或其他编程 AI，在项目仓库中实施 AI Live Commentary 的测试体系。请把本文件放在仓库根目录后执行。

## 1. 你的角色与总目标

你是一名负责测试架构、Python 自动化测试和实验数据分析的高级软件工程师。请基于当前仓库的**真实实现**，为 AI Live Commentary 功能建立可复现、可运行、可用于硕士团队报告的客观测试体系。

本任务只评价以下四项：

1. 功能正确性；
2. 真实比赛事件检测准确率；
3. 端到端延迟；
4. 稳定性与故障恢复。

不要加入用户实验，也不要对解说文本进行主观质量评分。

项目仓库（如当前工作区尚未打开仓库，可据此确认项目）：

```text
https://github.com/JulianC-2778/F1-simulator.git
```

## 2. 必须遵守的工作原则

- 先阅读当前代码、测试、配置和文档，再决定修改方案；不要根据本任务书猜测文件名、类名或接口。
- 以当前检出的 commit 为唯一实现依据，并在报告中记录 commit hash。
- 只修改与 Commentary 测试、必要可测试性改造、日志和实验脚本直接相关的内容。
- 保留用户现有改动，不覆盖无关代码，不执行破坏性 Git 操作。
- 优先复用现有测试框架和项目惯例；Python 测试优先使用 `pytest`。
- 测试应验证公开行为或稳定接口，避免过度绑定内部实现。
- 外部依赖（Granite、LM Studio、WebSocket、TTS、时间）应通过 mock/fake 或依赖注入隔离。
- 自动化测试必须确定性运行，不应依赖真实 TORCS、真实模型服务或互联网。
- 不得伪造真实比赛、延迟、稳定性或故障恢复结果。
- 不得为了达到验收阈值而在看到结果后修改计算方法、匹配窗口或事件阈值。
- 如果任务书中的事件名称、模式或状态码与真实代码不一致，应以代码为准，并在最终报告中建立对应关系。
- 如果某项功能在当前实现中不存在，不要编造通过结果；应标为缺失、跳过或待实现，并解释证据。
- 若发现实现缺陷，可以做最小范围修复，但必须先增加能复现缺陷的失败测试，再修复并执行完整回归。
- 所有生成的 CSV、JSON 和报告都必须明确区分：`sample/demo`、`automated test`、`real experiment`。

## 3. 首先执行：仓库审计与实施计划

在写代码前完成以下工作：

1. 阅读仓库根目录的 `README`、`AGENTS.md`、依赖文件和测试配置；
2. 定位 Commentary 的正式入口、事件检测器、模式控制、优先级、冷却、去重、Granite 调用、WebSocket 广播和 TTS 集成；
3. 定位 telemetry 的来源、数据模型和时间字段；
4. 定位现有测试、fixtures、日志格式和 CI 配置；
5. 运行当前已有的相关测试，记录基线结果；
6. 检查是否已有可复用的录制 telemetry 或比赛日志；
7. 输出一个简短实施计划，然后继续完成实现，不要只停留在分析阶段。

审计后请建立或更新一份机器可读/可追踪的测试映射，至少包含：

| Requirement | Real code location | Existing test | New test or script | Status |
|---|---|---|---|---|
| Input validation |  |  |  |  |
| Event detection |  |  |  |  |
| Modes |  |  |  |  |
| Priority/pre-emption |  |  |  |  |
| Cooldown |  |  |  |  |
| Event deduplication |  |  |  |  |
| Text deduplication before display |  |  |  |  |
| Granite failure |  |  |  |  |
| WebSocket disconnect/reconnect |  |  |  |  |
| TTS failure isolation |  |  |  |  |

## 4. 建议交付目录

请先适配仓库现有结构。若仓库没有等价约定，可采用：

```text
tests/
  commentary/
    test_input_processing.py
    test_event_boundaries.py
    test_modes_priority.py
    test_cooldown_deduplication.py
    test_runtime_integration.py
    test_fault_handling.py
    fixtures/

evaluation/
  commentary/
    README.md
    config.example.yaml
    schemas/
    scripts/
      match_events.py
      analyse_latency.py
      analyse_stability.py
      validate_experiment_data.py
      generate_report_tables.py
    templates/
      ground_truth_template.csv
      detected_events_template.csv
      latency_template.csv
      stability_run_template.csv
      fault_recovery_template.csv
    sample_data/
    results/

docs/
  commentary_test_matrix.md
  commentary_experiment_protocol.md
```

不要提交大型录像、真实模型文件、个人信息或临时缓存。若项目已有统一的 `scripts/`、`docs/` 或 `results/` 目录，应合并到现有结构中。

## 5. 工作包 A：功能正确性自动化测试

### 5.1 输入处理

覆盖以下行为，并按真实数据模型调整字段：

- 空 telemetry：不触发事件且不崩溃；
- 缺少可选字段：使用默认值或安全忽略；
- 缺少必需字段：产生明确、可控的错误或安全丢弃；
- 非数字、NaN、Infinity、错误类型和越界值；
- 完整合法 telemetry：正常处理；
- 连续重复帧：不重复产生同一事件。

### 5.2 事件触发与边界

识别代码当前支持的全部 Commentary 事件。预期至少检查下列概念；若名称不同，请建立映射：

- `contact`；
- `position_change`；
- `off_track`；
- `lap_complete`；
- `battle`；
- `pace_surge`；
- `pace_update`（定时事件）。

对每个带阈值的规则执行三点边界测试：

```text
T - epsilon
T
T + epsilon
```

边界是否包含必须来源于代码或产品规范，不可自行假设。测试至少涵盖：伤害增量、排名变化、赛道位置、圈数变化、对手距离、速度/圈速变化和定时间隔。

### 5.3 模式、优先级、冷却和抢占

若代码支持这些模式，应验证：

- `off`：不生成或广播 Commentary；
- `interval`：只允许定时 Commentary；
- `event`：只允许事件 Commentary；
- `hybrid`：允许定时和事件 Commentary；
- 同一检测周期出现多个事件时采用真实优先级规则；
- 冷却期内相同事件不重复输出；
- 冷却结束后事件可再次输出；
- 高优先级事件能否抢占低优先级任务；
- 低优先级事件不得错误抢占高优先级任务；
- 同优先级事件按照代码定义的策略处理。

如果当前系统没有抢占机制，测试应反映真实设计，不要虚构行为；在测试矩阵中注明“不适用”或“尚未实现”。

### 5.4 Runtime 和故障处理

使用 mock/fake 测试：

- Commentary 启用与禁用；
- Granite 正常响应、超时、连接失败、异常响应和空响应；
- 流式 token 与完成事件的顺序；
- 没有 WebSocket 客户端时后端不崩溃；
- WebSocket 广播失败不会导致主循环崩溃；
- TTS 失败不破坏字幕链路；
- 非法配置产生明确错误；
- 高频事件不会产生未处理异常或无限任务堆积。

### 5.5 两个强制回归测试

#### 文本去重必须发生在用户可见广播之前

1. 将模型替换为固定 fake，使其连续返回完全相同文本；
2. 触发两个在业务上允许进入生成流程的事件；
3. 从 Overlay/WebSocket 消费者视角监听完成消息；
4. 断言重复文本只向用户发送一次。

只检查后台缓存或日志不算通过。如果第二条文本先广播后去重，先写失败测试，再做最小修复。

#### 45 词限制必须测量真实结果

如果 `max_words=45` 只存在于提示词，不能把它写成程序保证。请：

- 提供统一的英文词数统计函数并配套测试；
- 在实验分析脚本中输出样本数、超长数和违反率；
- 不要默认截断输出，除非当前产品需求明确要求强制截断；
- 将“提示约束”和“程序强制约束”在文档中区分开。

### 5.6 自动化结果输出

测试执行后生成 JUnit XML，并提供一个脚本或明确命令，把结果汇总为：

| Test category | Tests | Passed | Failed | Skipped | Pass rate |
|---|---:|---:|---:|---:|---:|
| Input processing |  |  |  |  |  |
| Event boundaries |  |  |  |  |  |
| Modes and priority |  |  |  |  |  |
| Cooldown and deduplication |  |  |  |  |  |
| Runtime integration |  |  |  |  |  |
| Fault handling |  |  |  |  |  |
| Total |  |  |  |  |  |

统计时必须说明 skipped 是否计入分母。建议 pass rate 使用：

```text
passed / (passed + failed)
```

同时单独报告 skipped，避免掩盖未覆盖功能。

## 6. 工作包 B：真实比赛事件检测准确率

### 6.1 人工实验协议

创建可操作的实验说明，但不要伪造实验结果：

- 2 条不同赛道；
- 每条赛道 3 个 session，共 6 个；
- 每次 5–8 分钟，总时长约 30–45 分钟；
- 同步记录 telemetry、系统事件日志和屏幕录像；
- 主动制造 contact、position change、off-track、lap complete、battle 和 pace surge；
- 每类尽量至少 10 个真实实例；
- `pace_update` 单独检查触发间隔，不计入事件 F1。

请提供开始采集、停止采集和为每个 session 命名的具体命令或界面步骤。所有时间戳必须说明采用的时钟、单位和 session 起点。

### 6.2 CSV schema

至少支持以下格式；可以增加字段，但不得删除匹配所需字段：

```csv
session,event_id,event_type,start_time_s,end_time_s,description,annotator
S01,GT0001,contact,42.300,42.800,collision with opponent,A1
```

```csv
session,detection_id,event_type,detection_time_s,priority,source
S01,DET0001,contact,42.700,90,commentary_engine
```

脚本必须验证：

- 必需列存在；
- 时间为有限非负数；
- `end_time_s >= start_time_s`；
- session 和 event type 可识别；
- ID 在各自文件内唯一；
- 空数据和格式错误会产生明确提示。

### 6.3 匹配算法

实现确定性的一对一事件匹配：

- session 相同；
- event type 相同；
- detection time 到人工标注事件区间的距离不超过 1.0 秒；
- 一个检测最多匹配一个 ground-truth；
- 一个 ground-truth 最多匹配一个检测；
- 多个候选存在时优先选择时间距离最小者，并使用稳定 tie-break；
- 输出 TP 配对明细、未匹配检测（FP）和未匹配真实事件（FN）。

不要使用会因输入行顺序不同而改变结果的贪心实现。可使用最小代价匹配，或为每个 session/event type 实现具有明确全局规则的一对一匹配。为重复候选、区间边界、恰好 ±1 秒、跨 session、错误类型和空输入编写单元测试。

### 6.4 指标

按事件类型计算：

```text
Precision = TP / (TP + FP)
Recall    = TP / (TP + FN)
F1        = 2 * Precision * Recall / (Precision + Recall)
```

当分母为零时不得静默输出误导性数值；请使用 `N/A` 或明确记录所采用的零除策略。

Overall 必须采用 micro-average：先汇总所有类别的 TP、FP、FN，再计算 P/R/F1。不要把各类别 F1 简单平均后称为 Overall。

输出论文可直接使用的 CSV 和 Markdown 表：

| Event | Ground truth | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Contact |  |  |  |  |  |  |  |
| Position change |  |  |  |  |  |  |  |
| Off-track |  |  |  |  |  |  |  |
| Lap complete |  |  |  |  |  |  |  |
| Battle |  |  |  |  |  |  |  |
| Pace surge |  |  |  |  |  |  |  |
| Overall (micro) |  |  |  |  |  |  |  |

项目目标（不是允许修改结果的条件）：Overall Precision、Recall 和 F1 分别不低于 0.80，关键事件 Recall 不低于 0.70。未达标时保留真实结果并输出误差明细。

## 7. 工作包 C：端到端延迟

### 7.1 埋点定义

在不改变核心行为的前提下，加入或复用结构化时间戳：

- `t0_telemetry_received`：相关 telemetry 到达 Middleware；
- `t1_event_detected`：事件被确认；
- `t2_first_token`：收到 Granite 第一个 token；
- `t3_ai_done`：完整模型响应完成；
- `t4_caption_displayed`：Overlay 确认字幕显示；
- `t5_tts_started`：TTS 开始，仅在最终产品启用 TTS 时记录。

同一条链路必须带有稳定的 `session_id`、`event_id` 和 `request_id/correlation_id`，避免用时间接近来错误拼接记录。

进程内耗时使用单调时钟；跨进程时间若使用 wall clock，必须说明同步方法。不得把不同时间基准直接相减。

### 7.2 计算项

```text
detection_latency       = t1 - t0
first_token_latency     = t2 - t1
generation_latency      = t3 - t1
caption_latency         = t4 - t0
tts_latency             = t5 - t0  # 仅启用 TTS 时
```

为缺失时间戳、乱序时间戳、重复 ID、失败请求和超时设计验证逻辑。失败和超时必须计入 failures，不得从样本中静默删除。

### 7.3 实验与统计

创建 30 次独立事件触发的实验流程，使用最终演示配置，固定：

- Git commit；
- Granite 完整模型与量化版本；
- LM Studio 版本；
- 硬件和操作系统；
- temperature、token limit、streaming；
- Commentary 模式和检测周期；
- TTS 开关。

输出 count、minimum、mean、standard deviation、median、P95、maximum 和 failure count。P95 必须使用文档中说明的固定计算方法，并有小样本单元测试。

论文表：

| Stage | N | Median | P95 | Maximum | Failures |
|---|---:|---:|---:|---:|---:|
| Event detection |  |  |  |  |  |
| First model token |  |  |  |  |  |
| Complete model response |  |  |  |  |  |
| Caption displayed |  |  |  |  |  |
| TTS playback, if enabled |  |  |  |  |  |

目标值仅用于讨论：Detection median ≤ 0.5 s、Detection P95 ≤ 1.0 s、First-token median ≤ 2.0 s、Complete-caption median ≤ 4.0 s、模型失败率 < 5%。

## 8. 工作包 D：稳定性与故障恢复

### 8.1 持续运行实验

提供可重复执行的 3 × 30 分钟 endurance test 流程。每次包括：

- 正常持续 telemetry；
- 多次连续事件；
- 一次 WebSocket/Overlay 断开与恢复；
- 一次 Granite/LM Studio 中断与恢复；
- 一次 telemetry/UDP 中断与恢复；
- 若最终启用 TTS，再注入一次 TTS 失败。

记录：

- duration；
- events detected；
- commentary requests；
- successful outputs；
- model failures/timeouts；
- duplicate user-visible displays；
- unhandled exceptions/crashes；
- reconnect/recovery time；
- CPU average/peak；
- memory initial/final/peak；
- 输出总数、超过 45 词数量和违反率。

成功率：

```text
successful outputs / commentary requests * 100%
```

### 8.2 故障注入

为以下场景提供安全、明确、可恢复的步骤，每种独立故障重复 5 次：

| ID | Fault | Expected behaviour |
|---|---|---|
| RT-01 | Granite unavailable | 报告受控错误，Middleware 继续运行 |
| RT-02 | Granite restored | 无需重启 Middleware，后续请求恢复 |
| RT-03 | WebSocket/Overlay disconnect | Middleware 不崩溃 |
| RT-04 | Overlay reconnect | 能接收新的字幕 |
| RT-05 | Telemetry/UDP interruption | 不生成虚假比赛事件 |
| RT-06 | Telemetry restored | 继续检测新事件 |
| RT-07 | Invalid telemetry | 安全丢弃、默认处理或明确报错 |
| RT-08 | Duplicate frames | 不产生重复用户可见解说 |
| RT-09 | High event frequency | 按真实优先级处理且无无限堆积 |
| RT-10 | TTS failure | 字幕链路仍可使用 |
| RT-11 | UDP port occupied | 启动失败信息明确，不静默失效 |
| RT-12 | Commentary disabled | 不再生成或广播新解说 |

恢复时间定义：

```text
first successful output after restoration - service restoration time
```

若某故障不适用于当前架构，应在报告中说明原因，不要制造虚假测试。

论文结果表：

| Fault condition | Trials | Successful recovery | Median recovery time | Crashes | Result |
|---|---:|---:|---:|---:|---|
| Granite unavailable/recovery |  |  |  |  |  |
| WebSocket disconnection |  |  |  |  |  |
| Telemetry interruption |  |  |  |  |  |
| Invalid telemetry |  |  | N/A |  |  |
| TTS failure, if enabled |  |  |  |  |  |

目标值仅用于讨论：90 分钟 Middleware 崩溃为 0、成功率 ≥ 95%、WebSocket 均可恢复、Granite 恢复无需重启 Middleware、telemetry 中断不产生虚假事件、重复用户可见展示率为 0%。

## 9. 配置、隐私与可复现性

请提供 `config.example`，不得提交密钥、个人路径或敏感信息。实验元数据至少包含：

```yaml
git_commit: ""
test_timestamp_utc: ""
torcs_version: ""
middleware_version: ""
granite_model: ""
granite_quantisation: ""
lm_studio_version: ""
operating_system: ""
cpu: ""
gpu: ""
ram_gb: null
commentary_mode: ""
detection_interval_s: null
temperature: null
max_tokens: null
streaming: null
tts_enabled: null
tracks: []
opponents: null
event_match_tolerance_s: 1.0
```

所有生成命令应支持明确的输入和输出路径，避免依赖当前用户的绝对路径。随机过程必须固定 seed。脚本应有 `--help`，失败时返回非零退出码，并给出可理解的错误信息。

## 10. 必须执行的验证

完成代码后至少执行：

1. 新增 Commentary 自动化测试；
2. 仓库已有相关测试；
3. 格式检查、lint 或 type check（若仓库已配置）；
4. 使用 `sample_data` 验证所有分析脚本；
5. 验证同一输入重复运行产生相同指标；
6. 验证空输入、非法输入和缺少字段不会生成误导性报告；
7. 检查真实 `results/` 不含伪造数据；
8. 检查 README 中的命令能从干净环境执行。

如果完整测试因缺少 TORCS、模型或硬件无法运行，应：

- 仍运行所有不依赖这些外部条件的自动化测试；
- 明确列出未运行项、原因和用户需要执行的命令；
- 不把未运行项写成通过。

## 11. 最终交付物

完成后应交付：

- Commentary 自动化测试套件；
- 必要且最小的可测试性/日志改造；
- 实验 CSV 模板与 schema 验证；
- 事件一对一匹配与 Precision/Recall/F1 脚本；
- 延迟统计脚本；
- 稳定性与故障恢复统计脚本；
- 自动生成论文 Markdown/CSV 表格的脚本；
- 实验操作手册；
- 测试矩阵；
- 示例数据及其明确的 `SAMPLE/NOT REAL RESULTS` 标识；
- 实际执行过的测试结果摘要。

最终回复必须包含：

1. 改动文件列表；
2. 与真实架构对应的测试设计摘要；
3. 实际运行的命令；
4. 真实通过、失败、跳过数量；
5. 发现并修复的缺陷；
6. 尚未完成或需要人工执行的实验；
7. 人工实验的下一步命令；
8. 风险与限制。

不要只回复“已完成”。不要将样例结果当作论文结果。

## 12. 推荐执行顺序

- [ ] 审计仓库并建立需求—代码—测试映射
- [ ] 运行现有测试并记录基线
- [ ] 补齐输入、事件边界、模式、优先级、冷却和去重测试
- [ ] 补齐 Granite、WebSocket、TTS 和非法配置测试
- [ ] 验证并修复“广播前文本去重”问题
- [ ] 建立 CSV schema、模板和数据校验
- [ ] 实现确定性的一对一事件匹配和 P/R/F1
- [ ] 加入结构化延迟埋点与分析脚本
- [ ] 建立 endurance/fault-injection 操作与分析流程
- [ ] 自动生成论文表格
- [ ] 运行全部可运行验证并记录真实结果
- [ ] 更新实验手册与最终交付摘要

## 13. 正文结果应支持的最终结构

最终产物应足以支持团队报告中的以下小节，但本任务不要求编造数值或代写不存在的结果：

```text
5.X AI Live Commentary Evaluation
5.X.1 Evaluation Method
5.X.2 Functional Correctness
5.X.3 Event Detection Accuracy
5.X.4 End-to-End Latency
5.X.5 Stability and Fault Recovery
5.X.6 Limitations
```

正文建议仅保留：自动化测试汇总表、事件检测准确率表、延迟表/箱线图、故障恢复表，以及一个“缺陷—修复—回归通过”的真实案例。详细用例、日志和原始数据放附录或仓库。
