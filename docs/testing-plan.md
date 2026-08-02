# F1-simulator 测试流程

本文定义项目进入测试阶段后的完整执行流程。分五层，L0–L3 全自动、无外部依赖，
L4–L5 依赖真实模型/TORCS/Overlay 环境，需人工确认。

最后一次全量实测：**2026-07-26**，L0–L3 全部通过（7/7）。

## 0. 分层总览

| 层 | 内容 | 依赖 | 耗时 | 自动化 |
| --- | --- | --- | ---: | --- |
| L0 | 静态检查：Python 编译、Electron JS 语法 | 无 | <10s | ✅ `tools/run_tests.sh` |
| L1 | 离线单元测试：midware 单测 + 三个内置自测 | 无 | ~10s | ✅ 同上 |
| L2 | 进程内集成：API / WebSocket / UDP / Bot 心跳 | 无 | ~5s | ✅ 同上 |
| L3 | 真实服务冒烟：运行时矩阵 + UDP 遥测链路 | 端口 8880/3101 空闲 | ~30s | ✅ `--service` |
| L4 | 外部依赖：Granite / TTS / 语音输入 | LM Studio、Kokoro 权重、麦克风 | 5–10min | ❌ 人工 |
| L5 | 端到端验收：真实 TORCS + ai_bot + Overlay | TORCS 构建、WSLg、Node | 20–30min | ❌ 人工 |

一键跑 L0–L3：

```bash
cd ~/summer-project/F1-simulator
bash tools/run_tests.sh --service
```

不加 `--service` 只跑 L0–L2（不占端口，可随时跑）。`--only L1` 可单跑某一层。

## 1. 环境准备

```bash
cd ~/summer-project/F1-simulator
source .venv/bin/activate
pip install -r requirements-core.txt      # 测试只需要 core，voice/tts 是 L4 才用
```

开跑前确认三件事：

1. **端口空闲**：L3 会绑定 8880 和 3101。`ss -tulnp | grep -E '8880|3101|3001|8766|8881'`
   有残留进程先 `pkill -f midware.app`。
2. **只有一个 UDP 3101 监听者**。生产模式下只有 `TelemetryService` 能绑 3101，
   旧工具不会自动回退绑定；如果 L3 的遥测冒烟失败，先查是不是有 debug listener 占了端口。
3. **ai_bot.py 双副本**。仓库在 `~/summer-project/F1-simulator/`，但 TORCS 实际构建和
   运行在 `~/projects/for_summer_project/`。L5 跑之前必须同步，否则测的是旧代码：

   ```bash
   cp ~/summer-project/F1-simulator/{ai_bot.py,track_model.py} ~/projects/for_summer_project/
   md5sum ~/summer-project/F1-simulator/ai_bot.py ~/projects/for_summer_project/ai_bot.py
   ```

## 2. L0 — 静态检查

```bash
python -m compileall -q . -x '(\.venv|BUILD|src|export|data)'
node --check overlay-app/electron/main.js
node --check overlay-app/electron/preload.js
node --check overlay-app/src/engineer-renderer.js
node --check overlay-app/src/settings.js
```

通过标准：无输出、退出码 0。

> 已知情况：WSL 里没装 node（`which node` 为空，只有 `/mnt/d/tavern/npm` 是 Windows 侧的）。
> 脚本会自动跳过 JS 检查并打 SKIP。要覆盖这块需在跑 Overlay 的那台环境里执行，或在 WSL 装 Node。

## 3. L1 — 离线单元测试

```bash
python -m pytest tests/unit -q          # 或 python -m unittest discover -s tests/unit -v
python ai_bot.py                        # 内置控制/协议自测，不连 TORCS
python track_model.py                   # 内置赛道模型自测
python test_a_module_latency.py         # A 模块低延迟回归
```

`tests/unit` 当前 10 个文件覆盖：ModelBroker 优先级/队列满/过期、FeatureGate、
feature 禁用、遥测 schema、TORCS 字段适配、UDP 单一所有者、输出协议、Bot 心跳服务、
Bot 客户端、策略决策。

