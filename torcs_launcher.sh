#!/bin/bash
# TORCS WSLg launcher.
# Important: keep this script LF-only because it is executed from WSL.
# Recovery note:
# If TORCS has audio but no visible window under WSLg, the most reliable fix we
# have observed is to run `wsl.exe --shutdown` from Windows PowerShell, then
# restart WSL and relaunch this script. See docs/wslg-black-screen-recovery.md.

set -u

export DISPLAY=:0
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe

TORCS_HOME="${TORCS_HOME:-/home/yejian/torcs}"
cd "$TORCS_HOME/BUILD" || exit 1

if [ ! -x ./bin/torcs ]; then
    echo "TORCS binary not found at $TORCS_HOME/BUILD/bin/torcs"
    exit 1
fi

# Launch TORCS.
./bin/torcs -s &
TORCS_PID=$!

echo "TORCS PID: $TORCS_PID"
echo "If audio plays but no window appears, restart WSL from Windows with: wsl.exe --shutdown"

# Wait and reposition the window. The wrapper PID lookup can miss the real
# top-level XWayland window, so fall back to title-based lookup as well.
WINDOW_FOUND=0
for i in $(seq 1 30); do
    sleep 0.3
    WIN=$(xdotool search --pid "$TORCS_PID" 2>/dev/null | head -1)
    if [ -z "$WIN" ]; then
        WIN=$(xdotool search --name "torcs-bin" 2>/dev/null | head -1)
    fi
    if [ -n "$WIN" ]; then
        WINDOW_FOUND=1
        echo "Found torcs window: $WIN at attempt $i"
        xdotool windowmap "$WIN" 2>/dev/null
        xdotool windowmove "$WIN" 0 0 2>/dev/null
        xdotool windowsize "$WIN" 800 600 2>/dev/null
        xdotool windowraise "$WIN" 2>/dev/null
        xdotool windowactivate "$WIN" 2>/dev/null
        echo "Window repositioned to 0,0"
        break
    fi
done

if [ "$WINDOW_FOUND" -eq 0 ]; then
    echo "No TORCS window was detected by xdotool during startup."
    echo "Next step: run wsl.exe --shutdown in Windows PowerShell, reopen WSL, and launch again."
fi

sleep 2
echo "=== Window tree ==="
xwininfo -root -tree 2>/dev/null | head -20
echo "=== Weston log ==="
tail -8 /mnt/wslg/weston.log 2>/dev/null

wait "$TORCS_PID" 2>/dev/null
