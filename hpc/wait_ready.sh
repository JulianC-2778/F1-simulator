#!/usr/bin/env bash
# Run this ON THE CLUSTER (login node). It watches whichever granite-serve job
# is queued, waits for vLLM to finish loading, and then prints the exact ssh
# tunnel command to paste into WSL.
#
# Watching by hand means polling squeue, then guessing when the log is far
# enough along, then re-reading the endpoint file. This does all three and
# tells you plainly whether it worked or died.
#
#   bash ~/wait_ready.sh

set -uo pipefail

INTERVAL=20
LOGIN_HOST="${LOGIN_HOST:-bp1-login.acrc.bris.ac.uk}"

echo "watching for granite-serve jobs (Ctrl+C to stop, jobs keep running)"

while true; do
    # Newest granite-serve* job still in the queue, whatever partition.
    # %N must come LAST: it is empty while the job is PENDING, and a trailing
    # empty field is harmless whereas an empty one in the middle shifts every
    # column after it and loses the job name.
    read -r jobid state node <<<"$(squeue -h -u "$USER" -o "%i %T %j %N" \
        | awk '$3 ~ /^granite-serve/ {print $1, $2, $4}' | sort -rn | head -1)"

    if [[ -z "${jobid:-}" ]]; then
        echo "no granite-serve job in the queue -- submit one:"
        echo "  sbatch ~/serve_granite_2080.sbatch     # 2x RTX 2080 Ti, gpu_short"
        echo "  sbatch ~/serve_granite.sbatch          # A100 MIG slice, gpu"
        exit 1
    fi

    if [[ "$state" != "RUNNING" ]]; then
        printf "\r  %s  %s ... " "$jobid" "$state"
        sleep "$INTERVAL"
        continue
    fi

    log="$(ls -t "$HOME"/granite-serve*-"$jobid".out 2>/dev/null | head -1)"
    if [[ -z "$log" ]]; then
        printf "\r  %s  RUNNING, waiting for log ... " "$jobid"
        sleep "$INTERVAL"
        continue
    fi

    if grep -q "Application startup complete" "$log"; then
        port="$(awk -F'= *' '/^port/{print $2; exit}' "$HOME/granite_endpoint.txt")"
        cat <<EOF

READY -- job $jobid on $node, port $port

  1. In WSL, paste this and leave it open:

ssh -N -o ServerAliveInterval=30 -L 1234:${node}:${port} ${USER}@${LOGIN_HOST}

  2. In a second WSL window:

cd ~/summer-project/F1-simulator && .venv/bin/python -m midware.app

  3. In a third, check the whole chain:

curl -s -m 60 -X POST http://127.0.0.1:8880/api/engineer/ask -H "Content-Type: application/json" -d '{"question": "say hello in five words"}'

EOF
        exit 0
    fi

    if grep -qE "Engine core initialization failed|OutOfMemoryError|Traceback" "$log"; then
        echo
        echo "FAILED -- job $jobid died during startup. Root cause:"
        grep -m3 -E "OutOfMemoryError|ValueError|RuntimeError|available KV cache" "$log" | cut -c1-200
        echo
        echo "full log: $log"
        exit 1
    fi

    stage="$(grep -oE "Loading safetensors checkpoint shards: *[0-9]+%" "$log" | tail -1)"
    printf "\r  %s  RUNNING on %s  %s ... " "$jobid" "$node" "${stage:-starting}"
    sleep "$INTERVAL"
done
