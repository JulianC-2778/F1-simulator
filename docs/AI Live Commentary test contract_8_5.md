# AI Live Commentary 测试方案



## 1\.1 测试目标

测试需要回答四个核心问题：

1. Commentary 的事件检测规则是否按照设计正确运行？

2. 系统能否准确识别真实比赛事件？

3. 从事件发生到字幕生成的延迟是否满足实时解说需求？

4. 系统在持续运行和外部服务故障时是否稳定、能够恢复？

最终在论文正文中形成四组结果：

---

## 测试环境记录

正式测试前冻结最终代码版本，并记录：

测试期间不得更换模型、阈值或硬件配置。若进行了修复，应记录修复前后的 commit。

---

# 测试一：功能正确性测试

## 3\.1 测试目的

验证 Commentary Engine 的输入处理、事件触发、模式切换、优先级、冷却和运行时集成是否符合设计。

建议使用 `pytest` 自动执行。

## 3\.2 测试用例

### A\. 输入处理

### B\. 事件触发与边界

边界测试必须分别验证：

\[

x=T\-\\epsilon,\\qquad x=T,\\qquad x=T\+\\epsilon

\]

其中 \(T\) 为触发阈值。

### C\. 模式、优先级与冷却

### D\. Runtime 与接口

## 3\.3 论文结果表

## 3\.4 验收标准

- 所有 P0 功能测试必须通过；

- 总体通过率达到 95%以上；

- 不得存在导致 Middleware 崩溃的输入；

- 去重测试必须验证 Overlay 是否收到文本，不能只检查后台日志；

- 修复缺陷后必须重新运行完整回归测试。

---

# 测试二：真实比赛事件检测准确率

## 4\.1 测试目的

单元测试只能证明代码按照规则运行，不能证明规则适用于真实比赛。因此需要将自动检测结果与人工标注的真实事件进行比较。

## 4\.2 数据采集

建议采集：

- 2条不同赛道；

- 每条赛道3场；

- 共6场比赛；

- 每场5–8分钟；

- 总计30–45分钟；

- 同时保存 telemetry、Commentary 日志和屏幕录像。

主动制造以下事件：

- Contact；

- Position change；

- Off\-track；

- Lap complete；

- Battle；

- Pace surge。

每类事件尽量达到至少10个实例。`pace_update` 属于定时事件，不计入事件检测F1，可以单独验证触发间隔。

## 4\.3 人工标注格式

建立 `ground_truth.csv`：

```Plain Text
session,event_type,start_time,end_time,description
S01,contact,42.3,42.8,collision with opponent
S01,position_change,71.2,71.5,moved from P4 to P3
S01,off_track,103.1,104.6,left track boundary
```

系统检测结果建立 `detected_events.csv`：

```Plain Text
session,event_type,detection_time,priority
S01,contact,42.7,90
S01,position_change,71.6,80
```

同一事件类型且检测时间处于人工事件时间的 ±1 秒内，视为匹配。

- TP：正确检测；

- FP：系统检测到，但人工录像中没有；

- FN：人工录像中存在，但系统未检测。

## 4\.4 评价指标

\[

Precision=\\frac\{TP\}\{TP\+FP\}

\]

\[

Recall=\\frac\{TP\}\{TP\+FN\}

\]

\[

F\_1=2\\frac\{Precision\\times Recall\}\{Precision\+Recall\}

\]

Overall 结果建议使用所有事件的 micro\-average，而不是简单平均各类别结果。

## 4\.5 论文结果表

## 4\.6 验收标准

建议目标：

- Overall Precision ≥ 0\.80；

- Overall Recall ≥ 0\.80；

- Overall F1 ≥ 0\.80；

- 每个关键事件的 Recall ≥ 0\.70；

- 不允许出现大量连续重复误报。

这些是项目目标，不应为了通过而修改结果。若未达到，应分析：

- 固定阈值是否不适合不同赛道；

- telemetry 是否存在波动；

- battle 条件是否过于宽松；

- 冷却时间是否导致漏报；

- 多事件同帧择一是否造成其他事件未被记录。

---

# 测试三：端到端延迟

## 5\.1 测试目的

测量从比赛事件进入系统到字幕完成显示所需的时间，判断 Commentary 是否具备实时性。

## 5\.2 时间戳

记录以下时间：

- \(t\_0\)：事件 telemetry 到达 Middleware；

- \(t\_1\)：系统确认 `event_detected`；

- \(t\_2\)：收到 Granite 第一个 token；

- \(t\_3\)：收到完整 `ai_done`；

- \(t\_4\)：Overlay 显示完成；

- \(t\_5\)：TTS 开始播放，仅在正式产品启用 TTS 时记录。

