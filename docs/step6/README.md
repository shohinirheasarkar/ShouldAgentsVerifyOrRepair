# Step 6: measured LLM costs, cost replay, and dirty bits

Step 6 measures what VERIFY and REPAIR actually cost with the Step 5 local
LLM, turns those measurements into reusable cost profiles, replays them at
scale without calling the model, and measures how often a stale flag is a
false alarm. Everything is opt-in: without `--cost-profile` or latency
measurement, runs, logs, and hashes are unchanged.

## Corrected v6 pilot (Zaratan, November 28, 2026)

Job 24312071 completed with `llm-prompts-v6` on one H100 80 GB, Python
3.11.13, the pinned Qwen2.5-7B-Instruct model, NF4 4-bit, and pilot seed
2001. The scratch output is
`/scratch/zt1/project/cmsc848n/user/arikger/step6-v6-20261128-001639/pilot`.
The archived log and manifest show four toy runs, two 3,000-query cost
runs, two 1,000-query quality runs, and all three profiles completed without
a failed stage. The focused v6 live reader check was separately reported as
passed. These are development measurements from one seed.

| v6 metric | VERIFY | REPAIR |
| --- | ---: | ---: |
| Cost-run calls | 883 | 310 |
| Pooled profile samples (cost + quality) | 998 | 373 |
| Pooled wall-time mean | 885.9 ms | 3,321.5 ms |
| Pooled GPU-time mean | 884.5 ms | 3,319.1 ms |
| Pooled total-token mean | 624.0 | 1,087.5 |

The wall-time repair/verify ratio is 3.75. Cost-run VERIFY had 11 fallbacks
(1.25%) and 32 retried operations (3.62%), compared with 451 fallbacks
(51.1%) in v4. Its operation-time p50/p95/p99 were 820/1,047/3,052 ms;
the high tail includes retries. The first five fallback examples put an old
source ref in `retracted_slots` where a slot name was required. Those examples
do not establish the cause of every fallback; REPAIR counts are below.

The new profile files are archived in [`zaratan_24312071/`](zaratan_24312071/)
along with the manifest, dirty-bit sample, job log, base commit, and source
patch. Their SHA-256
hashes are: wall-ms `06f9553411394762873c8bd39c07c6b35adcec1c22f67d340a5ac5cd9f76f738`,
GPU-ms `2f8c88aa3ad9e4e9744a822bccbc758b313b67fdd20af4967b9a1c78fce95d8c`,
and total-tokens `2dc3f514f4d493a66e4e889d59d79c5bee8718f48e37871f0bb5899fde4471fe`.
The profile at `docs/step6/cost_profile_wall_ms.json` remains **v4**; use
[`zaratan_24312071/cost_profile_wall_ms.json`](zaratan_24312071/cost_profile_wall_ms.json)
for new measured-cost comparisons. Both profiles have the same `profile_id`
(`llm-wall_ms-pilot2001`), so identify them by SHA-256 and prompt version.

The transferred manifest resolves the answer-quality and fallback counts:

| v6 run | Exact match | False abstentions | Outdated-fact errors | Reader fallbacks |
| --- | ---: | ---: | ---: | ---: |
| Toy Verify-Only | 0.600 (5 queries) | 0 | 0 | 0 |
| Toy Eager Refresh | 0.600 (5 queries) | 0 | 0 | 0 |
| Toy Repair-on-First-Access | 0.600 (5 queries) | 0 | 0 | 0 |
| Toy Periodic-Refresh-Stale | 0.000 (5 queries) | 0 | 3 | 0 |
| Quality Verify-Only | 0.972 (1,000 queries) | 27 | 0 | 0 |
| Quality Repair-on-First-Access | 0.969 (1,000 queries) | 29 | 0 | 0 |

Cost-run REPAIR had 9 fallbacks and 17 retried operations among 310 calls;
quality-run REPAIR had 1 fallback and 2 retried operations among 63 calls.
Fallbacks use the deterministic builder after failed validation. Its
claim/source checks enforce supported content, but the saved manifest does
not constitute an independent human audit of REPAIR text. The toy reader
now abstains on unsupported office evidence; the retrieval miss remains, so
exact match stays 0.60.

The archived v6 wall-time profile also passed deterministic cost replay:
128 rows across all eight policies and four saved 100-memory traces, 20 rows
for five policies under a shared budget on the stable trace, and 16 rows for
four policies on a generated 10K stable development trace. All rows had
nonnegative regret. The 10K workload hash is
`f0f0d485a58f01c14ed4c7f1a82985b7ea036eb08ffc5409e92c816feef23e9c`.
These are development checks, not held-out comparisons; the 100K scale
experiment remains Step 11. Exact commands and validation totals are in
[`zaratan_24312071/replay_validation.md`](zaratan_24312071/replay_validation.md).

