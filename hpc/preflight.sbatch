#!/bin/bash
# 5-minute preflight before committing to a long GPU job.
#
# Checks the one thing that can't be checked from the login node: whether the
# node's NVIDIA driver is new enough for the CUDA 13 wheels that `pip install
# vllm` pulled in (torch 2.11 / cuda-toolkit 13.0.2 need driver >= 580).
#
# Short walltime on purpose -- the backfill scheduler slots 5-minute jobs in
# quickly even when the A100s are busy.
#
# sbatch ~/preflight.sbatch  &&  squeue -u $USER

#SBATCH --job-name=granite-preflight
#SBATCH --account=coms039904
#SBATCH --partition=gpu
#SBATCH --gres=gpu:a100:1
#SBATCH --time=00:05:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --output=%x-%j.out
#SBATCH --error=%x-%j.out

set -uo pipefail

WORK="/user/work/${USER}"
VENV="$WORK/venvs/vllm"
MODEL_DIR="$WORK/granite-4.1-8b"

echo "### node: $(hostname -s)"
echo
echo "### nvidia-smi (driver version is the number that matters)"
nvidia-smi
echo
echo "### torch / vllm"
module load languages/python/3.12.3
source "$VENV/bin/activate"
export HF_HOME="$WORK/.hf"

python - <<'PY'
import torch
print("torch        :", torch.__version__)
print("torch.cuda   :", torch.version.cuda)
print("is_available :", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device       :", torch.cuda.get_device_name(0))
    cc = torch.cuda.get_device_capability(0)
    print("capability   :", cc, "(bf16 needs >= (8,0))")
    free, total = torch.cuda.mem_get_info()
    print(f"vram free    : {free/2**30:.1f} GiB / {total/2**30:.1f} GiB")
    x = torch.randn(1024, 1024, dtype=torch.bfloat16, device="cuda")
    print("bf16 matmul  :", (x @ x).sum().item() is not None)
import vllm
print("vllm         :", vllm.__version__)
PY

echo
echo "### model dir readable from compute node"
ls -la "$MODEL_DIR" | head -5
echo
echo "### preflight done"
