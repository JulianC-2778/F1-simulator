# 用 BluePebble 跑 Granite（替代 Windows 上的 LM Studio）

TORCS 本体**留在本地**（需要 OpenGL 窗口和实时操作，Slurm 批处理跑不了）。
搬上超算的只有 Granite 推理这一层：

```
TORCS(本地 WSL) → midware(本地 127.0.0.1:8880) → SSH 隧道 → bp1-gpuXXX:PORT (vLLM)
```

**项目代码零改动。** vLLM 对外的模型名注册成 `ibm-granite`，正好是
[midware/runtime.py:82](../midware/runtime.py) 里 `api_config` 的默认值，隧道又把远程端口映射
成本地 1234（LM Studio 原来的端口），所以 midware 起来就能用，连
`POST /api/config/api` 都不用发。

---

## 每次要用时：完整流程

### 超算侧（PuTTY）

```bash
sbatch ~/serve_granite_2080.sbatch
```

```bash
bash ~/wait_ready.sh
```

`wait_ready.sh` 会挂着等，就绪后打印出**带真实节点和端口**的隧道命令。
**每次的节点和端口都不一样**（PORT 由 job id 算出），必须用它当次打印的那条。

### 本地侧（WSL，四个窗口，都要一直开着）

**① 隧道** — 用 `wait_ready.sh` 打印的那条，形如：

```bash
ssh -N -o ServerAliveInterval=30 -L 1234:bp1-gpuXXX:2XXXX sg25291@bp1-login.acrc.bris.ac.uk
```

**② midware**

```bash
cd ~/summer-project/F1-simulator && .venv/bin/python -m midware.app
```

**③ TORCS**

```bash
cd /home/jay/projects/for_summer_project/BUILD && export DISPLAY=:0 && export LIBGL_ALWAYS_SOFTWARE=1 && export GALLIUM_DRIVER=llvmpipe && ./bin/torcs
```

**④ bot**

```bash
cd ~/summer-project/F1-simulator && .venv/bin/python ai_bot.py --bot --granite
```

### 验证整条链路

```bash
curl -s -m 60 -X POST http://127.0.0.1:8880/api/engineer/ask -H "Content-Type: application/json" -d '{"question": "say hello in five words"}'
```

返回 `{"ok":true,"answer":"..."}` 即打通。

### ⚠️ 路径陷阱（都踩过）

| 写法 | 正确的 |
|---|---|
| `midware/.venv/bin/python` | **`.venv/bin/python`** — venv 在项目根目录 |
| `bash torcs_launcher.sh` | 脚本里写死了别人的 `/home/yejian/torcs/BUILD`，直接跑 `./bin/torcs` |
| TORCS 在 `~/summer-project/F1-simulator` | 编译产物在 **`~/projects/for_summer_project/BUILD`**，F1-simulator 只是代码仓库 |

### 用完必须收尾

```bash
scancel <jobid>
```

服务作业不会自己退出，会一直占着两张 GPU 直到 6 小时 walltime 到期。中途要停就
`scancel`，下次重新提交——实测从提交到 READY 只要几分钟，重开成本很低。

按顺序关：bot `Ctrl+C` → TORCS 退出 → midware `Ctrl+C` → 隧道 `Ctrl+C` → `scancel`。

---

## 这套环境的既定事实（都是实测出来的，别再试错）

| 项 | 值 |
|---|---|
| account | `coms039904` |
| 驱动 / CUDA | `580.126.20` / CUDA 13.0 → pip 默认装的 CUDA 13 wheel 可用 |
| 模型 | `/user/work/sg25291/granite-4.1-8b`，HF safetensors，**16.38 GiB** bf16 |
| 架构 | `GraniteForCausalLM`（dense，vLLM 0.26 原生支持） |
| venv | `/user/work/sg25291/venvs/vllm`（**不能放家目录**，配额不够） |
| 上下文 | 模型支持 128k，但**必须** `--max-model-len 4096`，否则 KV cache 撑爆 |

### 两个分区的取舍