计算：

\[

L\_\{\\text\{detection\}\}=t\_1\-t\_0

\]

\[

L\_\{\\text\{first\-token\}\}=t\_2\-t\_1

\]

\[

L\_\{\\text\{generation\}\}=t\_3\-t\_1

\]

\[

L\_\{\\text\{caption\}\}=t\_4\-t\_0

\]

\[

L\_\{\\text\{audio\}\}=t\_5\-t\_0

\]

## 5\.3 执行方法

- 使用最终演示配置；

- 执行30次独立事件触发；

- 尽量包含不同事件类型；

- 使用单调时钟，例如 `time.perf_counter()`；

- 每次测试之间等待冷却结束；

- Granite 测试期间不要同时执行其他模型任务；

- 记录失败和超时，不能将其从结果中删除。

## 5\.4 统计结果

分别计算：

- Median；

- P95；

- Maximum；

- Minimum；

- Mean；

- Standard deviation；

- Failure count。

延迟通常可能包含长尾，因此正文应重点报告 median 和 P95，而不是只报告平均值。

## 5\.5 论文结果表

## 5\.6 验收标准

建议目标：

- Detection median ≤ 0\.5秒；

- Detection P95 ≤ 1\.0秒；

- First\-token median ≤ 2\.0秒；

- Complete\-caption median ≤ 4\.0秒；

- 30次测试中模型失败率低于5%；

- 不出现 Middleware 崩溃。

如果模型生成时间超过目标，不一定代表系统无效，但必须明确讨论 Granite 推理是主要瓶颈，以及短提示词、减少 token 或使用更小量化模型能否改善结果。

---

# 测试四：稳定性与故障恢复

## 6\.1 持续运行测试

执行3次，每次30分钟，总计90分钟。

每次运行包含：

- 正常驾驶与持续 telemetry；

- 多次连续事件；

- 一次 WebSocket 断开与重新连接；

- 一次 Granite 服务中断与恢复；

- 一次 telemetry 暂停与恢复；

- 如果启用 TTS，再注入一次 TTS 失败。

记录：

- 检测事件数；

- Commentary 请求数；

- 成功输出数；

- 模型错误数；

- 重复展示数；

- Middleware 崩溃数；

- CPU平均值和峰值；

- 内存初始值和结束值。

成功率为：

\[

Success\\ Rate=

\\frac\{\\text\{Successful outputs\}\}

\{\\text\{Commentary requests\}\}

\\times100%

\]

### 结果表

## 6\.2 故障注入测试

每种故障重复5次。

恢复时间定义为：

# \[
T\_\{\\text\{recovery\}\}

## t\_\{\\text\{first successful output after recovery\}\}

t\_\{\\text\{service restored\}\}

\]

### 论文结果表

## 6\.3 验收标准

- 90分钟内 Middleware 崩溃次数为0；

- Commentary 成功率≥95%；

- 所有WebSocket断线均可恢复；

- Granite恢复后无需重启Middleware；

- telemetry中断期间不产生虚假比赛事件；

- 非法telemetry不得导致未捕获异常；

- TTS故障不得破坏字幕输出；

- 重复展示率应为0%。

---

# 两个必须重点验证的问题

## 7\.1 文本去重顺序

当前风险是模型完成结果可能先通过 `ai_done` 广播，之后才执行文本重复检查。

测试方式：

1. Mock Granite，使其连续返回完全相同文本；

2. 连续触发两个不同但允许生成的事件；

3. 监听Overlay实际收到的 `ai_done`；

4. 检查第二条重复文本是否已经被广播。

预期结果不是“日志显示重复”，而是：

> Overlay只收到一次该文本。
> 
> 

如果失败，应将去重检查移动到广播之前，然后执行回归测试。

## 7\.2 45词限制

`max_words=45` 如果只存在于提示词中，不能保证模型一定遵守。

客观测试方式：

- 在30次延迟测试和90分钟稳定性测试中记录所有输出；

- 自动统计每条输出的词数；

- 记录超过45词的数量和比例；

- 不评价文本好坏，只验证配置约束是否实际成立。

该指标可以放入稳定性结果：

---

# 执行顺序

---

# 论文中的篇幅安排

Commentary测试正文控制在约800–950词。

正文保留：

- 1张自动化测试汇总表；

- 1张事件检测准确率表；

- 1张延迟表或箱线图；

- 1张故障恢复表；

- 1个“发现缺陷—修复—回归通过”的案例。

完整测试用例、原始延迟数据、pytest日志和故障注入步骤放入附录或GitHub。这样既能满足“results obtained”“robust implementation”和“critical analysis”的评分要求，又不会挤占其他三个AI功能的篇幅。

