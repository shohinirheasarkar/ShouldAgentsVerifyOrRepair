#!/bin/bash
#SBATCH --job-name=verify-repair-step5
#SBATCH --account=cmsc848n-class
#SBATCH --partition=gpu
#SBATCH --gres=gpu:h100:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=00:45:00
#SBATCH --output=step5-%j.out
#SBATCH --error=step5-%j.err

# Reproducible UMD Zaratan GPU job for Step 5 live 4-bit Qwen2.5-7B-Instruct validation.
# Prerequisites (run once on a Zaratan login node):
#   1. Clone repo under /scratch/zt1/project/cmsc848n/user/$USER/ShouldAgentsVerifyOrRepair
#   2. Create Python 3.11+ venv under /scratch/zt1/project/cmsc848n/user/$USER/venv-llm
#      with `uv pip install '.[dev,llm]'`
#   3. Download Qwen2.5-7B-Instruct once into /scratch/zt1/project/cmsc848n/user/$USER/hf_cache:
#        python scripts/prefetch_llm_model.py --model Qwen/Qwen2.5-7B-Instruct \
#          --output-json /scratch/zt1/project/cmsc848n/user/$USER/hf_cache/qwen25_7b_snapshot.json
# Submit from repository root:
#   sbatch scripts/zaratan_step5.sbatch

set -euo pipefail

# Load Zaratan CUDA module; Python 3.11+ comes from VENV_DIR on scratch (e.g., via `uv`).
if command -v module >/dev/null 2>&1; then
  module purge
  module load cuda
fi

SCRATCH_BASE="/scratch/zt1/project/cmsc848n/user/${USER}"
REPO_DIR="${SLURM_SUBMIT_DIR:-$(pwd)}"
VENV_DIR="${VENV_DIR:-${SCRATCH_BASE}/venv-llm}"
HF_HOME="${HF_HOME:-${SCRATCH_BASE}/hf_cache}"
OUT_DIR="${OUT_DIR:-${REPO_DIR}/docs/step5/zaratan_${SLURM_JOB_ID:-local}}"
MODEL_ID="${LLM_MODEL:-Qwen/Qwen2.5-7B-Instruct}"

if [[ ! -d "${HF_HOME}" ]]; then
  echo "ERROR: HF_HOME directory ${HF_HOME} does not exist; run scripts/prefetch_llm_model.py on a login node first" >&2
  exit 1
fi

source "${VENV_DIR}/bin/activate"

# Auto-pin the exact commit SHA saved by scripts/prefetch_llm_model.py when LLM_REVISION is unset.
if [[ -z "${LLM_REVISION:-}" && -f "${HF_HOME}/qwen25_7b_snapshot.json" ]]; then
  LLM_REVISION="$(python -c "import json; print(json.load(open('${HF_HOME}/qwen25_7b_snapshot.json'))['model_revision'])")"
  export LLM_REVISION
fi

MODEL_REV_ARGS=()
if [[ -n "${LLM_REVISION:-}" ]]; then
  MODEL_REV_ARGS=(--llm-revision "${LLM_REVISION}")
fi

export HF_HOME
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export PYTHONPATH="${REPO_DIR}/src"

mkdir -p "${OUT_DIR}"

echo "=== Environment ==="
python --version
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

echo "=== 1. Core offline test suite ==="
python -m pytest -q

echo "=== 2. Opt-in live 4-bit LLM integration test ==="
RUN_LLM=1 python -m pytest -o addopts='' -m llm -v

echo "=== 3. Step 5.6 canonical Maryland->Boston end-to-end check ==="
python scripts/run_canonical_llm.py \
  --mode llm \
  --llm-model "${MODEL_ID}" \
  "${MODEL_REV_ARGS[@]}" \
  --llm-quantization nf4 \
  --llm-dtype auto \
  --output "${OUT_DIR}/canonical_maryland_boston.json"

echo "=== 4. Toy trace under local LLM (verify-only, eager-refresh, repair-on-first-access, periodic-refresh-stale) ==="
for policy in verify-only eager-refresh repair-on-first-access periodic-refresh-stale; do
  MANIFEST_ARGS=()
  if [[ "${policy}" == "verify-only" ]]; then
    MANIFEST_ARGS=(--save-llm-manifest "${OUT_DIR}/runtime_manifest.json")
  fi
  python -m verify_repair.cli run-toy \
    --policy "${policy}" \
    --mode llm \
    --llm-model "${MODEL_ID}" \
    "${MODEL_REV_ARGS[@]}" \
    --llm-quantization nf4 \
    --llm-dtype auto \
    --jsonl "${OUT_DIR}/toy_${policy}_logs.jsonl" \
    --answers-jsonl "${OUT_DIR}/toy_${policy}_answers.jsonl" \
    "${MANIFEST_ARGS[@]}"
done

echo "=== Step 5 Zaratan validation complete: ${OUT_DIR} ==="
