# 交接日志 2026-08-12 —— Dashboard 推理面板 + 环境不同步问题

## 一、这次做了什么

按交接文档的优先级 1（"dashboard 显示推理过程"），把 Granite 的 considered/rejected 从 trace 文件接到了 dashboard 上。

| 文件 | 改动 |
|---|---|
| `ai_bot.py` | `reporter.tick(...)` 新增 `details` 字段，把 `strategist.last_considered` / `strategist.last_rejected` 发给 midware（原来这两个值只写进了 trace 文件，从没发过 HTTP）|
| `midware/static/dashboard.html` | Bot 卡片里新增"Granite reasoning"面板：逐条列出 considered 的 factor/value/implication，rejected 选项加删除线 + why，标题栏显示当前策略 |

Schema 没改，`BotStatusUpdate.details: dict[str, Any]` 本来就透传任意 JSON。

**验证方式**：TORCS/LM Studio 那套环境这边起不来，所以是绕过 UDP 直接测的 —— 用 `BotStatusUpdate.model_validate(...)` → `BotStatusService.update(...)` 走了一遍完整 payload，确认 `snapshot.details.reasoning.considered/rejected` 的形状跟 dashboard JS 读的字段（`s.details.reasoning.considered` 等）完全对得上。另外跑了 `tests/bot/`，192/193 通过（1 个既存 flaky 网络测试，跟这次改动无关）。

## 二、发现的问题：两份 clone 不同步

打开 dashboard 截图看不到推理面板，一开始怀疑是 `TORCS_BOT_PROMPT` 没设成 `reasoning`，查下去发现问题更底层：

**这台机器上有两份独立的 clone**：

1. `C:\Users\abcdz\Desktop\ibm\F1-simulator`（= WSL 里的 `/mnt/c/Users/abcdz/Desktop/ibm/F1-simulator`）—— 这次改动提交在这里，现在跟 `origin/main` 同步（`531206b`）。
2. `/home/abcdz/F1-simulator` —— **这才是实际在跑 midware 和 ai_bot.py 的目录**（`ps aux` 确认 `midware/.venv/bin/python -m midware.app`、`ai_bot.py --bot --granite` 都是从这条路径起的）。这份 clone 落后 `origin/main` **16 个 commit**，缺整个 Module 4 PR（`f4ff6ed feat: update ai_bot with Granite strategy integration`）—— **`midware/bot_strategy.py` 这个文件在这份 clone 里根本不存在**，considered/rejected、TraceRecorder 全都没有。

所以 dashboard 截图看不到推理面板，根因不是 prompt 变体、也不是这次的代码有 bug，而是**运行的那份代码本来就没有这个功能**。

另外 `/home/abcdz/F1-simulator` 有 67 个文件是本地已修改未提交的状态，查过内容 —— 基本都是 `export/` 和 `src/drivers/scr_server/` 下的头文件（TORCS C++ 那边 `./configure && make` 产生的构建副本/时间戳变化），加上几个从没提交过的本地脚本（`start_midware.sh`、`start_torcs.sh`、`docs/block-mode-status.md`、`docs/testing-plan.pdf`）。看起来是构建产物，不是真正的源码改动，但没有直接 `git pull`，需要先 `git stash` 再拉。

## 三、接下来要做的（按顺序）

1. **同步代码**（在 `/home/abcdz/F1-simulator` 里）：
   ```bash
   cd /home/abcdz/F1-simulator
   git stash            # 67 个构建产物改动先存起来，不会丢
   git pull             # 43f68ad → 531206b，会拉到 bot_strategy.py 等 Module 4 代码
   ```
   `git stash` 存的东西不需要 `pop` 回来也没关系，都是构建产物；如果 `make` 之后想验证没坏，可以事后 `git stash pop` 看一眼再决定要不要留着。

2. **重启 midware，这次要带上环境变量**（在起 midware 的那个终端，即 pid 529 所在的窗口）：
   ```bash
   # Ctrl+C 停掉旧的 midware 进程
   export TORCS_BOT_PROMPT=reasoning   # 必须在这个终端 export，ai_bot 那边设没用
   python3 -m midware.app
   ```

3. **重启 ai_bot.py**：直接在 dashboard 上点 Stop ai_bot.py → Start ai_bot.py 即可（进程是 midware 用 `[sys.executable, "-u", "ai_bot.py", "--bot", "--granite"]` 拉起的，会自动继承第 2 步 export 的环境变量）。

4. **浏览器硬刷新**（Ctrl+Shift+R），确保加载的是新版 `dashboard.html`。

## 四、验证是否生效

- Trace 文件（老办法，交接文档里提过）：
  ```bash
  grep '"kind": "decision"' race*.jsonl | tail -1
  ```
  `"considered"` 非空 = Granite 端在推理。

- Dashboard 新增的验证点：Bot 卡片里应该出现"Granite reasoning"面板，列出考虑的因素和被排除的选项。如果 bot 已连接、trace 文件里 considered 非空，但面板还是不出现 → 大概率是浏览器缓存了旧的 `dashboard.html`，再硬刷新一次。