The v6 dirty-bit stage reported 8,734 stale accesses and 300 sampled cases:
structural precision 0.522, semantic precision 0.547, fresh-answer accuracy
0.987 (296/300), 14 flagged for review, and no reader fallbacks. Independent
teammate reviewer Arik Gershman reviewed and accepted all 14 query-time cases in
[`dirty_bit_review.md`](dirty_bit_review.md) on November 28 (job 24312071):
ten changed-back values and four false abstentions on “delta,” with no
unresolved cases. The four abstentions remain accuracy errors. Arik also
completed the independent review of the Step 5/6 changes and saved v6 evidence.
The optional
semantic-index profile skipped because `venv-semantic` was absent; the earlier
H100 semantic-index timings remain the relevant Step 6.3 measurements.

## Historical v4 results (Zaratan, November 24, 2026)

These are development pilots on a single pilot seed (2001). They measure
what the archived v4 pipeline costs; they are not research findings or costs
of the current corrected implementation.

- **Pilots and dirty bits:** job 24252220, code `6dc4bdf`, 1h21m
  ([`zaratan_24252220/`](zaratan_24252220/)).
- **Semantic-index profile:** the first submission, job 24252085
  ([`zaratan_24252085/`](zaratan_24252085/)), whose pilots stopped on the
  validation failure that motivated `llm_fallback`.

Both ran on one H100 80 GB with the Step 5 model and runtime:

- Qwen2.5-7B-Instruct at revision `a09a3545…`
- NF4 4-bit with bfloat16 compute, greedy decoding
- `llm-prompts-v4`, `--llm-max-retries 2`

The large logs, the 5 MB pilot workload, and the quality-run answers stay on
Zaratan scratch. The commit keeps the manifest, profiles, dirty-bit sample,
toy answers, and job log.

### Measured operation costs (1K pilot, cost runs, 3,000 queries each)

| Operation | n | p50 | p95 | p99 | mean | first call | retried | fell back |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| VERIFY (Verify-Only) | 883 | 2,453 ms | 2,686 ms | 3,178 ms | 1,759 ms | 2,665 ms | 464 | **451 (51%)** |
| REPAIR (Repair-on-First-Access) | 310 | 3,560 ms | 5,808 ms | 10,972 ms | 3,406 ms | 13,265 ms | 17 | 9 (3%) |

Where the time goes:

- **Generation is nearly all of it.** Source lookup takes about 0.15 ms, the
  SQLite/FTS5 store about 0.4 ms, and the semantic index 0 ms (keyword-only
  runs).
- **Tokens.** VERIFY averages 1,289 tokens per operation (1,181 prompt, 108
  completion). REPAIR averages 1,087 tokens (878 prompt, 209 completion).
- **Query-visible latency.** A query that needs no maintenance takes 0.2 ms.
  A query that triggers a VERIFY takes p50 2.45 s; one that triggers a REPAIR
  takes p50 3.56 s and p99 11.0 s.
- **Reader.** The fixed reader adds about 0.57 s per query (quality runs).
- **Model load.** Loading the model takes 250 s per process.

**Historical v4 issue: most LLM VERIFY calls fail validation.** 436 of the 451
fallbacks fail the same check: `verified_evidence missing current value … for
changed slot …`. The v4 VERIFY prompt asks for "concise, query-scoped"
evidence, so the model describes only the asked attribute. The validator
([`validation.py`](../../src/verify_repair/llm/validation.py),
`validate_verify_output`) requires the current value of *every* changed slot.
Single-fact summaries almost never fail; wider ones usually do. The VERIFY
wall time by fact count shows it:

| Facts per summary | 1 | 2 | 3 | 4 | 5 | 6 |
| --- | --- | --- | --- | --- | --- | --- |
| VERIFY median (ms) | 858 | 2,436 | 2,467 | 2,493 | 2,507 | 2,494 |
| REPAIR median (ms) | 1,126 | 1,968 | 2,799 | 3,649 | 4,461 | 5,258 |

What this means for the measured costs:

- **Successful VERIFY calls.** They take p50 866 ms (419 of 432 succeed on
  the first attempt).
- **Fallbacks.** A fallback spends three failed generations, p50 2,530 ms,
  before the deterministic VERIFY answers.
- **The profile mixes both.** The measured VERIFY cost is therefore about
  2.9× what a fixed prompt/validator pair would likely cost. The measured
  repair/verify ratio is 1.9; with successful calls only it would be about 3.8.

