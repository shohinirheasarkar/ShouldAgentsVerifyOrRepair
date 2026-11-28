#!/bin/bash
#SBATCH --job-name=verify-repair-reader-check
#SBATCH --account=cmsc848n-class
#SBATCH --partition=gpu
#SBATCH --gres=gpu:h100:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=00:30:00
#SBATCH --output=reader-check-%j.out
#SBATCH --error=reader-check-%j.err

# Focused recovery check for the v5 reader regression. One model load; saves
# raw requests/responses even if a behavioral assertion fails. No pilot runs.
set -euo pipefail
if command -v module >/dev/null 2>&1; then
  module purge
  module load cuda
fi
SCRATCH_BASE="/scratch/zt1/project/cmsc848n/user/${USER}"
REPO_DIR="${SLURM_SUBMIT_DIR:-$(pwd)}"
VENV_DIR="${VENV_DIR:-${SCRATCH_BASE}/venv-llm}"
export HF_HOME="${HF_HOME:-${SCRATCH_BASE}/hf_cache}"
export LLM_REVISION="${LLM_REVISION:-a09a35458c702b33eeacc393d103063234e8bc28}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export PYTHONPATH="${REPO_DIR}/src"
export LLM_TEST_OUTPUT_DIR="${OUT_DIR:-${SCRATCH_BASE}/reader-check-${SLURM_JOB_ID}}"
mkdir -p "${LLM_TEST_OUTPUT_DIR}"
source "${VENV_DIR}/bin/activate"
python --version
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
python -m pytest -q tests/test_llm_validation.py tests/test_llm_operations.py \
  tests/test_answer_reader_and_scoring.py tests/test_dirty_bit.py
RUN_LLM=1 python -m pytest -o addopts='' tests/test_llm_optional.py -v -s \
  --junitxml="${LLM_TEST_OUTPUT_DIR}/pytest.xml"
