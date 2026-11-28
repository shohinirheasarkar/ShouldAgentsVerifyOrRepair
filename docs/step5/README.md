# Step 5: Local LLM VERIFY, REPAIR, and Fixed Answer Reader

This step adds an opt-in local LLM mode (`--mode llm`) for **VERIFY**, **REPAIR**, and **final answer generation** while keeping the deterministic template substrate (`--mode deterministic`, the default), its logs, and its config hashes byte-identical.

## Model, runtime, and compute target

- **Compute target:** UMD Zaratan GPU partition (`--account=cmsc848n-class`, NVIDIA A100/H100 80 GB).
- **Default model:** `Qwen/Qwen2.5-7B-Instruct` (Apache 2.0, ungated; `--llm-model` also supports `meta-llama/Llama-3.1-8B-Instruct`).
- **Runtime & quantization:** `transformers` + `bitsandbytes` 4-bit NF4 (`load_in_4bit=True`, `bnb_4bit_quant_type="nf4"`, `bnb_4bit_use_double_quant=True`, `bnb_4bit_compute_dtype="auto"` → `bfloat16` on A100/H100, `float16` on older GPUs).
- **Decoding:** greedy deterministic (`do_sample=False`, `temperature=None`, `top_p=None`), explicit per-role token caps (`verify=256`, `repair=512` scaling up to `1024` for wide `synthetic-v2` summaries, `reader=128`), per-call `StoppingCriteria` timeout (`--llm-timeout`, default `60s`), bounded validation retry (`--llm-max-retries`, default `1`).
- **Versioned prompts:** `PROMPT_VERSION = "llm-prompts-v6"` in `src/verify_repair/llm/prompts.py` (`reader-v6`, unchanged `verify-v4` / `repair-v4` prompts); its SHA-256 `prompt_bundle_sha256()` is logged in `config_snapshot["llm"]` and `--save-llm-manifest`. Reader-v6 restores the exact v4 system prompt and adds the deterministic support check below. All retrieved evidence and source facts are wrapped in XML data tags with explicit untrusted-data guards.

## Operations and scoring semantics

1. **LLM VERIFY (`LLMVerifier`, `src/verify_repair/llm/operations.py`):**
   - Resolves current authoritative `FactVersion` records from the immutable ledger and generates a query-scoped `verified_evidence` statement citing exact current `<fact_id>@v<version>` refs.
   - Validated against current facts: explicit entity/attribute mentions in the question require the corresponding current citations and values (or retraction acknowledgments), including unchanged queried facts. Other changed slots need not be mentioned. For paraphrases without literal attribute names, validation grounds the cited facts instead; it is not a general semantic query parser. Unknown/stale citations and superseded-value leakage remain rejected.
   - **Never mutates** stored summary text, `summary_fact_sources`, `summary_fts`, semantic index, or `is_stale`. Timeouts or validation failures roll back episode counters and P2 access history.
2. **LLM REPAIR (`LLMSummaryBuilder`, `src/verify_repair/llm/operations.py`):**
   - Receives `RepairBuildRequest` (old summary text, old/current `FactVersion` records, changed slots) and emits JSON containing `summary_text` plus exact 1:1 `claims` citing `<fact_id>@v<version>`.
   - Validator enforces: exact current provenance per slot (no missing, duplicate, or unsupported claims), preservation of unchanged facts and entities present in the prior summary (preserving FTS5 keyword retrieval), removal of superseded values (unless still active in another slot), and explicit retraction markers for retracted slots.
   - Any timeout, validation failure, or index update failure rolls back SQLite, FTS5, semantic index, P2 access history, and shared `RepairBudget`.
3. **Fixed Answer Reader & Event-Time Scoring (`src/verify_repair/llm/reader.py`, `src/verify_repair/answer_scoring.py`):**
   - Every policy uses the **exact same frozen `AnswerReader`** and prompt, receiving only a policy-neutral `EvidencePacket` (fresh summary, repaired summary, verified current evidence, stale summary on `NO_OP`, or empty evidence on retrieval miss).
   - For the explicit forms `What is the current ATTRIBUTE of ENTITY?`, `What is ATTRIBUTE of ENTITY?`, and `What is ENTITY's ATTRIBUTE?`, a deterministic support check abstains if the supplied sources lack that `(entity, attribute)` identity. These identities come from supplied versions only, without consulting event-time truth, expected answer IDs/values, or source freshness. Other question forms retain the normal evidence-only model behavior. The model still runs and its usage is charged; `reader_llm_metadata.scope_guard` preserves any overridden model answer and citations.
   - `score_run_answers()` evaluates generated answers against event-time ledger truth: normalized Exact Match, Token F1, abstention accuracy / false abstentions / missed abstentions (handling retractions and retrieval misses separately), and **outdated-fact errors** (kept strictly separate from substrate `used_stale_source`).

## Google Colab (simplest first run)

[`notebooks/step5_colab.ipynb`](../../notebooks/step5_colab.ipynb) runs the
offline suite, the live 4-bit integration test, the Step 5.6 canonical check,
the toy policy runs, and the CLI smoke command on a free Colab GPU, then
downloads `step5_colab_out.zip` (runtime manifest, scored answers, logs, and
environment details). Open it at
<https://colab.research.google.com/github/shohinirheasarkar/ShouldAgentsVerifyOrRepair/blob/step5-6-llm-costs/notebooks/step5_colab.ipynb>,
choose **Runtime → Change runtime type → T4 GPU**, then **Run all**. The
per-call timeout is raised to 180 s for slower T4 GPUs.

## Zaratan setup and runnable commands

