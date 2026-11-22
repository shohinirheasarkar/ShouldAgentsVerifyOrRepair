# Step 5: Local LLM VERIFY, REPAIR, and Fixed Answer Reader

This step adds an opt-in local LLM mode (`--mode llm`) for **VERIFY**, **REPAIR**, and **final answer generation** while keeping the deterministic template substrate (`--mode deterministic`, the default), its logs, and its config hashes byte-identical.

## Model, runtime, and compute target

- **Compute target:** UMD Zaratan GPU partition (`--account=cmsc848n-class`, NVIDIA A100/H100 80 GB).
- **Default model:** `Qwen/Qwen2.5-7B-Instruct` (Apache 2.0, ungated; `--llm-model` also supports `meta-llama/Llama-3.1-8B-Instruct`).
- **Runtime & quantization:** `transformers` + `bitsandbytes` 4-bit NF4 (`load_in_4bit=True`, `bnb_4bit_quant_type="nf4"`, `bnb_4bit_use_double_quant=True`, `bnb_4bit_compute_dtype="auto"` → `bfloat16` on A100/H100, `float16` on older GPUs).
- **Decoding:** greedy deterministic (`do_sample=False`, `temperature=None`, `top_p=None`), explicit per-role token caps (`verify=256`, `repair=512` scaling up to `1024` for wide `synthetic-v2` summaries, `reader=128`), per-call `StoppingCriteria` timeout (`--llm-timeout`, default `60s`), bounded validation retry (`--llm-max-retries`, default `1`).
- **Versioned prompts:** `PROMPT_VERSION = "llm-prompts-v3"` in `src/verify_repair/llm/prompts.py`; its SHA-256 `prompt_bundle_sha256()` is logged in `config_snapshot["llm"]` and `--save-llm-manifest`. All retrieved evidence and source facts are wrapped in XML data tags with explicit untrusted-data guards.

## Operations and scoring semantics

1. **LLM VERIFY (`LLMVerifier`, `src/verify_repair/llm/operations.py`):**
   - Resolves current authoritative `FactVersion` records from the immutable ledger and generates a query-scoped `verified_evidence` statement citing exact current `<fact_id>@v<version>` refs.
   - Validated against current facts (rejects unknown/stale citations, missing current values, and leaked superseded values).
   - **Never mutates** stored summary text, `summary_fact_sources`, `summary_fts`, semantic index, or `is_stale`. Timeouts or validation failures roll back episode counters and P2 access history.
2. **LLM REPAIR (`LLMSummaryBuilder`, `src/verify_repair/llm/operations.py`):**
   - Receives `RepairBuildRequest` (old summary text, old/current `FactVersion` records, changed slots) and emits JSON containing `summary_text` plus exact 1:1 `claims` citing `<fact_id>@v<version>`.
   - Validator enforces: exact current provenance per slot (no missing, duplicate, or unsupported claims), preservation of unchanged facts and entities present in the prior summary (preserving FTS5 keyword retrieval), removal of superseded values (unless still active in another slot), and explicit retraction markers for retracted slots.
   - Any timeout, validation failure, or index update failure rolls back SQLite, FTS5, semantic index, P2 access history, and shared `RepairBudget`.
3. **Fixed Answer Reader & Event-Time Scoring (`src/verify_repair/llm/reader.py`, `src/verify_repair/answer_scoring.py`):**
   - Every policy uses the **exact same frozen `AnswerReader`** and prompt, receiving only a policy-neutral `EvidencePacket` (fresh summary, repaired summary, verified current evidence, stale summary on `NO_OP`, or empty evidence on retrieval miss).
   - `score_run_answers()` evaluates generated answers against event-time ledger truth: normalized Exact Match, Token F1, abstention accuracy / false abstentions / missed abstentions (handling retractions and retrieval misses separately), and **outdated-fact errors** (kept strictly separate from substrate `used_stale_source`).

## Zaratan setup and runnable commands