`ai_bot.py` 无参数运行时**只跑自测然后退出**（覆盖 `safety_filter`、
`_parse_strategy_response`、`_next_debounced_strategy`、`_build_strategy_prompt`）。
要真正驾驶必须加 `--bot`——这是最容易踩的坑。

通过标准：pytest 全绿；三个自测脚本分别打印 `All tests passed.` /
`All track_model tests passed.` / `ALL TESTS PASSED`。

## 4. L2 — 进程内集成测试

```bash
python -m pytest tests/integration -q
```

用 `TestClient(create_app())` 起真实 app，覆盖：

- Feature API 走 ModelBroker 路径（engineer/ask + history + clear）
- 四个 feature 逐个禁用后真的返回 HTTP 409 且 `active=false`
- WebSocket V1 字段（`version`/`request_id`/`sequence`）与 legacy 字段共存
- **真实 UDP 包**发到 3101 后进入共享 store，`/api/telemetry` 读得到
- Coach 从共享遥测读数据
- Bot 心跳的年龄判断

注意这一层已经会绑定 3101，别和 L3 或真实 TORCS 同时跑。

## 5. L3 — 真实服务进程冒烟

```bash
python -m midware.app &                 # 等 /api/health 可用
python tools/runtime_matrix_check.py
```

`runtime_matrix_check.py` 检查三组：

1. 7 个必备端点可用（health / features / features/status / race/snapshot /
   coach/dashboard / engineer/history / bot/status）
2. **全部 11 种 feature 组合**（2 选、3 选、4 选）都能 POST 成功且回读一致
3. 每个 feature 单独禁用后，对应 handler 真的返回 409 且状态为 inactive
   （不是只改元数据）

`tools/run_tests.sh --service` 在此基础上多做一步：注入一帧构造的 CSV 遥测到
UDP 3101，然后轮询 `/api/telemetry` 确认 `seq` 落地——验证的是**跨进程**的
UDP → TelemetryService → REST 全链路（L2 是进程内的）。

通过标准：`All checks passed.` + `UDP 3101 -> /api/telemetry OK`。

## 6. L4 — 外部依赖（人工）

需要外部服务，无法进 CI。**逐项的完整操作步骤、判定标准和失败排查见
[manual-test-guide.md](manual-test-guide.md)**，下表只是索引：

| # | 检查项 | 命令 | 通过标准 |
| --- | --- | --- | --- |
| 4.1 | Granite / LM Studio 连通 | `python lmstudio_smoke_test.py` | 打印连接 banner + 模型一句话回复 |
| 4.2 | 模型延迟在预算内 | 见 [torcs-granite-quickstart.md](torcs-granite-quickstart.md) 的测速片段 | 单次 ≤2.5s；否则按"约 2 倍最坏延迟"调 `_STRATEGY_INTERVAL` |
| 4.3 | Broker 串行化生效 | 同时打 engineer + coach 请求 | engineer 优先返回，coach 排队不阻塞实时控制 |
| 4.4 | TTS 服务 | `python tts_server.py` 后 `curl 127.0.0.1:8881/health` | 返回正常；Kokoro 权重需已下载 |
| 4.5 | 语音输入 | `python voice_input.py` | 麦克风识别出文本；WSLg PulseAudio 有环境差异 |

L4 失败不阻塞 L0–L3，但阻塞 L5。

## 7. L5 — 端到端验收（人工）

**每一项的详细步骤见 [manual-test-guide.md](manual-test-guide.md)**，本节是概览。

前置：L0–L4 通过；`ai_bot.py`/`track_model.py` 已同步到运行目录。
注意 TORCS 二进制只在 `~/projects/for_summer_project/BUILD/bin/torcs`，仓库里没有构建产物。
推荐用 `python launcher_gui.py` 图形面板统一启停，而不是手开五个终端。

启动顺序（各开一个终端）：

```bash
# 1. LM Studio（Windows 侧）Start Server，记下 Reachable at 地址
# 2. midware
python -m midware.app
# 3. TORCS，Quick Race 选 scr_server 1
export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe
bash torcs_launcher.sh
# 4. bot
python ai_bot.py --bot --granite
# 5. Overlay（Feature 1 工程师字幕窗口，可选）
cd overlay-app && npm install && npm start
```