Log in with `ssh <UMD directory ID>@login.zaratan.umd.edu` (UMD password and
Duo). Jobs need membership in the `cmsc848n-class` allocation.

Keep the repository, virtual environment, pip cache, and HuggingFace model cache under `/scratch/zt1/project/cmsc848n/user/$USER/` (avoid filling Zaratan's small home-directory quota).

### 1. One-time login-node setup (with internet access)

```bash
export SCRATCH_BASE="/scratch/zt1/project/cmsc848n/user/$USER"
mkdir -p "$SCRATCH_BASE"
cd "$SCRATCH_BASE"
git clone -b step5-6-llm-costs https://github.com/shohinirheasarkar/ShouldAgentsVerifyOrRepair.git
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

## Live run results (Zaratan, November 24, 2026)

Job 24252063, code `d45e908` (branch `step5-6-llm-costs`), one NVIDIA H100
80 GB, Python 3.11.13, torch 2.15.0+cu132, transformers 5.24.1, bitsandbytes
0.50.2. `Qwen/Qwen2.5-7B-Instruct` revision
`a09a35458c702b33eeacc393d103063234e8bc28`, NF4 4-bit with bfloat16 compute,
greedy decoding, `llm-prompts-v4` (bundle SHA-256 `28cc06b7…`). Outputs and the
job log are in [`zaratan_24252063/`](zaratan_24252063/).

| Check | Result |
| --- | --- |
| Core offline suite on the GPU node | 712 passed, 2 deselected (Python 3.11) |
| Live 4-bit integration test | passed |
| **5.6** canonical Maryland → Boston | passed for all four policies (below) |
| Toy trace, four policies | completed; scored answers saved |
| **5.1** standalone CLI toy command | completed |

Canonical check (`canonical_maryland_boston.json`):

| Policy | VERIFY | REPAIR | Stored summary after the run | Exact match | Outdated-fact errors |
| --- | --- | --- | --- | --- | --- |
| Verify-Only | 3 | 0 | stale, "…living in Maryland." | 1.00 | 0 |
| Eager Refresh | 0 | 2 | fresh, "…living in Boston." | 1.00 | 0 |
| Repair-on-First-Access | 0 | 1 | fresh, "…living in Boston." | 1.00 | 0 |
| Periodic-Refresh-Stale | 0 | 0 | stale, "…living in Maryland." | 0.00 | 3 |

Per-call cost on the H100 (toy logs): reader about 0.41 s (~300 tokens),
VERIFY about 0.53 s (~400 tokens), REPAIR 0.7–1.6 s (500–700 tokens), and
3.9 s when a validation failure forces a second attempt. Loading the model
from Zaratan's network file system takes about 6 minutes per process.

**Historical v4 issue (toy trace).** Exact match on the full toy trace is 0.60 for
Verify-Only, Eager Refresh, and Repair-on-First-Access. Both wrong answers are
"What is the current office of Alice?": keyword retrieval returns Alice's
*profile* summary instead of her office summary, and the v4 reader then answers
with her location ("Boston") instead of abstaining. The v4 prompt removed the
v3 rule "abstain if the evidence only describes a different attribute" (its
test calls this preserving the verified/stale answering contract); this case
shows the cost of that change. It is a retrieval miss plus a reader choice,
not a maintenance-policy effect.

**V5 live regression (November 27, job 24299576).** The prompt-only fix
restored the broad different-attribute abstention instruction. All 854 core
tests passed, and live canonical VERIFY/REPAIR checks passed, but the reader
abstained on all three stale Maryland location answers. The canonical check
correctly failed (`outdated_fact_error_count` was 0 instead of 3), stopping
the job before the full toy artifacts or Step 6. The office checks placed
after the canonical check were never reached. The v3-to-v4 commit `d522667`
had already removed this instruction to address stale-summary abstention.

**V6 correction (focused GPU check reported passed).** Restore the exact successful v4
model-facing reader prompt (SHA-256 `c1fa2d38ef922bc75fe6187b97cddb0a646df96d425684243818025294c855a5`)
and enforce explicit attribute support using source identities as described
above. Offline regressions inject an incorrect Boston office answer and
check that it is blocked without hiding its raw answer or cost; they also
retain Maryland answers regardless of packet freshness. The canonical
assertions remain unchanged.

Submit the focused check first from the updated repository:

```bash
OUT_DIR="/scratch/zt1/project/cmsc848n/user/$USER/reader-v6-$(date +%Y%m%d-%H%M%S)" \
sbatch scripts/zaratan_reader_check.sbatch
```

It runs the focused offline suite and one live model load, checking office
versus location, the exact current-location/Maryland packet, and the four
canonical policies. `requests_responses.jsonl` saves every model request
and raw response before assertions; `runtime.json` records model/runtime
settings, and a successful run saves `canonical.json`. `LLM_TEST_OUTPUT_DIR`
sets this diagnostic directory for direct pytest runs and is also set by
`scripts/zaratan_step5.sbatch`. The diagnostic harness is itself tested with
offline success and failure responses. The focused check was reported as passed, and
the subsequent v6 1K pilot completed in Zaratan job 24312071. Its toy answer
scores are in the transferred
[`pilot_manifest.json`](../step6/zaratan_24312071/pilot_manifest.json): exact
match remains 0.60 for Verify-Only, Eager Refresh, and
Repair-on-First-Access, but false abstentions and outdated-fact errors are
both zero. The office retrieval miss still prevents an exact answer.

Abstention prevents the unsupported Boston office answer but does not
recover missing office evidence or guarantee higher exact match. Historical
v4 measurements above remain unchanged.
