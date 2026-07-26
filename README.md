# F1 Simulator — TORCS × IBM Granite

基于 TORCS 1.3.7 的本地 AI 赛车演示系统。项目将 TORCS 遥测与 SCR 控制接口连接到 OpenAI-compatible 的 IBM Granite 模型服务，提供赛车工程师问答、实时遥测看板、赛事解说和 AI 驾驶四个功能方向。模型可以通过 LM Studio 在本机运行；AI 服务、字幕 Overlay 和语音能力均保留在本地。

## 演示截图

<!-- TODO: 在最终录制后添加实际演示截图。 -->

## 功能状态

| Feature | 功能 | 主要入口 | 当前状态 |
| --- | --- | --- | --- |
| 1 | AI 赛车工程师问答 | `POST /api/engineer/ask` | 由统一后端和 Model Broker 提供 |
| 2 | 实时遥测看板与驾驶建议 | `GET /api/coach/dashboard` | 使用共享 TelemetryStore |
| 3 | 事件驱动的 AI 实时赛事解说 | `python3 -m midware.app` | 正式后端入口 |
| 4 | Granite 辅助策略的 AI 驾驶机器人 | `ai_bot.py --bot --granite` | 策略经 Middleware，控制循环保持本地 |

“已实现”表示代码入口和运行链路已经存在，不代表所有机器配置、模型或赛道组合均已完成自动化验收。

## 系统架构

```text
                              OpenAI-compatible Granite API
                                        (LM Studio)
                                             ^
                                             |
TORCS human driver -- UDP :3101 --> Middleware (sole listener)
       |                              |-- Feature 1: racing engineer
       |                              |-- Feature 2: dashboard / coach
       |                              `-- Feature 3: commentary
       |                                         |
       |                         REST + WebSocket :8880/:8766
       |                                         |
       |                              Electron overlay-app
       |                                         |
       |                              optional Kokoro TTS :8881
       |
       `-- scr_server <------ UDP :3001 ------> Feature 4: ai_bot.py
```

端口和主机默认值集中在 `config.json`，Python 代码通过 `config.py` 读取。环境变量优先于配置文件。更详细的模块说明见 [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md)，集成协议见 [docs/integration-contract.md](docs/integration-contract.md)。

## 支持的平台

| 平台 | 支持情况 | 备注 |
| --- | --- | --- |
| Ubuntu 22.04/24.04（X11） | 推荐 | TORCS、Python 服务和 Electron 可在同一系统运行 |
| Windows 10/11 + WSL2/WSLg（Ubuntu） | 支持 | LM Studio 通常运行在 Windows；WSLg 图形、麦克风和音频桥接可能需要额外处理 |
| 原生 Windows / macOS | 未提供 | 当前没有对应的 TORCS 构建、启动和验收流程 |

Python 3.10+ 和 Node.js 18+ 为建议版本。WSL 中必须使用 Linux 版 `node`/`npm`，不能使用 `/mnt/c/...` 下的 Windows 可执行文件。

## 从全新 Ubuntu / WSL 环境安装

仓库没有“安装全部项目”的单一 shell 脚本。`setup_linux.sh` 是 TORCS 上游的运行配置安装辅助脚本，需要目标目录参数；它不是本 AI 项目的一键安装器。下面是完整、可复现的安装流程。

先获取代码（如果你正在阅读本地仓库，可跳过）：

```bash
cd ~
git clone https://github.com/JulianC-2778/F1-simulator.git
cd F1-simulator
```

### 1. 安装系统依赖

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential automake autoconf libtool pkg-config \
  libglib2.0-dev libgl1-mesa-dev libglu1-mesa-dev freeglut3-dev \
  libplib-dev libopenal-dev libalut-dev libxi-dev libxmu-dev \
  libxrender-dev libxrandr-dev libpng-dev libvorbis-dev \
  python3 python3-venv python3-pip python3-tk \
  nodejs npm curl ffmpeg alsa-utils pulseaudio-utils \
  xdotool x11-utils \
  libatk-bridge2.0-0 libgtk-3-0 libnss3 libxss1 libasound2 \
  libdrm2 libgbm1 speech-dispatcher espeak-ng
```

`xdotool` 和 `xwininfo`（由 `x11-utils` 提供）是 `torcs_launcher.sh` 实际使用的启动辅助工具。无麦克风需求时可不安装 `ffmpeg`、`alsa-utils`、`pulseaudio-utils`；不用 Electron 时可省略对应桌面运行库。

### 2. 创建 Python 环境并安装依赖

```bash
cd ~/F1-simulator
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

