#!/bin/bash
# Serve granite-4.1-8b as an OpenAI-compatible endpoint on a BluePebble GPU node.
#
# The rest of this project talks to an OpenAI-compatible /v1 endpoint (see
# midware/shared/model_gateway.py). This job replaces the Windows LM Studio
# server from docs/torcs-granite-quickstart.md with a GPU node, so nothing
# else in the pipeline has to change -- you only repoint base_url.
#
# Submit:  sbatch hpc/serve_granite.sbatch
# Then:    cat ~/granite_endpoint.txt   (gives you the exact ssh tunnel command)

#SBATCH --job-name=granite-serve
#SBATCH --account=coms039904
#SBATCH --partition=gpu
#SBATCH --gres=gpu:a100:1
#SBATCH --time=08:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --output=%x-%j.out
#SBATCH --error=%x-%j.out

# NOTE: the A100s on this cluster are in MIG mode -- `--gres=gpu:a100:1` gets you
# a ~19.5 GiB slice (bp1-gpu035, 14 SMs), NOT the full 80 GB card. With weights at
# 16.38 GiB that leaves very little room, so:
#   max-model-len 4096  8192 needed 1.25 GiB of KV cache and only 1.05 GiB was free
#                       (job 18286830). This project's prompts are far shorter.
#   utilization 0.90    at 4096 the same 1.05 GiB of KV cache is comfortably more
#                       than the 0.63 GiB one sequence needs, and the leftover
#                       ~2 GiB covers transient tensors. Do NOT raise this: the
#                       twin 2080 job OOMed at 0.95 because transients live
#                       outside the utilization budget.
#
# NOTE on GPU choice: the model is 17.6 GB of bf16 safetensors.
#   gpu:a100:1     <- default, plenty of headroom
#   gpu:rtx_3090:1 <- fallback if the A100 queue is long (24 GB, still fits)
#   gpu_short partition is rtx_2080 (11 GB) only -- the model does NOT fit there.
# V100 is bf16-incapable (Volta); if you ever land on one, add --dtype float16.

set -euo pipefail

WORK="/user/work/${USER}"
MODEL_DIR="${MODEL_DIR:-$WORK/granite-4.1-8b}"
VENV="${VENV:-$WORK/venvs/vllm}"
# Two aliases: "ibm-granite" is midware's built-in default (runtime.py api_config),
# so with the tunnel on local port 1234 the project needs no config change at all.
SERVED_NAME="ibm-granite granite"
PRIMARY_NAME="ibm-granite"

# Per-job port so two of your jobs on the same node never collide.
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

2) Check the tunnel:
     curl -s http://127.0.0.1:1234/v1/models

3) Start midware as usual -- local port 1234 + model "ibm-granite" are already
   its defaults, so nothing else is needed:
     midware/.venv/bin/python -m midware.app

   (Only if you used a different local port:)
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

# HF_HOME on /user/work: the home quota is small and vLLM/transformers will
# otherwise scatter caches into ~/.cache and fail mid-load.
export HF_HOME="$WORK/.hf"
export HF_HUB_OFFLINE=1
export OUTLINES_CACHE_DIR="$WORK/.outlines"
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

# `vllm serve` is the stable CLI in 0.2x; the older
# `python -m vllm.entrypoints.openai.api_server` path still works if this moves.
exec vllm serve "$MODEL_DIR" \
  --served-model-name $SERVED_NAME \
  --host 0.0.0.0 \
  --port "$PORT" \
  --max-model-len 4096 \
  --max-num-batched-tokens 2048 \
  --gpu-memory-utilization 0.90
