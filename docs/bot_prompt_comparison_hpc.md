# Bot 策略层 —— HPC 平台测量 (companion run)

> **Status: real data**, measured 2026-08-16 against Bristol BluePebble
> (`bp1-gpu001`, reached over an SSH tunnel from WSL:
> `ssh -N -L 1234:bp1-gpu001:20317 sg25291@bp1-login.acrc.bris.ac.uk`).
> Raw outputs, alongside the earlier runs' data in the same directory:
> [`evaluation/bot/results/hpc_prompt_comparison_20260816.txt`](../evaluation/bot/results/hpc_prompt_comparison_20260816.txt)
> (§1 variant comparison) and
> [`evaluation/bot/results/real_experiment_bot_drive_hpc_20260816.jsonl`](../evaluation/bot/results/real_experiment_bot_drive_hpc_20260816.jsonl)
> (§3–4 race trace; reproduce with
> `python3 evaluation/bot/scripts/analyse_bot_trace.py <that file>`).
> The comparison log is `.txt` rather than `.log` because `.gitignore:46`
> excludes `*.log` and would silently drop it.
>
> **This document does not replace or amend
> [`bot_prompt_comparison_race3.md`](bot_prompt_comparison_race3.md) or
> [`bot_real_experiment_20260812.md`](bot_real_experiment_20260812.md).** Those
> runs stand on their own. This is an independent set of measurements on
> different hardware, kept separate so the two can be compared rather than
> conflated.
>
> **What it was for:** the earlier reports mix two kinds of result, and only one
> of them should depend on the machine. Decision content — which strategy the
> model picks, and whether it picks differently in different situations — is a
> property of the model and the prompt. Response time is a property of the
> hardware. This run tests whether that separation holds, and fills the one
> measurement the earlier report explicitly could not make (§4 below).

## 平台

| | `race3.jsonl` 那份 | 本文档 |
|---|---|---|
| 推理后端 | LM Studio | HPC 推理服务（OpenAI 兼容端点） |
| 位置 | `/home/abcdz/` 那台机器 | BluePebble `bp1-gpu001`，SSH 隧道到 `localhost:1234` |
| 模型 id | `granite-4.1-8b` | `ibm-granite`（端点同时暴露 `granite` 别名） |
| 裸调用实测 | 未记录 | **19.6 tok/s**（413 prompt / 137 gen / 7.0 s） |
| 采样方式 | `shuf -n 25`（随机，抽样文件未保留） | `--limit 50`（均匀间隔，确定性可复现） |

> ⚠️ **模型权重是否相同未能确认。** HPC 端点只报 id `ibm-granite`，未暴露量化方式或
> 参数量。两者很可能是同一个模型，但没有验证过——这限制了对内容差异的解释力度。

**参考基线：** 同一提示词在本项目的开发笔记本上（GTX 1650 4 GB，模型 5.35 GB 装不进
显存）实测 **4.7 tok/s / 30.3 s**，比这里慢 4.3 倍，且延迟在 30–51 s 之间摆动、出现过
超时与 JSON 截断导致的 502。**这台笔记本不是 `race3.jsonl` 的测量平台**（见下方 §1）。

---

## §1 四变体决策对照

`python3 bot_replay.py --compare --states race3.jsonl --limit 50`

| 变体 | 应答 | 策略分布 | distinct | consistent | 中位耗时 |
|---|---|---|---:|---|---:|
| legacy | 50/50 | ATTACK 50 | 1 | True | 1.7s |
| bare | 50/50 | ATTACK 38 / NORMAL 12 | 2 | True | 2.7s |
| concise | 50/50 | ATTACK 39 / NORMAL 11 | 2 | True | 5.0s |
| reasoning | 50/50 | ATTACK 48 / NORMAL 2 | 2 | True | 6.9s |

（`sensitive` 一列已从表中移除——该指标有实现缺陷，见 §5。）

### 与 `race3.jsonl` 那份对照

| 指标 | 那份（25 条） | 本次（50 条） |
|---|---|---|
| legacy distinct | 1 | 1 |
| bare distinct | 2 | 2 |
| reasoning distinct | 2 | 2 |
| 耗时 legacy / bare / reasoning | 2.5 / 3.3 / 7.6s | 1.7 / 2.7 / 6.9s |