# 完整安装：核心 + 语音输入 + Kokoro TTS
python -m pip install -r requirements.txt
```

如果只演示不含语音的核心功能，可缩短安装时间：

```bash
python -m pip install -r requirements-core.txt
```

分层依赖如下：

- `requirements-core.txt`：midware、Granite API、WebSocket 广播和测试依赖。
- `requirements-voice.txt`：Feature 1 的 faster-whisper 语音输入。
- `requirements-tts.txt`：Kokoro TTS 服务及模型下载工具。
- `requirements.txt`：聚合以上三个文件，用于完整安装。

### 3. 安装 Electron Overlay

```bash
cd ~/F1-simulator/overlay-app
npm install
cd ..
```

WSL 用户应先执行 `which node` 和 `which npm`，确认结果不是 `/mnt/c/...` 或 `/mnt/d/...`。

### 4. 编译 TORCS

```bash
cd ~/F1-simulator
export CFLAGS="-fPIC"
export CPPFLAGS="$CFLAGS"
export CXXFLAGS="$CFLAGS"

./configure --prefix="$(pwd)/BUILD"
make -j"$(nproc)"
make install
make datainstall
```

编译结果位于仓库内的 `BUILD/`，不会安装到系统目录。TORCS 用户配置仍默认写入 `~/.torcs`。

### 5. 可选：准备 Kokoro 模型

```bash
source ~/F1-simulator/.venv/bin/activate
cd ~/F1-simulator
python -c "from huggingface_hub import hf_hub_download; hf_hub_download('hexgrad/Kokoro-82M', 'kokoro-v1_0.pth', local_dir='.'); hf_hub_download('hexgrad/Kokoro-82M', 'voices/bm_lewis.pt', local_dir='.')"
```

模型权重不提交到 Git。详细语音配置见 [docs/tts-setup.md](docs/tts-setup.md) 和 [docs/voice-input-setup.md](docs/voice-input-setup.md)。

## Granite 模型选择与配置

1. 在 LM Studio 中下载并加载一个 IBM Granite instruct/chat 模型。
2. 在 Developer / Local Server 页面启动 OpenAI-compatible server，通常为 `http://127.0.0.1:1234/v1`。
3. 建议优先选择机器能够稳定实时运行的 Granite 模型；仓库不锁定具体量化版本。较小模型响应更快，较大模型通常需要更多内存/显存。
4. 验证连接：

```bash
cd ~/F1-simulator
source .venv/bin/activate
python lmstudio_smoke_test.py
```

默认会读取服务的 `/v1/models` 并优先选择 ID 中包含 `granite` 的模型。需要显式指定时：

```bash
export TORCS_AI_BASE_URL="http://127.0.0.1:1234/v1"
export TORCS_AI_MODEL="<LM Studio 中显示的模型 ID>"
```

WSL2 无法访问 Windows 上的 LM Studio 时，在 LM Studio 开启局域网访问，并把 `127.0.0.1` 替换为其显示的可达地址。Feature 1 还支持 `TORCS_ENGINEER_BASE_URL` / `TORCS_ENGINEER_MODEL` 覆盖；项目端口可直接编辑 `config.json` 或使用 `TORCS_MIDWARE_PORT`、`TORCS_FEATURE2_PORT`、`TORCS_TTS_PORT` 等环境变量。

## 最小演示流程（Feature 3 实时解说）

以下流程从仓库根目录开始，使用四个终端。

终端 1：启动 Granite 模型服务后，启动主 midware：

```bash
cd ~/F1-simulator
source .venv/bin/activate
python3 -m midware.app
```

终端 2：启动字幕 Overlay：

```bash
cd ~/F1-simulator/overlay-app
npm start
```

终端 3：配置遥测并启动 TORCS：

```bash
cd ~/F1-simulator
mkdir -p logs
export TORCS_PLAYER_LOG_DIR="$PWD/logs"
export TORCS_PLAYER_LOG_HZ=20
export TORCS_PLAYER_UDP_HOST=127.0.0.1
export TORCS_PLAYER_UDP_PORT=3101
./torcs_launcher.sh
```

若不需要 WSLg 窗口修复，也可直接运行 `./BUILD/bin/torcs`。进入 **Race → Quick Race**，选择 human driver 并开始驾驶。

终端 4（可选）：启动本地 TTS：

```bash
cd ~/F1-simulator
source .venv/bin/activate
python tts_server.py
```

验收信号：`http://127.0.0.1:8880` 能打开、遥测数值随比赛变化、Overlay 已连接，并在事件触发后显示 Granite 生成的解说。

## 四个 Feature 的启动方法

### Feature 1 — AI 赛车工程师

```bash
cd ~/F1-simulator
source .venv/bin/activate
python chat_engineer.py  # legacy debug API client
# 或调试用桌面 GUI
python chat_engineer_gui.py
```

没有 TORCS 时可使用假数据演示：