| | `gpu_short`（**推荐**） | `gpu` |
|---|---|---|
| 卡 | RTX 2080 Ti ×N，**10.57 GiB/张** | A100 **MIG 切片，19.5 GiB**（不是 80GB 整卡！） |
| 排队 | ~26 个待运行，实测几十分钟内能排上 | ~141 个待运行 |
| walltime | 6 小时 | 14 天 |
| 配置 | 双卡 `-tp 2` + `--dtype float16`（Turing 无 bf16） | 单卡，bf16 |
| 脚本 | `serve_granite_2080.sbatch` | `serve_granite.sbatch` |

两个都提交、谁先跑起来用谁，是很划算的做法。

---

## 显存这件事（踩过两次坑，务必读）

`--gpu-memory-utilization` 这个预算**只覆盖权重 + KV cache**。推理过程中的临时张量
（尤其是 `10 万词表 × chunk` 的 logits，以及双卡下合并它的 all_gather）用的是
**预算之外**剩下的那部分显存。所以利用率不能拉满。

实测两次失败：

| 作业 | 配置 | 报错 |
|---|---|---|
| 18286830 | A100 MIG, util 0.90, **max-len 8192** | `Available KV cache memory: 1.05 GiB`，而 8192 需要 1.25 GiB |
| 18287260 | 2×2080Ti, **util 0.95**, max-len 4096 | KV 够了（2.0 GiB），但 `Tried to allocate 98.00 MiB, only 87.06 MiB is free` |

现在的参数是按这两次的实测数字定的：

- **2080 双卡**：`util 0.88` → 权重 8.19 + KV ~1.1 GiB，留 ~1.2 GiB 给临时张量
- **A100 MIG**：`util 0.90` → 权重 16.38 + KV ~1.05 GiB，留 ~2 GiB 给临时张量
- 两边都 `--max-model-len 4096`（一条 4096 的序列只要 0.63 GiB KV，很宽裕）
- 两边都 `--max-num-batched-tokens 2048`（压低单次批量的激活值峰值）

**还 OOM 的话**：先降 `--max-model-len` 到 2048，再考虑 `--gres=gpu:3` + `-tp 3`。
不要往上调 utilization，方向是反的。

---

## 目录里的文件

| 文件 | 用途 | 跑在哪 |
|---|---|---|
| `serve_granite_2080.sbatch` | 双 2080 Ti 起服务（主力） | 超算 |
| `serve_granite.sbatch` | A100 MIG 起服务（备选） | 超算 |
| `preflight.sbatch` | 5 分钟体检：驱动版本、显存、模型可读性 | 超算 |
| `wait_ready.sh` | 等作业就绪，自动打印隧道命令；失败则揪出根因 | 超算 |
| `connect.sh` | 自动读节点+端口并开隧道 | 本地 WSL |

超算上的这几个都在 `~/`。改完本地要重传：

```bash
scp ~/summer-project/F1-simulator/hpc/*.sbatch ~/summer-project/F1-simulator/hpc/wait_ready.sh sg25291@bp1-login.acrc.bris.ac.uk:~/
```

---

## 常见坑

| 现象 | 处理 |
|---|---|
| 终端敲不进字 | `until`/`wait_ready.sh` 在前台跑着，`Ctrl+C` 停掉即可，不影响作业 |
| 玩到一半 AI 不回话 | walltime 到了。`squeue -u $USER` 一看便知，重新提交＋重开隧道 |
| 作业在跑但 `Connection refused` | 权重还在加载，等 `wait_ready.sh` 打印 `READY` |
| 隧道断了 | 已带 `ServerAliveInterval=30`；笔记本休眠必断，醒来重开 |
| 在 PowerShell 里开的隧道连不上 | **必须在 WSL 里开**，midware 的 `127.0.0.1` 是 WSL 自己的网络命名空间 |
| `sbatch: error: Unable to open file` | 命令末尾多打了中文标点（比如 `、`） |
| 首 token 比 LM Studio 慢 | 隧道多了校园网一跳。测延迟时把网络那部分单独标出来 |

---

## 另一条路：把超算当批处理跑评测

上面这套是"实时联机"，适合开着 TORCS 演示。如果目的是**给报告出数据**（延迟分布、
不同 prompt/参数对比、多模型横评），更该做的是**不开隧道**——写个 headless 脚本，
在同一个作业里先起 server 再打一批固定问题，全程在计算节点跑完写成 CSV。提交完就
可以关电脑，排队多久都无所谓，数字还不含网络抖动。