**耗时只差 10–25%，不是数量级差异。** 结合上面 4.3 倍的笔记本对照可以判断：
`race3.jsonl` 那份**本来就不是在那台笔记本上跑的**（它的语料路径是 `/home/abcdz/`，
另一台机器）。两份耗时可以并列，但**不能解读为硬件升级带来的收益**。

---

## §2 延迟分布

`python3 bench_latency.py --modes legacy,bare,reasoning --repeats 10`

§1 的中位数混入了 50 个不同赛况本身的差异。此处固定同一赛况重复调用，测的是延迟本身；
首次调用单列，不计入统计。

| 变体 | n | min | median | mean | P95 | max | 首次调用 | 错误 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| legacy | 9 | 1.3 | 1.3 | 1.4 | 1.5 | 1.5 | 1.4 | 0 |
| bare | 9 | 2.4 | 2.5 | 2.6 | 3.0 | 3.0 | 2.5 | 0 |
| reasoning | 9 | 7.0 | 7.1 | 7.1 | 7.5 | 7.5 | 7.2 | 0 |

**延迟是可预测的。** reasoning 全距 0.5 s，P95 与中位数相差 0.4 s，27 次调用零错误。
笔记本上同一提示词在 30–51 s 之间摆动——**受限硬件不只是慢，而且不稳定；这里两者都不成立。**

未观察到冷启动惩罚（首次调用 ≈ 中位数），但测量时模型已处于热状态，**只能说本次没测到，
不能断言该端点无冷启动成本**。

---

## §3 真实比赛中的延迟 —— 填补 `bot_real_experiment_20260812.md` §4 的缺口

那份报告只能测「两次决策记录之间的间隔」，并明确写明该数字**不可当作延迟引用**，因为它
混入了轮询间隔、被丢弃的过期请求，以及当时按 reason 文本去重导致的漏记。

`ai_bot.py` 的 `GraniteStrategist._call_granite` 现在直接对 HTTP 往返计时，`TraceRecorder`
把 `round_trip_s` 写进每条 decision 记录；同时决策记录改为按 `answer_seq`（每次完成的
往返计数）触发，不再按 reason 文本去重。本次是第一份带该字段的语料。

| | N | min | median | max |
|---|---:|---:|---:|---:|
| **真实比赛中**（`race_hpc.jsonl`） | 15 | **6.7 s** | **7.2 s** | **7.6 s** |
| 离线基准（§2 reasoning） | 9 | 7.0 s | 7.1 s | 7.5 s |

**两者几乎完全重合。** 边开车边推理，与串行离线调用相比没有可测量的代价——TORCS 与推理
服务不争抢资源（推理在远端 GPU 节点上，TORCS 在本地）。

这有两个用处：**(a)** 补上了那份报告标注为无法测量的一项；**(b)** 说明离线回放的耗时
可以代表真实比赛的延迟，后续不必每次都真跑一场才能测延迟。

**决策节奏同样干净：** 15 次决策的间隔 min 14.48 / median 15.02 / max 15.97 s，
即 `_STRATEGY_INTERVAL = 15 s` 本身，**15 次请求一次未丢**。7 s 的响应装进 15 s 的间隔
有充足余量。

> ⚠️ `analyse_bot_trace.py` 输出末尾那句
> `Intervals within one poll cycle (4-6s) ... consistent with the text-unchanged dedup`
> **已经过时**：`4-6s` 是按旧的 `_STRATEGY_INTERVAL = 5.0` 硬编码的，且文本去重问题已修。
> 该行不可引用。

---

## §4 驾驶质量（同一场比赛顺带产出）

`python3 evaluation/bot/scripts/analyse_bot_trace.py race_hpc.jsonl`

| 项目 | 值 |
|---|---|
| 时长 / 距离 | 3.7 min / 9.90 km |
| 完成圈数 | 3（80.1 / 67.6 / 65.1 s） |
| 出界 | 1 次，**100% 恢复** |
| 碰撞（damage 跳变） | 6 次，最终 damage 2967 |
| 决策来源 | **granite 14 / rule_block 1** |