**Fix applied November 27, 2026:** validation now requires the queried slots,
including their exact current citations, values, and retraction handling,
rather than every changed slot. Explicit entity/attribute mentions determine
query scope; for paraphrased questions without literal attributes, the cited
facts are checked. Superseded-value leakage remains checked across all changes.
VERIFY stays non-mutating. The prompt-only reader-v5 fix regressed live
stale answering in job 24299576. Reader-v6 restores the successful v4 prompt
and adds a deterministic source-identity support check for explicit attribute
questions (see `docs/step5/README.md`). The v6 run above completed.

Offline regressions and the focused live check cover the fixes; the v6 pilot
above produced new wall-time, GPU-time, and token profiles. Keep the saved v4
profiles and replay results as historical development artifacts.

**Other failure modes:**

- **VERIFY:** 11 outputs listed an unknown slot in `retracted_slots`, and 4
  cited a superseded ref.
- **REPAIR:** 8 of the 9 fallbacks omitted the explicit retraction marker for a
  retracted slot.

### Answer quality (1K pilot, quality runs, first 1,000 queries, with reader)

| Policy | VERIFY | REPAIR | Exact match | False abstentions | Outdated-fact errors |
| --- | --- | --- | --- | --- | --- |
| Verify-Only | 115 | 0 | 0.972 | 27 | 0 |
| Repair-on-First-Access | 0 | 63 | 0.969 | 29 | 0 |

The deterministic fallback keeps answers correct when LLM VERIFY fails, so
these numbers do not measure LLM VERIFY quality on its own.

The toy pilot reproduces Step 5:

- exact match 0.60 for Verify-Only, Eager Refresh, and Repair-on-First-Access
  (the reader issue documented in `docs/step5/README.md`);
- exact match 0.00 for Periodic-Refresh-Stale, with 3 outdated-fact errors.

### Cost profiles

Each profile was built from all 1K-pilot operations: 998 VERIFY (cost and
quality runs) and 373 REPAIR. Every fact-count stratum has at least 52 samples.

| Profile | VERIFY mean | REPAIR mean | Ratio |
| --- | --- | --- | --- |
| [`cost_profile_wall_ms.json`](cost_profile_wall_ms.json) (`llm-wall_ms-pilot2001`) | 1,783 ms | 3,413 ms | 1.91 |
| `zaratan_24252220/cost_profile_gpu_ms.json` | 1,781 ms | 3,411 ms | 1.92 |
| `zaratan_24252220/cost_profile_total_tokens.json` | 1,311 tok | 1,088 tok | 0.83 |

GPU time and wall time agree within about 3 ms, so the GPU bounds the
operations. By tokens, a REPAIR is *cheaper* than a VERIFY: the VERIFY prompt
carries the stored summary, all facts, and the change list, and failed
attempts multiply it. Which unit counts as "cost" is a modeling choice for the
final experiments.

### 6.4 check: measured replay at 100 memories

The command in 6.4 (all eight policies, four 100-memory v2 traces, wall-ms
profile, λ = 2,000 ms) wrote 128 rows with no negative regret. Totals over
the whole trace, in seconds of LLM time:

| Policy | stable | bursty | shifting | fanout |
| --- | --- | --- | --- | --- |
| Verify-Only | 1,454 | 1,299 | 2,636 | 1,687 |
| Eager Refresh | 1,012 | 1,064 | 1,158 | 1,415 |
| Repair-on-First-Access | 494 | 310 | 679 | 693 |
| Periodic Refresh | 1,091 | 1,064 | 1,312 | 1,389 |
| Oldest-First | 1,463 | 1,185 | 2,545 | 1,663 |
| P1 | 645 | 446 | 878 | 795 |
| P2 | 614 | 437 | 795 | 772 |
| Periodic-Refresh-Stale (λ) | 1,113 | 1,082 | 1,360 | 1,388 |
| Oracle (no stale use) | 416 | 293 | 587 | 530 |

A **10K check** ran on a stable trace from `generate-evolving --memories 10000
--seed 2001 --facts-per-summary 1 6 --changes-per-memory 3` (126,599
queries, 32,793 episodes). It replayed Repair-on-First-Access, P1, P2, and
Verify-Only in 3 min 56 s on a laptop, with no negative regret. Cost versus
the oracle: 1.25×, 1.49×, 1.42×, and 3.18×.

This is a replay of measured costs on development traces. It demonstrates the
pipeline and is not an experimental result. With a measured ratio of 1.9,
P1 repairs on the second stale access, so Repair-on-First-Access is close to
P1's behavior; the ranking may change once the VERIFY issue above is fixed.

