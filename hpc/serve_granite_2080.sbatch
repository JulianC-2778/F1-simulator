#!/bin/bash
# Fallback: serve granite-4.1-8b on TWO rtx_2080 cards in the gpu_short partition.
#
# Why this exists: the `gpu` partition (A100 / rtx_3090) had 141 jobs pending
# while gpu_short had 26. One rtx_2080 cannot hold the 17.6 GB model, but two
# of them with tensor parallelism can -- at the cost of a 6-hour walltime cap.
#
# Differences from serve_granite.sbatch:
#   --dtype float16        Turing (sm_75) has no bf16 support
#   -tp 2                  split the weights across both cards
#   --max-model-len 4096   much less VRAM headroom left over for KV cache
#
# sbatch ~/serve_granite_2080.sbatch

#SBATCH --job-name=granite-serve-2080
#SBATCH --account=coms039904
#SBATCH --partition=gpu_short
#SBATCH --gres=gpu:2
#SBATCH --time=06:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --output=%x-%j.out
#SBATCH --error=%x-%j.out

# VRAM budget, measured on bp1-gpu001 (preflight job 18286828):
#   RTX 2080 Ti, 10.6 GiB usable per card, nothing else resident.
#   weights are 16.4 GiB total -> 8.2 GiB per card under -tp 2.
#   At utilization 0.95 that leaves ~1.9 GiB per card for KV cache + activations.
# --enforce-eager skips CUDA graph capture, which would otherwise eat ~1 GiB of
# that margin. It costs some throughput; drop it if the job proves to have room.
#
# utilization is 0.88, NOT higher: job 18287260 died at 0.95 with
#   "Tried to allocate 98.00 MiB ... only 87.06 MiB is free".
# The utilization budget covers weights + KV cache only. Transient tensors --
# here the tensor-parallel all_gather of a 100352-wide logits chunk -- live
# OUTSIDE it, so the remaining (1 - util) has to be big enough for them.
# 0.88 leaves ~1.2 GiB per card for transients and still gives ~1.1 GiB of KV
# cache, against the 0.63 GiB one 4096-token sequence actually needs.

set -euo pipefail

WORK="/user/work/${USER}"
MODEL_DIR="${MODEL_DIR:-$WORK/granite-4.1-8b}"
VENV="${VENV:-$WORK/venvs/vllm}"
# Two aliases: "ibm-granite" is midware's built-in default (runtime.py api_config),
# so with the tunnel on local port 1234 the project needs no config change at all.
SERVED_NAME="ibm-granite granite"
PRIMARY_NAME="ibm-granite"

PORT=$(( 20000 + SLURM_JOB_ID % 10000 ))
NODE="$(hostname -s)"
LOGIN_HOST="${LOGIN_HOST:-bp1-login.acrc.bris.ac.uk}"

cat > "$HOME/granite_endpoint.txt" <<EOF
job_id  = ${SLURM_JOB_ID}
node    = ${NODE}
port    = ${PORT}
model   = ${PRIMARY_NAME}

1) Run this on your LOCAL machine (WSL), keep the terminal open:
     ssh -N -o ServerAliveInterval=30 -L 1234:${NODE}:${PORT} ${USER}@${LOGIN_HOST}

2) Point midware at it:
     curl -s -X POST http://127.0.0.1:8880/api/config/api \\
       -H "Content-Type: application/json" \\
       -d '{"base_url": "http://127.0.0.1:1234/v1", "model": "${PRIMARY_NAME}"}'
EOF

echo "=== endpoint info ==="
cat "$HOME/granite_endpoint.txt"
echo "====================="

nvidia-smi || true

module load languages/python/3.12.3
source "$VENV/bin/activate"

export HF_HOME="$WORK/.hf"
export HF_HUB_OFFLINE=1
export OUTLINES_CACHE_DIR="$WORK/.outlines"
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

exec vllm serve "$MODEL_DIR" \
  --served-model-name $SERVED_NAME \
  --host 0.0.0.0 \
  --port "$PORT" \
  --dtype float16 \
  --tensor-parallel-size 2 \
  --max-model-len 4096 \
  --max-num-batched-tokens 2048 \
  --gpu-memory-utilization 0.88 \
  --enforce-eager
