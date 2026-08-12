# Prompt 变体真实语料对照表 —— race3.jsonl (2026-08-12)

## 语料来源

`/home/abcdz/F1-simulator/race3.jsonl`,8 圈真实比赛(`TORCS_BOT_TRACE=race3.jsonl`,`TORCS_BOT_PROMPT=reasoning`),461 行(432 条 state + 29 条 decision),油量从 94.0L 降到 73.7L(耗 20.3L),比 `race2.jsonl`(3 圈、仅耗 5L)覆盖更多真实赛况。

从中过滤出 `"kind": "state"` 的记录、随机抽样 25 条得到 `sample25.jsonl`,用 `bot_replay.py --compare` 对 legacy / bare / reasoning 三个 prompt 变体各问一遍同样的 25 个状态。

**注意**:直接对 `race3.jsonl` 做 `shuf -n 25` 会把 `"kind": "decision"` 的记录也采样进去——这类记录没有 `state` 字段,会被 `bot_replay.py` 的 `load_states()` 误当成状态使用,污染样本。抽样前务必先按 `"kind": "state"` 过滤。

## 对照结果

| 变体 | 应答 | 策略分布 | distinct(区分度) | sensitive(对扰动敏感) | consistent(自洽) | 响应中位数 |
|---|---|---|---|---|---|---|
| legacy | 25/25 | ATTACK 25 | **1(模式坍缩)** | **False** | True | 2.5s |
| bare | 25/25 | ATTACK 17 / NORMAL 8 | 2 | True | True | 3.3s |
| reasoning | 25/25 | ATTACK 22 / NORMAL 3 | 2 | True | True | 7.6s |

- **distinct**:25 个不同真实赛况给出的不同策略种类数。1 = 模式坍缩,不管状态如何都给同一个答案。
- **sensitive**:sensitivity probe——把某个状态的 fuel 从原值改成 60.0 再问一遍,看策略/理由是否变化。False = 没注意到关键数字被改了。
- **consistent**:同一个状态连问两次,看答案是否一致。

## 结论

1. **legacy 是真的模式坍缩**:25 个完全不同的真实赛况(不同圈速、不同油量、不同损伤、不同对手车距)清一色给 ATTACK,sensitivity probe 也测不出反应,证实它就是在读几条硬编码阈值,不是在读传进去的状态。
2. **reasoning 的排除理由引用的是真实遥测数值**,不是模板句。例如:
   ```
   trace @ 31327 m   NORMAL   7.2s   Balanced approach ensures safety and efficiency.
       · gap behind: 11 m (~0.3 s) -> risk of collision if aggressive
       · damage: 228/10000 -> moderate wear, can handle slight mistakes
       · fuel spare: 75.6 L -> enough for multiple laps
       x ruled out ATTACK: high risk due to close gap behind and moderate damage
   ```
   bare 变体虽然答案也会变,但很多理由是固定措辞反复出现("With no car in front and a large gap behind, the driver can...."),更像是套模板而非真正逐条核对数据。
3. **reasoning 的代价是响应变慢**:中位数 7.6 秒,是 legacy(2.5s)的 3 倍——生成 considered/rejected 的结构化 JSON 比直接吐一个策略词要慢。

## 局限(这份语料还没测到的)

这 25 个采样状态的 fuel spare 基本都在 67-76 L 之间——整场 8 圈只耗了 20L,起始 94L,从未真正逼近"油量不够跑完"的临界点。**SAVE_FUEL / PIT / DEFEND 三个策略在这份真实语料里一次都没被选中过**,低油量、真正需要防守的贴身局面这些"难局面"依然只在手写的合成数据(`bot_replay.py` 里的 `SYNTHETIC_STATES`)里测过。后续如果要验证这些分支,需要故意拉长比赛圈数、调低起始油量,或者做一场对手更密集的比赛。

## 原始数据

- 完整回放输出(含全部 75 次问答的逐条理由):`/home/abcdz/F1-simulator` 主机上 `/tmp/bot_replay_compare.log`(会话临时文件,未纳入版本控制)
- 采样文件:`/home/abcdz/F1-simulator/sample25.jsonl`
- 完整语料:`/home/abcdz/F1-simulator/race3.jsonl`
