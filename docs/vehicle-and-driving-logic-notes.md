# 赛车物理 / 驾驶逻辑参考笔记

2026-08-08 整理。内容来自一次问答式的代码走查，把车辆物理（损伤、传感器）、`ai_bot.py` 底层控制器架构、超车机制、以及 TORCS 自带车型/AI 车手做了一遍梳理，并顺带落地了一个超车逻辑的改动。供以后回来对着改代码时当速查手册用。

---

## 1. 车辆损伤如何影响车速

物理引擎在 [aero.cpp:122-124](../src/modules/simu/simuv3/aero.cpp#L122-L124)：

```cpp
tdble dmg_coef = ((tdble)car->dammage / 10000.0);
car->aero.drag = -SIGN(car->DynGC.vel.x) * car->aero.SCx2 * v2 * (1.0 + dmg_coef) * dragK * dragK;
```

`car->dammage`（TORCS 原生拼写）越大，空气阻力系数越高，直接压低极速和加速。另外三条路径：

- **额外扭矩**（[aero.cpp:132-147](../src/modules/simu/simuv3/aero.cpp#L132-L147)）：损伤后阻力还会对车身产生 Mx/My/Mz 扭矩，车变得不稳定、易甩尾。
- **悬挂/对准受损**：碰撞累积 `bent_damage_x/z`、`rotational_damage_x/z`（[collide.cpp:128-139](../src/modules/simu/simuv3/collide.cpp#L128-L139)、[wheel.h:101-105](../src/modules/simu/simuv3/wheel.h#L101-L105)），影响抓地力和转向精度。
- **轮胎磨损**（`tyre_damage` 选项）持续降低抓地极限。

一句话：损伤不是掉血条，而是通过阻力+扭矩+悬挂/轮胎抓地三条路径实打实拖慢车速、恶化操控。

---

## 2. 传感器视距

前向 19 束 track 传感器量程由 `scr_server.cpp` 里的版本号决定：

```cpp
// scr_server.cpp:211-223
if version == "2009": __SENSORS_RANGE__ = 100
elif version in {2010,2011,2012,2013}: __SENSORS_RANGE__ = 200
```

[main.cpp:47](../src/linux/main.cpp#L47) 默认 `setVersion("2013")` → **默认量程 200m**。同一个量程同时喂给 track（19束前向）和 opponents（36束环绕）两套传感器。

`ai_bot.py` 里从 ±10° 前向光束取最长的作为 `sight`（[ai_bot.py:1503](../ai_bot.py#L1503) 附近），直接影响：

| 系统 | 依赖方式 |
|---|---|
| 弯速上限 | `dist_limit = sqrt(floor² + sight·speed_factor)` |
| 直道判定 `_STRAIGHT_CLEAR` | `sight >= 180` 才当直道处理 |
| TRUST 信任门控 `_TRUST_SIGHT_FRAC` | 传感器读数要和地图给的下个弯道距离一致才信任地图限速 |
| 转向瞄准点（pursuit target） | ±60° 内按光束长度加权平均角度 |
| 横向避让（不同传感器，同一 200m 量程） | opponents 数组左右间隙判定 |

---

## 3. 底层控制器有 7 层，按优先级串联

`compute_control()`（[ai_bot.py:1258](../ai_bot.py#L1258)）不是并行的多个系统简单相加，是一套**优先级抢断 + 显式仲裁**的结构：

1. **崩溃/脱轨恢复**（最高优先级，互斥抢断，命中就直接 `return`）：stabilize latch → reverse burst → recovery gate → blind 兜底。
2. **转向系统**（叠加式，但每项都有仲裁机制）：`steer = aim·_PP_GAIN + centre + barrier + avoid - speed_y·_STEER_DAMP`，再除以速度衰减，再加阻尼项 `angle·steer_gain`。
3. **目标车速系统**（链式 `min`/`max` 仲裁，不是叠加）：corner-sight 限速 → side-ease 乘法修正 → front-follow cap → 地图限速（或 TRUST 模式下反向抬高）。
4. **策略参数层**：ATTACK/NORMAL/DEFEND/SAVE_FUEL/PIT/BLOCK 只是换一组增益旋钮（`_DriveParams`），不改变 2、3 层算法结构。
5. **Safety filter**（[ai_bot.py:1654](../ai_bot.py#L1654)）：逐帧规则引擎，把 Granite 的策略压成安全策略。
6. **Granite LLM 策略层**：5 秒轮询，只在 5 个策略里选，经第 5 层过滤才生效。
7. **执行器保护层**：ABS/TCL/换挡，纯下游后处理。

**能不能拆？** 结论是**不建议按"系统独立"拆**——代码里一大段调参历史注释（`_AVOID_GAIN` 0.15→0.22→0.45→0.22 那段）本身就是"系统互相打架"的案例集，修复方式从来不是把系统拆得更独立，而是让它们**更明确地协商**（`line_raw==0` 互斥门控、`_LINE_SLEW`/`_AVOID_SLEW` 斜坡限幅、`_AVOID_FADE_FLOOR` 独立地板值）。真正能安全拆的只有第 7 层这种严格下游、无需感知其他系统状态的后处理逻辑；第 2、3 层可以拆成独立函数改善可读性，但仲裁信号（`pursuit`/`fade`/`left_gap`/`right_gap`）必须继续显式共享，不能拆成互不知情的并行模块。

---

## 4. 超车机制

没有独立的"超车决策模块"，是内嵌在 `compute_control` 转向/限速两条 stack 里的距离+速度触发式规则。

**选边**（[ai_bot.py:1443-1451](../ai_bot.py#L1443-L1451)，2026-08-08 改过，见第 5 节）：`front_gap < 55m`（`_OVERTAKE_TRIGGER_M`）且前车间距在**真实缩短**（见下）时，比较 `left_gap`/`right_gap`，差距 > 5m（`_OVERTAKE_ROOM_MARGIN`）就选空当大的一侧，设一个目标横向位置 `line_raw = ±0.50`（`_OVERTAKE_BIAS`），经 `_LINE_SLEW` 缓慢逼近（约1秒走完），且只在循迹信号"安静"时才生效（弯道不会被超车意图打断）。

**速度**：超车不改变纵向目标速度，只挪横向位置。真正会刹车的只有 `front-follow cap`（[ai_bot.py:1524-1539](../ai_bot.py#L1524-L1539)）——两侧都窄（`_FRONT_ESCAPE_M=20m`）且前车贴到 `_FRONT_BRAKE_M=10m` 内才用 sqrt 曲线降速跟车，是最后手段，不是超车准备动作。

**时机链**：55m 外开始试探换线 → 14m 内（`_AVOID_DIST`）被动切换成"以不撞为主"（避让项会抵消超车 bias）→ 10m 内且两侧都堵死才真正刹车。

**冲突消解**：vs 地图出弯线，`line_raw==0` 门控，地图优先；vs BLOCK 防守偏置，两者共用 `line_raw`，超车 bias 判断在前，互斥不叠加；vs 侧向碰撞规避 `avoid`，这是刻意保留的冲突——贴近时安全规避会盖过超车换线。

---

## 5. 改动记录：超车触发加了"闭合速度"门控

**问题**：原逻辑只看绝对距离（`front_gap < 55m`），同速跟车（train，间距恒定不缩短）会被当成"发现慢车"一样触发换线，白白偏离赛车线却超不了车。

**改动**（[ai_bot.py:862-881](../ai_bot.py#L862-L881) 常量 + [ai_bot.py:1450-1461](../ai_bot.py#L1450-L1461) 核心逻辑 + [ai_bot.py:1482-1483](../ai_bot.py#L1482-L1483) 门控）：

- 新增 `_close_rate_lp`：每帧用 `(上一帧front_gap - 这一帧front_gap) / 0.02s` 算出闭合速度，EMA 平滑（α=0.1）。SCR 对手传感器只给距离不给相对速度，这个量是免费从距离导出的，不需要新传感器数据。
- 触发条件新增 `_close_rate_lp > _OVERTAKE_CLOSE_RATE_MIN`(1.5 m/s)：只有前车间距真的在缩短才允许换线。
- 首帧保护：`_front_gap_prev`/`front_gap` 任一为 200（无车）时闭合速度记 0，避免"车刚进锥形区"被误读成瞬间贴近。

**测试**：`python3 ai_bot.py`（无参数跑内置自测）全部通过，包括：
- 把原来两个静态间距超车测试改成"间距 40m→25m 匀速缩短"（约 12.5 m/s，明显超过阈值）；
- 新增一个"间距恒定 25m、一侧畅通"的回归测试，断言 `|steer| < 0.05`——这正是本次修复要解决的假触发场景。

**尚未验证**：`_OVERTAKE_CLOSE_RATE_MIN=1.5`、`_CLOSE_RATE_ALPHA=0.1` 是估算的初值，只过了单元测试，没有实车/TORCS 里跑过。日志里新加了 `crate=` 字段（per-step debug log），下次实车测试建议盯着这个字段确认触发时机符合预期，参照项目里其它常数（如 `_AVOID_GAIN`）都是这样反复实测调出来的先例。

**2026-08-08 补丁：`crate` 传感器窗口跳变误判**（实车日志抓到，验证了上面那条"尚未验证"）——`front_gap` 是 `_FRONT_CONE` 这批光束的 min，不是对同一目标的连续跟踪，所以当一辆车的方位角刚好扫过锥形区 ±30° 边界时，`front_gap` 会瞬间跳变，即便这辆车的真实距离几乎没变。实测日志：`ogap` 一帧内 23.7→7.5m（隐含闭合速度约 970 m/s），而同一辆车在 `_AVOID_RIGHT` 窗口读到的 `rgap` 那帧只从 7.3→7.5m——真实距离几乎没动，只是"哪个窗口先看到它"变了。原实现没防住这种情况，EMA 平滑后 `crate` 还是冲到 +69.6，并且要衰减约 0.7 秒才会掉回阈值以下，这段时间超车换线的门会被假开着。

修法：给单帧 `front_gap` 变化量加了合理性上限 `_CLOSE_RATE_SANITY_MAX=50 m/s`（[ai_bot.py:1462-1470](../ai_bot.py#L1462-L1470)），超过这个值直接当噪声丢弃（`raw_close_rate` 记 0，让 EMA 衰减回 0，而不是把尖峰吃进去）。新增回归测试复现了这个确切场景（`front_gap` 从 24m 跳到 7.5m 一帧内），断言 `crate` 不会超过 5——`python3 ai_bot.py` 全部通过。这条常数（50 m/s）也还没有实车反复验证过，只是按"两车速度差不太可能超过 180km/h"估的。

---

## 6. TORCS 车型对比，以及我们在用的车

仓库 `data/cars/models/` 打包了 9 类车型：

| 类别 | 代表车型 | 驱动 | 车重 | 转速上限 | Cx | 特点 |
|---|---|---|---|---|---|---|
| **trb1（我们在用）** | car1~8-trb1 | RWD | 1150 kg | 9152 rpm | 0.35 | 涡轮增压，峰值扭矩 483N·m@8000rpm（≈540马力），**带真实前后定风翼** |
| ow1（方程式） | car1-ow1 | RWD | 600 kg | 18700 rpm | 0.32 | 估算峰值功率 ~850马力，动力/重量比比真实F1还夸张，纯游戏化 |
| stock1/2 | car1-stock1/2 | RWD | 1550 kg | 7000-9000 rpm | 0.38-0.42 | 美式 Stock Car 路线 |
| Offroad-4WD（WRC） | pw-206wrc 等6款 | 4WD | 1350 kg | 8200 rpm | 0.48 | 比 trb1 重、阻力大，砂石地形调校 |
| Historic（经典GT） | kc-\* 共17款 | 多为RWD | 各异(980-1400kg) | ~6000 rpm | ~0.35 | 自然吸气，无定风翼，复古 |
| Track-4WD-GrB | 155-DTM | 4WD | 1100 kg | 8500 rpm | 0.32 | DTM 房车 |
| Track-FWD-GrB | p406 | FWD | 1500 kg | 6500 rpm | 0.32 | 前驱房车 |
| Track-RWD-GrB | acura-nsx-sz | RWD | 1400 kg | 8500 rpm | 0.34 | 公路跑车调校 |
| Offroad-RWD-GrA | baja-bug/buggy | RWD | 600 kg | 5500-7000 rpm | 0.45 | 沙滩越野车 |

我们的车 `car1-trb1` 由 [scr_server.xml:22](../src/drivers/scr_server/scr_server.xml#L22) 等 10 个车手位固定写死。它是 SCR 竞赛标准基准车型，不是最快也不是最像真实车——比它夸张的有 ow1，比它贴近街车的有 p406/acura-nsx-sz。

粗略核算：峰值功率约 400kW、`Cx·迎风面积 ≈ 0.67m²`，理论极速（不含传动损耗和后翼额外阻力）约 340-360 km/h。对照 `_ATTACK_PARAMS.max_speed=330`（[ai_bot.py:559](../ai_bot.py#L559)）、`NORMAL=250`，大致落在这台车物理上够得着的区间。

---

## 7. TORCS 内置 AI 车手（不是车型）

`berniw hist`、`InfHist`、`berniw two` 这类名字是**机器人模块**给自己 10 个车手席位起的显示名，不是车。命名规律：`name`=排位榜车手名，`car name`=分配的具体车型。

| 模块 | 车手名前缀 | 10 席位车型 |
|---|---|---|
| berniw（基础版） | inferno/berniw 编号 | 主要 `car1~7-trb1` + `trb3`（同我们车型） |
| berniw2 | "berniw two" | 前6席 `pw-*wrc`（WRC拉力），后4席 trb1/trb3 |
| berniw3 | "berniw hist" | 全部 10 席 `kc-*`（经典GT） |
| inferno（基础版） | "inferno" | 1席ow1、1席p406，其余 trb1/trb3 |
| inferno2 | "InfHist" | 和 berniw3 完全相同的 10 辆 kc-\* |
| bt/tita/lliaw/olethros | 对应编号 | 主力 trb1/trb3，各混1席 ow1 或 p406/pw-corollawrc |
| damned | 对应编号 | 主要 `pw-*wrc` |
| sparkle | — | 单一 `baja-bug` |

这些名字只有手动在 TORCS Quick Race 车手选择界面把它们拉进对手栏才会出现，跟 `scr_server` 固定开 `car1-trb1` 无关。

---

## 8. 测试时对手车该怎么选

**我们自己的车**：保持 `car1-trb1` 不动。所有调参常数都是围着这台车的物理特性调出来的，换车等于全部重调。

**日常回归测试**：对手选 `bt`/`tita`/`inferno`/`berniw`（主力 trb1/trb3），车型接近才会制造真实的"势均力敌、贴身缠斗"场景——这正是验证超车 closing-rate 门控、BLOCK 防守、avoid 横向规避是否正常工作所需要的中间态。

**不建议做主力对手**：`berniw3`/`inferno2`（kc-\* 经典车，速度差太大，只会呈现单调超车）、`sparkle`（越野车，没有代表性）、`damned`/`berniw2`（WRC为主，更重更慢）。

**边界/压力测试**：想专门验证极端速度差下会不会误判，借 `tita`/`lliaw`/`olethros` 车队里自带的 `car1-ow1`（极快）或拉一台 `damned`/`berniw2`（更慢）进场专项测试，不作为日常基准。