验收清单：

| # | 检查项 | 通过标准 |
| --- | --- | --- |
| 5.1 | SCR 握手 | bot 打印 `Identified! Entering drive loop.`；`ss -tunp` 见 `udp ESTAB ...:3001` |
| 5.2 | Granite 策略生效 | 启动行 `granite=True`；之后约每 5s 一行 `[Granite] XXX — reason` |
| 5.3 | 安全过滤优先 | 低油量/高损伤时策略被本地 `safety_filter` 覆写，且不等待模型 |
| 5.4 | 人类遥测入库 | 人工驾驶时 `/api/telemetry` 的 `seq`/`speedX` 持续变化 |
| 5.5 | Feature 2 页面 | Coach dashboard 随驾驶更新 |
| 5.6 | Feature 1 路由 | 工程师提问的回答出现在工程师窗口，不串到解说 |
| 5.7 | 自动解说 | 浏览器 dashboard 上事件触发后有解说输出，无明显重复（去重生效） |
| 5.8 | WebSocket 重连 | 杀掉 midware 再拉起，dashboard 和 Overlay 都自动恢复 |
| 5.9 | Overlay 行为 | 无边框/透明/置顶/顶部居中；设置窗口可开 |
| 5.10 | TTS 播报 | 设置里开启语音后能听到解说 |

Overlay 的详细人工验收表见 [../overlay-app/TESTING.md](../overlay-app/TESTING.md)。
黑屏问题见 [wslg-black-screen-recovery.md](wslg-black-screen-recovery.md)。

## 8. 当前覆盖缺口

以下模块**目前没有任何自动化测试**，按补测性价比排序（都是纯函数，好测）：

| 优先级 | 模块 | 规模 | 建议补的用例 |
| --- | --- | ---: | --- |
| P0 | `midware/feature2_core.py` | 39 KB | `build_rule_feedback` / `build_priority_issues` / `severity_rank` / `overlay_payload`：给定几组 frames 断言产出的 issue 数量、严重度排序、文案截断 |
| P0 | `midware/commentary_engine.py` | 16 KB | `detect_event` 的触发与不触发边界、`event_signature` + `normalize_text_key` 去重、`summarize_frames` 空输入 |
| P1 | `midware/context_manager.py` | 13 KB | prompt 裁剪后长度上界、历史条数上界、超长输入不炸 |
| P1 | `telemetry_common.py` | 35 KB | `LatestTaskRunner` 只保留最新任务（有并发语义，容易回归）、模型连接兜底链的分支 |
| P2 | `midware/shared/race_snapshot.py` | — | 缺字段/空遥测时的快照默认值 |
| P2 | `track_model.py` | 24 KB | 已有内置自测，但只覆盖 unit-test 轨和 g-track-2；可扩到更多真实赛道 XML |
| P3 | `race_analyzer.py`、`overlay_broadcast.py`、`launcher_gui.py` | — | 优先级最低，GUI/脚本类 |

另外两项工程缺口：

- **没有 CI**（无 `.github/`）。L0–L2 完全无外部依赖、5 秒跑完，非常适合上 GitHub Actions。
- **没有覆盖率数据**。`pip install pytest-cov` 后 `pytest tests/ --cov=midware --cov-report=term-missing`
  可以给出准确的缺口清单，替代上面的人工估计。

## 9. 什么时候跑哪一层

| 场景 | 跑什么 |
| --- | --- |
| 每次改代码后 | `bash tools/run_tests.sh`（L0–L2，约 15s） |
| 提交前 / 合并前 | `bash tools/run_tests.sh --service`（L0–L3） |
| 改了模型调用、prompt、策略 | L0–L3 + L4 |
| 演示前 / 交付前 | 全部 L0–L5，并填完第 6、7 节的清单 |

## 10. 缺陷记录模板

```text
层级：L?            用例：?
环境：WSL2 Ubuntu / Python 3.14.4 / .venv
复现：<命令或操作步骤>
期望：
实际：
日志：/tmp/f1sim_midware_test.log 或终端输出
```
