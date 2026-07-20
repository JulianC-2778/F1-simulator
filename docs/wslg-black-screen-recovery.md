# TORCS 在 WSLg 下黑屏的排查与恢复

## 现象

- TORCS 已经启动，可以听到游戏声音。
- WSL 或日志里能看到 `torcs` 进程存在。
- 但桌面上没有正常可见的游戏窗口，或者窗口只有黑屏。

## 这次项目里验证过的有效恢复方式

当前最稳定的恢复手段不是反复重启 `torcs` 进程，而是直接重启整个 WSL 图形会话：

1. 在 Windows PowerShell 中执行：

   ```powershell
   wsl.exe --shutdown
   ```

2. 重新打开 WSL 终端。
3. 再次进入 TORCS 构建目录并启动：

   ```bash
   cd ~/F1-simulator
   ./torcs_launcher.sh
   ```

## 为什么这样记录

这次排查过程中，单独重启 `torcs`、重新拉起中间件、重新定位窗口都不能稳定解决问题；`wsl.exe --shutdown` 后重新进入 WSL，再启动 TORCS，窗口恢复正常显示。

因此目前可以把结论固化为：

- 如果“有声音但没画面”，优先怀疑 WSLg 图形会话状态异常。
- 第一优先级恢复动作是 `wsl.exe --shutdown`，不是只杀掉 `torcs`。

## 辅助确认项

### 1. 确认启动脚本存在

项目内使用的启动脚本是：

- [`torcs_launcher.sh`](../torcs_launcher.sh)

这个脚本已经加入了启动提示，会在窗口未被检测到时提醒执行 `wsl.exe --shutdown`。

### 2. 确认仓库目录完整

默认文档假设仓库直接克隆在 WSL 的 Linux 文件系统中：

```bash
cd ~/F1-simulator
ls midware/commentary.py config.json
```

如果仓库位于 `/mnt/c` 等 Windows 挂载目录，性能和文件权限行为可能不同；不要把 `midware` 单独软链接到另一份仓库，以免代码版本失配。

### 3. 确认 TORCS 可执行文件位置

当前验证过的路径：

```bash
cd ~/F1-simulator/BUILD
ls bin/torcs
```

## 建议的后续操作顺序

后面如果再次出现黑屏，建议统一按这个顺序处理：

1. 先确认是否“有声音但无窗口”。
2. 如果是，直接在 Windows 执行 `wsl.exe --shutdown`。
3. 重开 WSL 后，用 `torcs_launcher.sh` 启动。
4. 如果仍异常，再检查仓库完整性和 `BUILD/bin/torcs` 是否正常。

这套顺序已经比“先各种重启单个进程”更省时间，也更适合交接给组内其他同学。

## 这次额外确认的根因

这次还额外踩到了一个不是 WSLg 本身、而是启动方式的问题：

- 直接在 WSL 里执行 Windows 挂载路径下的脚本时，可能遇到行尾问题，例如：

  ```bash
  bash /mnt/c/path/to/F1-simulator/torcs_launcher.sh
  ```

- 该脚本当时带有 Windows `CRLF` 行尾，WSL 中会报：

  ```text
  $'\r': command not found
  ```

- 结果是脚本看起来“执行了几行”，但实际上没有正确进入仓库的 `BUILD` 目录，TORCS 也没有按预期启动。

因此现在把这个经验也固化下来：

1. `.sh` 脚本必须保持 `LF` 行尾。
2. 在不确定脚本同步状态时，优先执行 WSL 侧的启动器：

   ```bash
   cd ~/F1-simulator
   ./torcs_launcher.sh
   ```

3. 如果仓库来自 Windows 文件系统，先确认脚本没有 `CRLF`；建议直接在 WSL 的 Linux 文件系统中克隆并运行仓库。

## 本次已经补上的防呆

- 仓库已增加 `.gitattributes`，强制 `*.sh` 使用 `LF`。
- `torcs_launcher.sh` 已增加更稳的窗口查找逻辑：
  先按 PID 查找，失败时再按 `torcs-bin` 窗口标题回退查找。
- 启动脚本已经明确标注：它必须以 `LF` 形式保存。
