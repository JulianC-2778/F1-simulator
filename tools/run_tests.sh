#!/usr/bin/env bash
#
# F1-simulator 分层测试执行器。
#
#   bash tools/run_tests.sh              # L0-L2：静态检查 + 离线单测 + 集成测试
#   bash tools/run_tests.sh --service    # 追加 L3：拉起 midware 跑运行时矩阵和 UDP 冒烟
#   bash tools/run_tests.sh --only L1    # 只跑某一层
#
# L4（模型/TTS/语音）和 L5（真实 TORCS + Overlay）依赖外部环境，
# 不在本脚本内，流程见 docs/testing-plan.md。

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

WITH_SERVICE=0
ONLY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --service) WITH_SERVICE=1 ;;
        --only) ONLY="${2:-}"; shift ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

PASSED=0
FAILED=0
FAILED_NAMES=""

run_step() {
    local name="$1"; shift
    printf '\n\033[1m>>> %s\033[0m\n' "$name"
    if "$@"; then
        printf '\033[32m    PASS  %s\033[0m\n' "$name"
        PASSED=$((PASSED + 1))
    else
        printf '\033[31m    FAIL  %s\033[0m\n' "$name"
        FAILED=$((FAILED + 1))
        FAILED_NAMES="$FAILED_NAMES\n    - $name"
    fi
}

want() {
    [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]
}

# ---------------------------------------------------------------- L0 静态检查
if want L0; then
    printf '\n\033[1;36m===== L0 静态检查 =====\033[0m\n'
    run_step "L0 Python 语法编译" "$PY" -m compileall -q . -x '(\.venv|BUILD|src|export|data)'
    if command -v node > /dev/null 2>&1; then
        run_step "L0 Electron main.js" node --check overlay-app/electron/main.js
        run_step "L0 Electron preload.js" node --check overlay-app/electron/preload.js
        run_step "L0 engineer-renderer.js" node --check overlay-app/src/engineer-renderer.js
        run_step "L0 settings.js" node --check overlay-app/src/settings.js
    else
        printf '    SKIP  L0 Electron 静态检查（未找到 node）\n'
    fi
fi

# ------------------------------------------------------------ L1 离线单元测试
if want L1; then
    printf '\n\033[1;36m===== L1 离线单元测试 =====\033[0m\n'
    run_step "L1 midware 单元测试" "$PY" -m pytest tests/unit -q
    run_step "L1 ai_bot 内置控制/协议自测" "$PY" ai_bot.py
    run_step "L1 track_model 内置赛道模型自测" "$PY" track_model.py
    run_step "L1 A 模块低延迟回归" "$PY" test_a_module_latency.py
fi

# -------------------------------------------------------------- L2 集成测试
if want L2; then
    printf '\n\033[1;36m===== L2 进程内集成测试 =====\033[0m\n'
    run_step "L2 API/WebSocket/UDP/Bot 集成" "$PY" -m pytest tests/integration -q
fi

# ------------------------------------------------- L3 真实服务进程冒烟（可选）
service_smoke() {
    local log=/tmp/f1sim_midware_test.log
    pkill -f 'midware.app' > /dev/null 2>&1
    sleep 1
    nohup "$PY" -m midware.app > "$log" 2>&1 &
    local pid=$!
    local ready=0
    local i
    for i in $(seq 1 30); do
        sleep 1
        if curl -s -m 2 http://127.0.0.1:8880/api/health > /dev/null 2>&1; then
            ready=1
            echo "    midware 就绪（${i}s）"
            break
        fi
    done
    if [ "$ready" -eq 0 ]; then
        echo "    midware 未在 30s 内就绪，日志尾部："
        tail -15 "$log"
        kill "$pid" > /dev/null 2>&1
        return 1
    fi

    local rc=0
    "$PY" tools/runtime_matrix_check.py || rc=1

    echo "    注入一帧假遥测到 UDP 3101 ..."
    "$PY" - <<'PYEOF' || rc=1
import csv, io, socket, time, urllib.request, json
from midware.telemetry import MAIN_CSV_FIELDS

values = {f: "0" for f in MAIN_CSV_FIELDS}
values.update({"seq": "424242", "sim_time": "12.5", "lap": "2", "speedX": "188.0"})
stream = io.StringIO()
csv.writer(stream).writerow([values[f] for f in MAIN_CSV_FIELDS])
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(stream.getvalue().encode(), ("127.0.0.1", 3101))
sock.close()

deadline = time.time() + 3.0
while time.time() < deadline:
    with urllib.request.urlopen("http://127.0.0.1:8880/api/telemetry", timeout=2) as r:
        telemetry = json.loads(r.read()).get("telemetry") or {}
    if int(float(telemetry.get("seq", 0))) == 424242:
        print("    UDP 3101 -> /api/telemetry OK  speedX=%s" % telemetry.get("speedX"))
        raise SystemExit(0)
    time.sleep(0.05)
raise SystemExit("    FAIL: 注入的遥测帧没有出现在共享 store")
PYEOF

    kill "$pid" > /dev/null 2>&1
    pkill -f 'midware.app' > /dev/null 2>&1
    return "$rc"
}

if [ "$WITH_SERVICE" -eq 1 ] && want L3; then
    printf '\n\033[1;36m===== L3 真实服务进程冒烟 =====\033[0m\n'
    run_step "L3 运行时矩阵 + UDP 遥测链路" service_smoke
    # 独立起自己的 midware.app 子进程（随机端口，不占 8880），跟 service_smoke
    # 互不冲突，可以放在同一个 L3 阶段里顺序跑。
    run_step "L3 Commentary 排队模式黑盒冒烟" "$PY" tools/smoke_test_commentary_queue.py
fi

# ---------------------------------------------------------------------- 汇总
printf '\n\033[1m===== 汇总 =====\033[0m\n'
printf '通过 %d，失败 %d\n' "$PASSED" "$FAILED"
if [ "$FAILED" -gt 0 ]; then
    printf '失败项：%b\n' "$FAILED_NAMES"
    exit 1
fi
if [ "$WITH_SERVICE" -eq 0 ]; then
    printf '提示：加 --service 可追加 L3；L4/L5 见 docs/testing-plan.md\n'
fi
exit 0