**圈速逐圈变快**（80.1 → 67.6 → 65.1 s）。**6 次碰撞全部集中在 t=20614–20711**，即开赛后
前 100 秒的起步混战，之后至结束无新碰撞。

`0.606 次/km` 高于那份报告的 `0.392 次/km`，但本场只有 3.7 分钟、9.9 km，**分母小且全部
落在起步阶段**，两个数字不宜直接比较。

**样本量明显不足**：计划 8 圈，实际 3 圈。这是一个数据点，不是一个分布。

---

## §5 `sensitive` 指标有缺陷 —— 两份报告的该列都不应引用

`legacy` 在 `race3.jsonl` 那份是 `False`，本次是 `True`。**这个差异不是发现，是指标本身的
问题**，缺陷在 `bot_replay.py`，两次运行都受影响：

```python
moved = (before.get("strategy") != after.get("strategy")
         or before.get("reason", "") != after.get("reason", ""))
```

判定「模型注意到输入变化」的条件包含 **reason 文本任意改变**。`temperature=0.2` 下措辞
本就会抖动，一次**决策完全没变、只是换了说法**的回答也会被记为 `sensitive=True`。

更根本的是这份语料的取值范围：**油量 74.1–94.0 L**，扰动测试把它改成 60 L——**两个值都属于
"油多得用不完"**，任何合理策略都不该因此改变。**这个探测在这份语料上问不出有效信息。**

要得到有意义的敏感性结论需要：只比较 `strategy` 不比较 reason 文本；且扰动到一个真正会
改变正确答案的值（例如油量降到跑不完全程）——后者需要一份覆盖低油量的语料，见 §7。

---

## §6 结论

**1. 决策内容与硬件无关。** 四个变体的 `distinct` 与 `race3.jsonl` 那份完全一致。虽然模型
权重是否相同未验证，但在这个层面上，"提示词决定模型读不读状态"这个结论不依赖跑在哪台机器上。

**2. HPC 上延迟低且可预测，真实比赛与离线一致。** 7 s 量级、全距 0.5 s、零错误、零请求丢弃；
真实比赛 6.7–7.6 s 与离线 7.0–7.5 s 重合。

**3. `bare` 不是可上线的选项。** 它不输出 `considered`，**无法展示模型的推理过程**——而那正是
本模块要证明的东西。真正的候选只有 `reasoning`（7.1 s，3 因素）和 `concise`（5.0 s，2 因素）。

**4. `concise` 相对 `bare` 的额外 2.3 s，买到的正是可见的推理过程。** 两者决策分布几乎相同
（39/11 vs 38/12），差别只在有没有推理轨迹。

### ⚠️ 一条需要撤回的说法

本文档早期版本曾把「`legacy` 模式坍缩（distinct=1）」称为最关键的发现。**加入 `concise` 与
`reasoning` 的完整分布后，这个说法站不住：**

- 这份语料是顺风局（油 74–94 L，损伤 0–580），**ATTACK 几乎永远是正确答案**
- `reasoning` 本身也是 48/50 答 ATTACK，与 `legacy` 的 50/50 差距很小
- 反而是 `bare`/`concise` 有约 22% 的情况答了 NORMAL，在这份语料上**更像是无谓的保守**

**在正确答案本身就单一的语料上，`distinct` 高不代表好。** 该指标在此不能作为质量判据。
`legacy` 与 `bare` 的差异在统计上显著，与 `reasoning` 的差异则证据不足。

---

## §7 局限

- **语料无法区分变体 —— 这是目前最大的缺口。** 油量 74.1–94.0 L、损伤 0–580，从未接近任何
  决策边界。**`SAVE_FUEL` / `PIT` / `DEFEND` 在真实语料中一次都没被选中过**，两个平台皆然。
  要证明 `reasoning` 优于 `legacy`，需要的是**一份含难局面的语料**，而不是更快的硬件。
- **驾驶质量样本量不足。** 3 圈 / 3.7 分钟，一个数据点。
- **模型权重未验证同一。** 见平台表下方警告。
- **单次运行。** 每项都只测了一轮，未测量运行间波动。
- **`sensitive` 指标不可用。** 见 §5。
