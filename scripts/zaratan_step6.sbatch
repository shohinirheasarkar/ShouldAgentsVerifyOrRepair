#!/bin/bash
#SBATCH --job-name=verify-repair-step6
#SBATCH --account=cmsc848n-class
#SBATCH --partition=gpu
#SBATCH --gres=gpu:h100:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=48G
#SBATCH --time=01:45:00
#SBATCH --output=step6-%j.out
#SBATCH --error=step6-%j.err

# Step 6 GPU pilots on UMD Zaratan: measured operation costs, answer quality,
# cost profiles, dirty-bit checks (scripts/run_step6_pilot.py), and
# semantic-index maintenance profiling (scripts/profile_semantic_index.py).
# Prerequisites: the Step 5 setup (docs/step5/README.md: venv-llm and cached
# Qwen weights). For the semantic profile, also a venv with the semantic extra
# and cached BGE-M3 weights:
#   uv venv --python 3.11 $SCRATCH_BASE/venv-semantic
#   source $SCRATCH_BASE/venv-semantic/bin/activate && uv pip install '.[semantic]'
#   python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-m3')"
# Submit from the repository root:  sbatch scripts/zaratan_step6.sbatch

set -euo pipefail

if command -v module >/dev/null 2>&1; then
  module purge
  module load cuda
fi

SCRATCH_BASE="/scratch/zt1/project/cmsc848n/user/${USER}"
REPO_DIR="${SLURM_SUBMIT_DIR:-$(pwd)}"
VENV_DIR="${VENV_DIR:-${SCRATCH_BASE}/venv-llm}"
SEMANTIC_VENV_DIR="${SEMANTIC_VENV_DIR:-${SCRATCH_BASE}/venv-semantic}"
HF_HOME="${HF_HOME:-${SCRATCH_BASE}/hf_cache}"
OUT_DIR="${OUT_DIR:-${REPO_DIR}/docs/step6/zaratan_${SLURM_JOB_ID:-local}}"
MODEL_ID="${LLM_MODEL:-Qwen/Qwen2.5-7B-Instruct}"

export HF_HOME HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export PYTHONPATH="${REPO_DIR}/src"

if [[ -z "${LLM_REVISION:-}" && -f "${HF_HOME}/qwen25_7b_snapshot.json" ]]; then
  LLM_REVISION="$(python3 -c "import json; print(json.load(open('${HF_HOME}/qwen25_7b_snapshot.json'))['model_revision'])")"
fi
MODEL_REV_ARGS=()
if [[ -n "${LLM_REVISION:-}" ]]; then
  MODEL_REV_ARGS=(--llm-revision "${LLM_REVISION}")
fi

mkdir -p "$(dirname "${OUT_DIR}")"

echo "=== Environment ==="
git -C "${REPO_DIR}" log -1 --format='code commit: %H'
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
nproc

echo "=== 1. Step 6 pilots (one model load) ==="
source "${VENV_DIR}/bin/activate"
python --version
python scripts/run_step6_pilot.py \
  --mode llm \
  --llm-model "${MODEL_ID}" \
  "${MODEL_REV_ARGS[@]}" \
  --llm-quantization nf4 \
  --llm-dtype auto \
  --llm-max-retries 2 \
  --cost-queries "${COST_QUERIES:-3000}" \
  --quality-queries "${QUALITY_QUERIES:-1000}" \
  --dirty-sample "${DIRTY_SAMPLE:-300}" \
  --output-dir "${OUT_DIR}"
deactivate

echo "=== 2. Semantic-index profile (Step 6.3) ==="
if [[ -f "${SEMANTIC_VENV_DIR}/bin/activate" ]]; then
  source "${SEMANTIC_VENV_DIR}/bin/activate"
  python scripts/profile_semantic_index.py --sizes 10 100 1000 \
    --output "${OUT_DIR}/semantic_index_profile.json" \
    || echo "semantic-index profile failed (see step6-${SLURM_JOB_ID}.err)"
else
  echo "skipped: ${SEMANTIC_VENV_DIR} not found"
fi

nvidia-smi > "${OUT_DIR}/nvidia_smi.txt"
echo "=== Step 6 Zaratan pilots complete: ${OUT_DIR} ==="
