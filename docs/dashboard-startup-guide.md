# Dashboard 与 TORCS 启动说明

这份文档记录当前项目中已经验证过的本地启动方式。当前环境的关键点是：

- Dashboard / middleware / LM Studio 调用运行在 Windows 侧可访问的本地服务上。
- TORCS 游戏本体运行在 WSL 的 Linux 图形环境中。
- 不要直接从 Windows 项目副本启动 TORCS，因为当前 Windows 副本没有 `BUILD/bin/torcs`。
- 当前可运行的 TORCS 编译目录在 WSL 内部：`/home/yejian/torcs`。

## 1. 启动 Dashboard 服务

在 Windows PowerShell 中进入当前项目目录：

```powershell
cd "C:\Users\yejian\Desktop\F1项目\F1-simulator"
```

启动 middleware 服务：

```powershell
wsl.exe bash -lc 'cd "/mnt/c/Users/yejian/Desktop/F1项目/F1-simulator" && nohup midware/.venv/bin/python -m midware.app > /tmp/f1-main-dashboard.log 2>&1 < /dev/null & disown'
```

服务启动后访问：

```text
http://127.0.0.1:8880/static/dashboard.html
```

说明：

- 端口是 `8880`，来源于 `config.py` / `config.json`。
- 当前更稳定的页面入口是 `/static/dashboard.html`。
- 服务刚启动时可能需要几十秒导入模块，短时间内打不开属于正常现象。

## 2. 检查 Dashboard 是否启动成功

在 Windows PowerShell 中检查：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8880/api/health
```

如果只想确认页面是否能打开：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8880/static/dashboard.html
```

如果服务没有起来，查看 WSL 侧日志：

```powershell
wsl.exe bash -lc "tail -120 /tmp/f1-main-dashboard.log"
```

停止 middleware 服务：

```powershell
wsl.exe bash -lc "pkill -f 'midware/.venv/bin/python -m midware.app' || true"
```

## 3. 启动 TORCS 游戏

当前实际可运行的游戏目录是 WSL 内部的：

```bash
/home/yejian/torcs
```

推荐从 Windows PowerShell 调用 WSL 侧启动器：

```powershell
wsl.exe bash -lc "cd /home/yejian/torcs && setsid -f ./torcs_launcher.sh > /tmp/f1-torcs-launch.log 2>&1 < /dev/null"
```

这个启动器会做几件重要事情：

- 设置 WSLg 显示环境：`DISPLAY=:0`。
- 启用软件渲染：`LIBGL_ALWAYS_SOFTWARE=1` 和 `GALLIUM_DRIVER=llvmpipe`。
- 从 `/home/yejian/torcs/BUILD` 启动 `./bin/torcs -s`。
- 使用 `xdotool` 查找 `torcs-bin` 窗口。
- 把窗口移动到可见位置，并调整为 `800x600`。

不要优先使用 Windows 项目副本中的：

```text
C:\Users\yejian\Desktop\F1项目\F1-simulator\torcs_launcher.sh
```

原因是当前这个 Windows 项目副本没有 `BUILD/bin/torcs`，直接从这里启动会找不到游戏可执行文件。

## 4. 检查 TORCS 是否真的可见

只看到进程或听到声音不代表启动成功。需要检查窗口是否存在。

查看 TORCS 进程：

```powershell
wsl.exe bash -lc "pgrep -a torcs || true"
```

查看启动日志：

```powershell
wsl.exe bash -lc "tail -100 /tmp/f1-torcs-launch.log"
```

查看 WSLg 窗口树：

```powershell
wsl.exe bash -lc "DISPLAY=:0 xwininfo -root -tree 2>/dev/null | head -60"
```

正常情况下能看到类似：

```text
/home/yejian/torcs/BUILD/lib/torcs/torcs-bin
800x600
```

也可以直接查窗口：

```powershell
wsl.exe bash -lc "DISPLAY=:0 xdotool search --name torcs-bin 2>/dev/null | head"
```

如果有窗口 ID，可以再次强制移动和激活：

```powershell
wsl.exe bash -lc "WIN=$(DISPLAY=:0 xdotool search --name torcs-bin 2>/dev/null | head -1); if [ -n \"$WIN\" ]; then DISPLAY=:0 xdotool windowmap \"$WIN\" windowmove \"$WIN\" 0 0 windowsize \"$WIN\" 800 600 windowraise \"$WIN\" windowactivate \"$WIN\"; fi"
```

## 5. 黑屏或有声音但看不到画面时

这是之前已经验证过的 WSLg 图形会话问题。最可靠的恢复方式不是反复杀 `torcs`，而是重置 WSL 图形会话。

在 Windows PowerShell 执行：

```powershell
wsl.exe --shutdown
```

然后按顺序重新启动：

1. 启动 Dashboard middleware。
2. 等 `http://127.0.0.1:8880/api/health` 可以访问。
3. 使用 `/home/yejian/torcs/torcs_launcher.sh` 启动 TORCS。
4. 用 `xwininfo` 或 `xdotool` 确认 `torcs-bin` 窗口存在。

注意：`wsl.exe --shutdown` 会关闭 WSL 内的 middleware 服务，所以执行后需要重新启动 Dashboard。

## 6. 当前测试时的推荐顺序

完整启动顺序如下：

```powershell
cd "C:\Users\yejian\Desktop\F1项目\F1-simulator"

wsl.exe bash -lc 'cd "/mnt/c/Users/yejian/Desktop/F1项目/F1-simulator" && nohup midware/.venv/bin/python -m midware.app > /tmp/f1-main-dashboard.log 2>&1 < /dev/null & disown'

Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8880/api/health

wsl.exe bash -lc "cd /home/yejian/torcs && setsid -f ./torcs_launcher.sh > /tmp/f1-torcs-launch.log 2>&1 < /dev/null"
```

然后在浏览器打开：

```text
http://127.0.0.1:8880/static/dashboard.html
```

## 7. 常见状态说明

- Dashboard 能打开，但 `telemetry.ok=false`：middleware 正常，游戏遥测还没有进入。
- Coach 显示 `degraded`：通常是还没有收到 TORCS 遥测帧。
- Bot 显示 `disconnected`：说明 `ai_bot.py` 还没有连接到 TORCS 的 SCR 端口。
- 游戏有声音但没有窗口：优先执行 `wsl.exe --shutdown`，然后重新按顺序启动。

## 8. 相关文件

- Dashboard 页面：`midware/static/dashboard.html`
- Dashboard 服务入口：`midware/app.py`
- 共享端口配置：`config.py` 和 `config.json`
- Windows 项目副本启动器：`torcs_launcher.sh`
- 当前实际使用的 WSL 启动器：`/home/yejian/torcs/torcs_launcher.sh`
- 黑屏恢复说明：`docs/wslg-black-screen-recovery.md`