### Semantic dirty bits (6.5)

Verify-Only replay of the 1K pilot has 8,734 stale accesses; the seeded
sample has 300.

- **Structural flag precision (all accesses):** 0.522, 95% CI 0.511–0.532.
  About half of all stale flags are raised by a change to a fact the
  question does not ask about.
- **Semantic flag precision (sample):** 0.547, CI 0.490–0.602.
- **Fresh-summary answer accuracy (sample):** 0.987, CI 0.966–0.995.
- **Stale answer wrong, given:**
  - the asked fact changed: 0.943 (164 of 174);
  - it did not change: 0.000 (0 of 126).

The sample is identical, answer for answer, in both jobs (the reader is
deterministic). The 14 cases received owner sign-off; each is explained in
[`dirty_bit_review.md`](dirty_bit_review.md):

- 10 where the asked fact changed and then changed back to its old value, so
  the stale answer is right;
- 4 where the reader abstained on "delta" in the fresh summary, despite its
  being explicitly stated (the stale answers were different, outdated values).

Structural precision therefore matches semantic precision closely on this
workload; the remaining gap is the changed-back values.

### Semantic-index maintenance (6.3)

Each measurement is the median of 3 runs (BGE-M3 + FAISS on the H100):

| Indexed summaries | Full rebuild (current REPAIR) | Single-text embed | Search |
| --- | --- | --- | --- |
| 10 | 29 ms | 15.5 ms | 23 ms |
| 100 | 159 ms | 15.5 ms | 14 ms |
| 1,000 | 691 ms | 15.5 ms | 14 ms |

The full rebuild grows linearly, about 0.68 ms per indexed summary; an
incremental update would cost about 15.5 ms. **Decision: no incremental
index now.**

- The synthetic replays and final experiments use keyword retrieval, so
  semantic maintenance never enters their costs.
- Hybrid retrieval runs only on the toy trace, where a rebuild costs under
  30 ms beside a 1–5 s LLM repair.

An incremental update is needed before hybrid retrieval runs at 1,000+
memories with REPAIR, where a rebuild would add about 20% to each repair.

## 6.1 Instrumentation

`MaintenanceService.verify` and `.repair` report component wall-clock timings
in milliseconds (`timing_ms`):

| Component | VERIFY | REPAIR |
| --- | --- | --- |
| `source_lookup` | resolve current fact versions | resolve current fact versions |
| `generation` | LLM verifier call(s), including validation retries | LLM (or template) rebuild, including retries |
| `store_and_keyword_index` | — | write text and provenance, update FTS5 |
| `semantic_index` | — | BGE-M3 + FAISS update (0 when keyword-only) |
| `total` | whole operation | whole operation |

With `measure_latency=True` (`--measure-latency` in `run-experiment`) every log
row also carries `timing_ms` and `query_visible`:

- **Query rows** (`query_visible: true`): `retrieval`, the operation's
  components prefixed `verify_` / `repair_`, and `reader` (answer generation)
  on the query's last row. `latency_ms_instrumentation_only` is the whole
  query.
- **Background rows** (`query_visible: false`): correction-time, scheduled-tick,
  and deferred-queue repairs, which no user waits for.

LLM calls additionally log tokens and GPU time (CUDA events) in
`maintenance_llm_metadata.usage` and `reader_llm_metadata.usage` (Step 5).

## 6.2 Pilots

`scripts/run_step6_pilot.py` loads the model once and runs:

1. **Toy pilot:** the toy trace under Verify-Only, Eager Refresh,
   Repair-on-First-Access, and Periodic-Refresh-Stale with the fixed reader.
2. **1K pilot:** a 1,000-memory synthetic-v2 trace (1–6 facts per summary,
   3 changes per memory on average, stable demand, **pilot seed 2001**, never
   used for experiments). *Cost runs* replay the first 3,000 queries without
   the reader under Verify-Only (VERIFY samples) and Repair-on-First-Access
   (REPAIR samples). *Quality runs* replay the first 1,000 queries with the
   reader (answer quality and reader latency).
3. **Cost profiles** from all 1K pilot operations.
4. **Dirty bits** (6.5).

`pilot_manifest.json` records settings, model/runtime/quantization versions,
hardware, stage durations, per-operation distributions (p50/p95/p99 of every
timing component, tokens, GPU ms), first-call (cold) versus warm medians,
validation retries, query-visible latency with and without maintenance,
background repair time, answer-quality summaries, and any failures.

## 6.3 Semantic-index maintenance

