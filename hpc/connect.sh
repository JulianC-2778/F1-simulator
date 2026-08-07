#!/usr/bin/env bash
# Open the SSH tunnel to whichever GPU node the Granite job landed on.
#
# The node and port change every time the job is resubmitted (PORT is derived
# from SLURM_JOB_ID), so this reads them back out of the endpoint file the job
# wrote instead of making you copy them by hand.
#
# Run this from WSL, not PowerShell -- midware resolves 127.0.0.1 inside WSL's
# own network namespace, so a tunnel opened on the Windows side is invisible to it.
#
#   ./hpc/connect.sh              # tunnel local :1234 -> the running job
#   LOCAL_PORT=1235 ./hpc/connect.sh

set -euo pipefail

LOGIN="${LOGIN:-sg25291@bp1-login.acrc.bris.ac.uk}"
LOCAL_PORT="${LOCAL_PORT:-1234}"

if ss -ltn "sport = :${LOCAL_PORT}" 2>/dev/null | grep -q LISTEN; then
    echo "!! Local port ${LOCAL_PORT} is already in use."
    echo "   Either an old tunnel is still up (fine -- just use it), or LM Studio"
    echo "   is forwarded there. Kill it, or rerun with LOCAL_PORT=1235."
    exit 1
fi

echo ".. reading endpoint from ${LOGIN}"
info="$(ssh -o BatchMode=yes "$LOGIN" 'cat ~/granite_endpoint.txt 2>/dev/null')" || {
    echo "!! Could not read ~/granite_endpoint.txt."
    echo "   Set up key auth first (ssh-copy-id), or the job has never run."
    exit 1
}

field() { awk -F'= *' -v k="$1" '$1 ~ "^"k {print $2; exit}' <<<"$info"; }
job="$(field job_id)"
node="$(field node)"
port="$(field port)"
model="$(field model)"

if [[ -z "$node" || -z "$port" ]]; then
    echo "!! Endpoint file looks malformed:"; echo "$info"; exit 1
fi

state="$(ssh -o BatchMode=yes "$LOGIN" "squeue -h -j ${job} -o %T" 2>/dev/null || true)"
if [[ "$state" != "RUNNING" ]]; then
    echo "!! Job ${job} is not RUNNING (state: ${state:-gone})."
    echo "   The endpoint file is stale -- it describes a finished job."
    echo "   Submit again:  ssh ${LOGIN} 'sbatch ~/serve_granite.sbatch'"
    exit 1
fi

cat <<EOF

  job    ${job}  (RUNNING)
  node   ${node}
  port   ${port}
  model  ${model}

  local  http://127.0.0.1:${LOCAL_PORT}/v1

Leave this terminal open. In another one:
  curl -s http://127.0.0.1:${LOCAL_PORT}/v1/models
  cd ~/summer-project/F1-simulator && midware/.venv/bin/python -m midware.app

EOF

exec ssh -N \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
    -L "${LOCAL_PORT}:${node}:${port}" "$LOGIN"