Keep the repository, virtual environment, pip cache, and HuggingFace model cache under `/scratch/zt1/project/cmsc848n/user/$USER/` (avoid filling Zaratan's small home-directory quota).

### 1. One-time login-node setup (with internet access)

```bash
export SCRATCH_BASE="/scratch/zt1/project/cmsc848n/user/$USER"
mkdir -p "$SCRATCH_BASE"
cd "$SCRATCH_BASE"
git clone -b step5-local-llm https://github.com/shohinirheasarkar/ShouldAgentsVerifyOrRepair.git
cd ShouldAgentsVerifyOrRepair

module purge
module load cuda

export UV_CACHE_DIR="$SCRATCH_BASE/uv_cache"
export UV_PYTHON_INSTALL_DIR="$SCRATCH_BASE/uv_python"
export PIP_CACHE_DIR="$SCRATCH_BASE/pip_cache"
export HF_HOME="$SCRATCH_BASE/hf_cache"
mkdir -p "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR" "$PIP_CACHE_DIR" "$HF_HOME"

# Zaratan RHEL8 modules only ship Python 3.8/3.10; install standalone Python 3.11 on scratch via uv
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$SCRATCH_BASE/bin" sh
export PATH="$SCRATCH_BASE/bin:$PATH"

uv venv --python 3.11 "$SCRATCH_BASE/venv-llm"
source "$SCRATCH_BASE/venv-llm/bin/activate"
python --version   # Python 3.11.x
uv pip install '.[dev,llm]'

python scripts/prefetch_llm_model.py \
  --model Qwen/Qwen2.5-7B-Instruct \
  --output-json "$HF_HOME/qwen25_7b_snapshot.json"
```

Note the printed `model_revision` SHA so you can optionally pin `LLM_REVISION=<SHA>` in batch runs.

### 2. Submit offline GPU validation job on Zaratan

```bash
cd "/scratch/zt1/project/cmsc848n/user/$USER/ShouldAgentsVerifyOrRepair"
sbatch scripts/zaratan_step5.sbatch
```

This runs completely offline (`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`) using `--account=cmsc848n-class` and defaults to `VENV_DIR=/scratch/zt1/project/cmsc848n/user/$USER/venv-llm` and `HF_HOME=/scratch/zt1/project/cmsc848n/user/$USER/hf_cache`, executing:
1. Core offline test suite (`python -m pytest -q`)
2. Live 4-bit integration test (`RUN_LLM=1 python -m pytest -o addopts='' -m llm -v`)
3. Step 5.6 canonical Maryland → Boston end-to-end check (`scripts/run_canonical_llm.py --mode llm`)
4. Toy trace under `verify-only`, `eager-refresh`, `repair-on-first-access`, and `periodic-refresh-stale`, saving logs, scored answers, and `runtime_manifest.json`.

### 3. Single toy command (on any CUDA GPU with cached weights)

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m verify_repair.cli run-toy \
  --policy verify-only --mode llm \
  --llm-model Qwen/Qwen2.5-7B-Instruct --llm-quantization nf4 \
  --answers-jsonl /tmp/toy-verify-answers.jsonl \
  --save-llm-manifest /tmp/runtime-manifest.json
```

### 4. Local offline verification (CPU/Mac, no GPU or downloads required)

```bash
python -m pytest -q
python scripts/run_canonical_llm.py --mode fake
```

## Status & next step

- **Completed locally (offline):** Core LLM client/prompts/validators, `LLMVerifier`, `LLMSummaryBuilder`, frozen `AnswerReader`, event-time answer scoring, CLI `--mode llm` integration, Zaratan prefetch/Slurm scripts, and full offline regression suite.
- **Pending Zaratan GPU run:** Execute `scripts/prefetch_llm_model.py` + `sbatch scripts/zaratan_step5.sbatch` on Zaratan to capture the live `runtime_manifest.json` and scored toy/canonical outputs into `docs/step5/`, then proceed to Step 6 (cost profiling & measured-cost replay).