```bash
TORCS_ENGINEER_USE_FAKE_DATA=true python chat_engineer.py
```

在提问提示符输入 `v` 可录入英文语音。该工具只调用 Middleware API，不监听 UDP，也不直连 Granite。

### Feature 2 — 遥测看板 / 驾驶建议

先启动 `python3 -m midware.app`。以下独立服务仅为一个版本周期的兼容代理：

```bash
cd ~/F1-simulator
source .venv/bin/activate
python midware/feature2_service.py
```

打开 `http://127.0.0.1:8766/feature2`。保留的早期 CLI 驾驶建议入口为：

```bash
python telemetry_analyzer.py
```

### Feature 3 — AI 实时赛事解说

按“最小演示流程”启动 `python3 -m midware.app`、`overlay-app` 和 TORCS。事件检测、上下文构建与流式输出说明见 [docs/commentary-loop.md](docs/commentary-loop.md)。

### Feature 4 — AI 驾驶机器人

先以 SCR 2013 协议启动 TORCS：

```bash
cd ~/F1-simulator
./BUILD/bin/torcs -ver 2013
```

在 Quick Race 中选择 `scr_server 1`。另开终端：

```bash
cd ~/F1-simulator
source .venv/bin/activate
python ai_bot.py --bot --granite
```

`--bot` 必须提供；不加时只运行内置自测试。完整操作和故障排查见 [docs/torcs-granite-quickstart.md](docs/torcs-granite-quickstart.md)。

## 测试

项目包含标准库单元测试和内置控制算法回归测试；完整端到端验收仍需真实 TORCS/Granite 环境。
分层流程、人工验收清单和覆盖缺口见 [docs/testing-plan.md](docs/testing-plan.md)。

一键执行静态检查、离线单测、集成测试和服务冒烟（L0–L3）：

```bash
bash tools/run_tests.sh --service
```

也可以逐条手动执行：

```bash
cd ~/F1-simulator
source .venv/bin/activate

# Python 语法检查
python -m compileall -q *.py midware tools

# Granite 连通性（需要已启动模型服务）
python lmstudio_smoke_test.py

# AI bot 内置协议/控制测试（不连接 TORCS）
python ai_bot.py

# 后端单元测试
python -m unittest discover -s tests/unit -v

# API/WebSocket/UDP/Bot 集成测试
python -m unittest discover -s tests/integration -v

# 运行时矩阵检查
python tools/runtime_matrix_check.py

# Electron JavaScript 静态检查
node --check overlay-app/electron/main.js
node --check overlay-app/src/renderer.js
node --check overlay-app/src/engineer-renderer.js
```

端到端测试应至少确认：UDP 3101 遥测变化、Feature 2 页面更新、WebSocket 断线重连、Feature 1 输出路由到工程师窗口、TTS `/health` 返回正常，以及 `ai_bot.py` 与 UDP 3001 完成 SCR 握手。Overlay 的详细人工验收表见 [overlay-app/TESTING.md](overlay-app/TESTING.md)。

## Demo 视频与 vlog

- Demo 视频：
- 开发 vlog：

## 已知限制

- 首次安装不是完全离线流程：Python/npm 包、Granite 模型、Whisper 模型和 Kokoro 权重均可能需要下载。
- WSLg 的 OpenGL、PulseAudio 麦克风和音频播放存在环境差异；黑屏恢复见 [docs/wslg-black-screen-recovery.md](docs/wslg-black-screen-recovery.md)。
- LM Studio 位于 Windows、服务位于 WSL2 时，`localhost` 路由取决于 WSL 网络模式。
- Feature 2 独立服务依赖主 midware 的遥测历史 API；它不能单独产生真实遥测。
- 生产模式只有 Middleware 监听 UDP 3101；旧工具不会自动回退绑定该端口。
- Kokoro 权重和 voices 不在仓库中，必须单独下载。
- Feature 4 的 Granite 只通过 Model Broker 负责低频策略选择；实时转向、油门、制动和安全过滤由本地控制器完成。
- 自动化测试和 CI 覆盖仍不完整，最终演示前需执行人工端到端检查。

## 数据采集与底层接口

- human driver 可按 `TORCS_PLAYER_LOG_*` 环境变量写出 CSV，并将同一遥测行发送至 UDP 3101。
- `scr_server 1` 至 `scr_server 10` 使用 UDP 3001–3010，为外部客户端提供双向 SCR 控制接口。
- 更完整的字段、协议与原生 TORCS 信息保留在 [README](README) 和 `doc/` 目录中。

## License

TORCS 引擎及项目代码沿用仓库内已有许可证。部分 `data/cars/models/pw-*` 和 `data/cars/models/kc-*` 车辆素材具有单独许可，请在再分发前查看相应目录的 `readme.txt`。