`scripts/profile_semantic_index.py` times BGE-M3 + FAISS maintenance apart
from generation: a full rebuild (what one REPAIR costs today, since
`index_summary` re-embeds everything), a single-text embedding (the lower
bound for an incremental update), and one search, at N = 10, 100, 1,000.

Synthetic replays use keyword retrieval, so semantic rebuilds never enter the
synthetic cost profiles or the final controlled experiments. The profile
documents the cost for hybrid runs and decides whether an incremental update
is needed (see results).

## 6.4 Measured-cost replay

`cost-profile-v1` JSON (`src/verify_repair/costs.py`) stores measured samples
per operation, pooled and stratified by summary fact count (a stratum is used
when it has at least 5 samples). Profiles are built per metric:

| Metric | Meaning |
| --- | --- |
| `wall_ms` | operation wall-clock ms: lookup + generation + validation + store/index |
| `gpu_ms` | GPU ms of the operation's LLM calls (CUDA events) |
| `total_tokens` | prompt + completion tokens of the operation's LLM calls |

Replay (`--cost-profile PATH`, synthetic-v2 only):

- **Shared keyed draws.** Each VERIFY is charged the sample selected by
  hashing `(profile, seed, query event, summary)`; each REPAIR by
  `(profile, seed, summary, stale episode)`. Whatever triggers a repair
  (correction, query, tick, or queue), an episode's repair costs the same, so
  every policy and both oracles see identical costs.
- **What policies see.** Policies receive the profile's mean VERIFY and REPAIR
  costs as their fixed `c_v` / `c_r`, so P1 and P2 keep their fixed-cost
  behavior; realized future costs stay hidden.
- **Budget.** Reservations use the realized repair cost. The budget-aware
  oracle becomes a knapsack per window (total repair cost at most `B`), solved
  exactly by the same integer program; the matrix is no longer totally
  unimodular, so `budget_oracle_gap` is reported.
- **Oracle.** Per episode, `min(sum of its queries' VERIFY draws, its REPAIR
  draw)` (stale-use: `min(c_v, λ)` per query, 0 for expiry episodes).
- **Labels.** Rows carry `cost_profile_id`; the config snapshot records the
  profile hash. Units are the profile's metric (for example ms), so `λ` must
  be given in the same units. Replay results are cost replay, not
  LLM-in-the-loop measurements.

```bash
PYTHONPATH=src python -m verify_repair.cli run-experiment \
  --workload data/synthetic_v2/{stable,bursty,shifting,fanout}-100-seed1.json \
  --cost-profile docs/step6/zaratan_24312071/cost_profile_wall_ms.json --stale-penalty 2000 \
  --output measured.csv
```

## 6.5 Semantic dirty-bit check

`src/verify_repair/dirty_bit.py` (evaluator-only; nothing reaches a policy):

1. Replay the trace under Verify-Only with deterministic operations, so every
   stale access is a VERIFY row and stored text stays old.
2. **Structural precision** on every stale access: the flag is needed when the
   queried fact's version in the summary's provenance is no longer current.
3. **Semantic precision** on a seeded sample: the frozen reader answers each
   question from the stale stored summary and from a freshly rebuilt summary
   (deterministic template over current facts, so rebuild errors cannot leak
   in). The flag is needed when the stale-summary answer is wrong against
   event-time truth (a retracted fact is answered correctly only by
   abstaining).
4. Wilson 95% intervals; `dirty_bit_sample.csv` marks `needs_review` where the
   two answers disagree or the fresh answer is itself wrong.

## Reproduce

```bash
# Offline (no GPU): deterministic pipeline, wall-ms profile, structural dirty bits
python scripts/run_step6_pilot.py --mode deterministic --output-dir /tmp/step6-det

# Zaratan (after the Step 5 setup in docs/step5/README.md)
sbatch scripts/zaratan_step6.sbatch
```

After the focused v6 reader check passes, use the same cached model revision and pilot
settings, saving to a new scratch directory (the output directory must be
empty). This includes toy, cost, quality, and the 300-case dirty-bit sample:

```bash
OUT_DIR="/scratch/zt1/project/cmsc848n/user/$USER/step6-v6-$(date +%Y%m%d-%H%M%S)" \
LLM_REVISION=a09a35458c702b33eeacc393d103063234e8bc28 \
COST_QUERIES=3000 QUALITY_QUERIES=1000 DIRTY_SAMPLE=300 \
sbatch scripts/zaratan_step6.sbatch
```

Run the Step 5 live test first (`docs/step5/README.md`). Check the new
manifest's prompt version/hash, validation retry/fallback rates, answer
quality, and reviewed dirty-bit disagreements before replacing any profile
used for comparisons.
