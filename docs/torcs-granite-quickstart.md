# 启动 TORCS 并连接 LM Studio（Granite）— 复用指南

## 先测一下要不要走全部流程

```bash
curl -s -m 3 http://127.0.0.1:8880/api/health >/dev/null && echo "midware 还活着，直接跳到【步骤4】" || echo "midware 没起，从【步骤1】开始"
```

---

## 步骤1：Windows 上开 LM Studio

1. LM Studio → 左侧 **Local Server**（`>_`）。
2. 选好 Granite 模型（如 `granite-4.1-8b`），状态是 `READY`。
3. 点 **Start Server**。

---

## 步骤2：查 Windows 主机 IP，测通 LM Studio

WSL 终端里：

```bash
ip route | grep default
```

输出类似 `default via 172.21.160.1 dev eth0`，记下这个 IP（下面全部命令里的 `172.21.160.1` 都换成你查到的）。

```bash
curl -s -m 5 http://172.21.160.1:1234/v1/models
```

能看到 JSON 里有 `granite` 字样就算通。

---

## 步骤3：起 midware，指向 LM Studio

**终端 A**（一直开着，不要关）：

```bash
cd ~/F1-simulator
midware/.venv/bin/python -m midware.app
```

**终端 B**（跑完这两条就可以关了）：

```bash
curl -s -X POST http://127.0.0.1:8880/api/config/api \
  -H "Content-Type: application/json" \
  -d '{"base_url": "http://172.21.160.1:1234/v1", "model": "granite-4.1-8b"}'

curl -s -m 30 -X POST http://127.0.0.1:8880/api/engineer/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "say hello in five words"}'
```

第一条返回 `{"ok":true}`，第二条返回里有 AI 的 `answer` 字段，就说明 midware → LM Studio 通了。

---

## 步骤4：开 TORCS

**终端 C**：

```bash
cd ~/F1-simulator
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
TORCS_HOME=~/F1-simulator bash torcs_launcher.sh
```

选赛道进 Quick Race，画面停在 `Initializing Driver scr_server 1...` 就对了，别关这个窗口。

---

## 步骤5：开 bot

**终端 D**：

```bash
cd ~/F1-simulator
python3 ai_bot.py --bot --granite
```

看到 `granite=True` 且没有反复出现 `[ModelBroker] error`，就是连上了。

**正常情况下终端会很安静**——每 5 秒问一次 Granite，但只有策略从 NORMAL 变成别的（如 ATTACK）才会打印，安静不代表没在工作。想确认真的在请求，另开终端跑：

```bash
curl -s http://127.0.0.1:8880/api/health | python3 -c 'import sys,json; print(json.load(sys.stdin)["model"]["scheduler"])'
```
`completed` 数字持续增长就说明在正常工作。

---

## 常见坑

| 现象 | 处理 |
|---|---|
| 忘了加 `--bot`，跑完直接 `All tests passed.` 退出 | 重新执行 `python3 ai_bot.py --bot --granite` |
| `[ModelBroker] error: ... Connection refused` | midware 没起，或 base_url 配错了 → 回步骤3 |
| 设了 `TORCS_AI_BASE_URL` 还是不行 | 这个变量对 `--granite` 无效，只对 `lmstudio_smoke_test.py` 有效；配置要走步骤3的 curl |
| TORCS 卡在 `Initializing Driver...` | bot 没连上/没起 → 检查步骤5 |
| TORCS 有声音没画面 | `LIBGL_ALWAYS_SOFTWARE=1` + `GALLIUM_DRIVER=llvmpipe`；仍不行就 `wsl.exe --shutdown` 重开（见 [wslg-black-screen-recovery.md](wslg-black-screen-recovery.md)） |
| 终端很久没打印 `[Granite] ...` | 正常，见步骤5说明 |
