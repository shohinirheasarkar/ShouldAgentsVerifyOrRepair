# Can Agents Learn When to Verify or Repair Memory Under Budget Constraints and Access Uncertainty?

CMSC 848N · Fall 2026 · [GitHub repository](https://github.com/shohinirheasarkar/ShouldAgentsVerifyOrRepair)

## Project intro
Long-running agents may retain summaries built from facts that later change. When a query touches a summary flagged as stale, the agent can **verify** the current source for this answer or **repair** the stored summary for later use. We aim to compare decision policies under limited cost and uncertain future reuse. The proposal suggests a controlled event stream, simple fixed memory components, and comparisons to verify-only, eager repair, first-access repair, a threshold policy, and a hindsight oracle. **Access uncertainty** means uncertainty about how frequently future queries will access a specific fact or memory component. A shared budget still needs a precise definition. The synthetic generator controls future question counts; P2 now estimates future reuse with a simple moving average of past accesses.

## How to navigate this repo

| Location | Purpose | Status |
| --- | --- | --- |
| [README.md](README.md) | Project overview and contribution guide | Current |
| [docs/proposal.md](docs/proposal.md) | Proposal requirements| Draft checklist |
| [docs/timeline.md](docs/timeline.md) | Progress against the proposal timeline and next steps | Current |
| [docs/midterm.md](docs/midterm.md) | Midterm requirements| Pending |
| [docs/artifact-audit.md](docs/artifact-audit.md) | Audit rubric and reproducibility evidence | Pending |
| [docs/final-presentation.md](docs/final-presentation.md) | Presentation rubric and slides | Pending |
| [docs/final-report.md](docs/final-report.md) | Final paper checklist | Pending |
| [docs/related-works.md](docs/related-works.md) | Papers to verify and annotated notes | In progress |
| [docs/research-grade-memory-handoff.md](docs/research-grade-memory-handoff.md) | Roadmap from the deterministic MVP to a research-grade memory experiment | Active handoff |


## How to obtain, set up, and use the code

The shared substrate is the accepted deterministic pre-LLM phase in
[`IMPLEMENTATION_PLAN_PRE_LLM.md`](IMPLEMENTATION_PLAN_PRE_LLM.md), extended
with the requested simple baselines, Step 3 synthetic generator, Step 4 P1
threshold policy, and Step 5 P2 prediction-assisted threshold. It uses
SQLite for immutable fact versions and derived summaries, FTS5 for keyword
retrieval, and YAML for the inspectable toy trace. Timestamps at public APIs
are timezone-aware and normalized to UTC ISO 8601 in SQLite. Retractions are
immutable tombstones. A bounded exception overrides the latest applicable
permanent fact while active; a later permanent event takes precedence, and
expiry exposes the underlying permanent value again. Expiry is a simulator
transition that can mark summaries stale and trigger policy maintenance.

From the repository root, use Python 3.11 or newer in a virtual environment.
Install and run the CLI with that same interpreter:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[dev]'
pytest -q
python -m verify_repair.cli --help
python -m verify_repair.cli run-toy --policy verify-only
python -m verify_repair.cli run-toy --policy eager-refresh
python -m verify_repair.cli run-toy --policy repair-on-first-access
python -m verify_repair.cli run-toy --policy p1
```

An editable install (`python -m pip install -e '.[dev]'`) also works on normal
Python setups. On this macOS workspace, installed `.pth` files can receive a
hidden file flag and Python skips them, so the regular install above is more
reliable. After changing package code, rerun `python -m pip install '.[dev]'`.
The toy demo reads `configs/` and `data/` from this checkout, so run it from
the repository root or a subdirectory.

Pass `--verbose` to print every transition or `--jsonl PATH` to save stable
structured records. The configuration is in `configs/toy.yaml`; data are in
`data/toy/`. Costs are **nominal units for accounting tests**, not measured
latency, model expense, or research findings. A run records its seed, dataset
ID, policy, and config hash. Same trace and config yield identical logs; the
latency field is null in deterministic runs.

The toy data contain 20 logical facts, 10 summaries, and correction, update,
retraction, and temporary exception events. Alice's location begins as
Maryland, changes to Boston, and is queried three times. VerifyOnly returns
Boston as fresh evidence each time while the stored summary remains Maryland
and stale. EagerRefresh repairs the summary at correction time and the later
queries need no maintenance. `RepairOnFirstAccess` leaves an affected summary 
stale until it is first retrieved by a later query, then repairs it persistently; 
if the summary is never queried, no repair cost is paid. All toy data are 
development/demo data; there is no held-out benchmark in this phase.

Optional dense retrieval uses BGE-M3 with FAISS:

```bash
python -m pip install -e '.[semantic]'
python -m verify_repair.cli run-toy --policy eager-refresh --retrieval hybrid
RUN_SEMANTIC=1 pytest -o addopts='' -m semantic
```

The optional adapter loads a model only when selected. Core `pytest -q`
excludes that marked integration test and needs no model download. Semantic
vectors are reconstructed from SQLite summaries for a run; if vector update
fails during REPAIR, the SQLite transaction rolls back and the vector index is
rebuilt from committed summary text. The adapter initializes BGE-M3 before
importing FAISS: the reverse native-library import order caused a segmentation
fault on the tested macOS/Python 3.13 environment.

## Oldest-First maintenance

Oldest-First is a limited-budget comparison baseline. It verifies stale query
accesses and repairs at scheduled ticks, with a default interval of 100 processed
events and one repair slot per tick. Both settings must be positive. Candidates
are ranked by `(stale_since, summary_id)` ascending. `stale_since` tracks the
beginning of the uninterrupted stale period, even across further corrections.

```bash
python -m verify_repair.cli run-toy --policy oldest-first --refresh-interval-events 100 --repair-slots-per-tick 1
```

## Synthetic workloads (Step 3)

The generator creates people, projects, and places, with **one memory = one
summary over three initial facts**. Every memory receives one correction to a
source fact. All corrections precede all questions, creating one controlled
stale episode per memory; questions are then interleaved using a local seeded
random generator. IDs and UTC timestamps are deterministic.

| Group | Future questions per memory, inclusive |
| --- | --- |
| Cold | 0–2 |
| Warm | 3–10 |
| Hot | 11–50 |

The default population is balanced, with any remainder assigned to cold then
warm: 100 memories yield 34 cold, 33 warm, and 33 hot. Counts are sampled
uniformly within each group's range. Use fresh output filenames: generation
refuses to overwrite an existing artifact. No additional dependencies are
needed; reinstall the package after editing code as described above.

Start with 100 memories, replay them, then increase to 1,000 and 10,000:

```bash
python -m verify_repair.cli generate-synthetic --memories 100 --seed 12345 --output /tmp/workload-100.json
python -m verify_repair.cli run-synthetic --workload /tmp/workload-100.json --policy verify-only
python -m verify_repair.cli run-synthetic --workload /tmp/workload-100.json --policy repair-on-first-access

python -m verify_repair.cli generate-synthetic --memories 1000 --seed 12345 --output /tmp/workload-1000.json
python -m verify_repair.cli run-synthetic --workload /tmp/workload-1000.json --policy repair-on-first-access

python -m verify_repair.cli generate-synthetic --memories 10000 --seed 12345 --output /tmp/workload-10000.json
python -m verify_repair.cli run-synthetic --workload /tmp/workload-10000.json --policy repair-on-first-access
```

Choose group sizes and query ranges explicitly when needed. For exact counts,
set equal endpoints; the following creates 50 never-accessed cold memories,
30 warm memories with seven questions each, and 20 hot memories with 50 each:

```bash
python -m verify_repair.cli generate-synthetic --memories 100 --seed 12345 --group-counts 50 30 20 --cold-queries 0 0 --warm-queries 7 7 --hot-queries 50 50 --output /tmp/workload-exact.json
```

Group sizes must be nonnegative and sum to `--memories`; query ranges must
stay within the bands above. The versioned JSON artifact contains the resolved
configuration, initial facts, summary templates/source slots, correction/query
events, and evaluator-only `demands` (group labels and exact future counts).
The generator prints the artifact's SHA-256. Repeating the same seed and
configuration produces byte-identical artifacts. Saving and loading validate
group/access counts, source references, unique IDs, and event ordering. Replay
loads the saved inputs into fresh in-memory SQLite, builds summaries with exact
v1 provenance, and uses the existing simulator; the saved workload is never
changed by repair.

Synthetic replay uses keyword retrieval and questions containing a unique,
stable entity name, so each question reaches its intended memory before and
after repair. This isolates maintenance decisions from retrieval uncertainty.
All seven policies, including P1 and P2, can replay the same artifact. Policies
receive neither demand labels nor future counts. `--jsonl PATH` saves the existing
structured logs; `--refresh-interval-events` and `--repair-slots-per-tick`
retain their existing meanings. Hybrid retrieval remains available in the toy
demo.

These are controlled development workloads with independent memories and one
correction each. They are not a held-out benchmark or natural-language accuracy
evaluation. Costs remain nominal. Demand prediction, repeated corrections,
and dependency fan-out are outside this Step 3 change.

## P1 threshold maintenance (Step 4)

P1 (`--policy p1`, Python class `AccumulatedThreshold`) verifies a stale summary
until the next verification would bring episode spending to at least one repair.
It then repairs **instead of verifying**. With verification cost 1 and repair
cost 5, questions 1–4 verify, question 5 repairs, and later questions use the
fresh summary without maintenance. Each summary has its own counter. Another
relevant correction or effective exception expiry resets the counter; successful
repair resets it and clears staleness. Unqueried stale memories are not repaired.

For fixed positive costs, the repair question is `ceil(repair / verify)`:

| Verification cost | Repair cost | Repair question |
| --- | --- | --- |
| 1 | 5 | 5 |
| 2 | 5 | 3 |
| 0.1 | 1 | 10 |
| 5 | 1 | 1 |

Zero-cost repair happens on the first stale access (also when both costs are
zero). Free verification with positive repair cost continues indefinitely.
Negative, infinite, and NaN costs are rejected. P1 uses exact arithmetic on the
decimal representations of the configured fixed costs, so binary floating-point
accumulation cannot delay the repair. Existing nominal cost logs remain floats.
Costs varying within a stale episode are outside this step.

Both replay commands accept `--verify-cost` and `--repair-cost`; omitted values
retain the toy configuration or synthetic defaults (1 and 5). Resolved costs
appear in the existing config snapshot/hash. Workload JSON remains unchanged.

```bash
python -m verify_repair.cli run-toy --policy p1 --verify-cost 0.1 --repair-cost 0.3 --verbose
python -m verify_repair.cli run-synthetic --workload /tmp/workload-100.json --policy p1 --verify-cost 2 --repair-cost 5
python -m pytest -q tests/test_threshold.py tests/test_synthetic.py tests/test_cli.py
```

The engine passes an immutable `StaleAccessContext` as the `context` keyword to
`on_stale_access(summary_id, query, *, context=None)`. Custom policies must accept
this keyword. Existing baseline calls with two arguments still work; P1 requires
the context. It contains the stored successful-verification count and fixed run
costs, plus an optional past-only `demand_prediction` for P2 (see below). It
contains no future events, group labels, or actual future-demand counts. SQLite's
existing `queries_this_episode` field counts successful VERIFY operations, not all queries;
no duplicate policy-owned state or schema migration is needed.

The threshold tests establish deterministic switching and accounting with nominal
costs; they do not establish research performance or held-out accuracy. Oracle,
shared budgets, learned demand, and generative LLM work remain future work.

## P2 prediction-assisted threshold (Step 5)

P2 (`--policy p2`, Python class `PredictedThreshold`) uses a time-decayed moving
average to estimate additional accesses. It repairs early when a sufficiently
recent history predicts that the current query plus those additional accesses
would cost at least one repair. Otherwise it follows P1 exactly. With nominal
costs 1/5, cold or uncertain memories wait until access 5; a recently popular
memory can repair on access 1. Starting without history, closely spaced accesses
build enough evidence to repair on access 4 under the defaults. An unqueried
memory is never repaired. Free-cost behavior is identical to P1.

The predictor uses only **completed retrieved-summary accesses before the current
query**, including accesses while fresh. For each summary, SQLite stores the
lifetime access count, last-access timestamp, decayed count, and its half-life in
`summary_access_history`. At event time, it decays the previous count by
`2 ** (-elapsed_seconds / half_life_seconds)` and adds one after a successful
access. The estimated additional accesses are
`decayed_count * ln(2) * horizon_seconds / half_life_seconds`.
This gives more weight to recent use and decays through periods without queries.

| Setting | CLI option | Default |
| --- | --- | --- |
| Access half-life | `--p2-half-life-seconds` | 3,600 seconds (1 hour) |
| Rolling forecast horizon | `--p2-horizon-seconds` | 86,400 seconds (1 day) |
| Minimum completed accesses | `--p2-min-accesses` | 3 |

An estimate is considered confident only when the minimum access count has
been observed, the last access is at most one half-life old, and the decayed
count is at least half the configured minimum. These are simple heuristic gates,
not calibrated probabilities. An unrepresentable forecast also falls back to P1.
Durations must be finite and positive with a finite horizon/half-life ratio; the
minimum count must be a positive integer. P2 options on other policies are rejected.

```bash
python -m verify_repair.cli run-toy --policy p2 --verbose
python -m verify_repair.cli run-synthetic --workload /tmp/workload-100.json --policy p2 --jsonl /tmp/p2.jsonl
python -m verify_repair.cli run-synthetic --workload /tmp/workload-100.json --policy p2 --p2-half-life-seconds 3600 --p2-horizon-seconds 86400 --p2-min-accesses 3 --verify-cost 1 --repair-cost 5
python -m pytest -q tests/test_prediction.py tests/test_threshold.py tests/test_synthetic.py tests/test_cli.py
```

The engine enables history tracking for P2 and supplies an immutable
`DemandPrediction(expected_accesses, confident)` through
`StaleAccessContext.demand_prediction`; other policies receive `None`. Policy
instances contain only frozen `PredictionConfig` settings. Corrections and
exception expiry reset P1 episode spending but retain access history, as does
repair. Failed maintenance rolls back the access observation along with the
summary transition. The new table is created automatically when opening an old
database. Persistent history cannot be reused with a different half-life;
use a fresh replay database for a different setting. CLI replay already does so.

All resolved prediction settings appear under `config_snapshot.prediction` and
participate in P2's config hash and run ID. Top-level log keys and existing
policies' hashes remain unchanged. Saved workload JSON is unchanged. No new
packages or generative LLM calls are required.

The forecast horizon is a fixed rolling window, never the remaining trace length
or a shared-budget horizon. No future labels or counts reach the policy. Low
predictions wait longer **than high predictions**, never beyond P1's threshold.
The fallback caps delay, but an incorrect high estimate can still cause an
unnecessary early repair. The defaults and controlled hot/cold tests are development choices, not held-out
accuracy, calibrated uncertainty, measured costs, or research conclusions.

## Oracle, regret, and experiments

`run-experiment` replays saved synthetic workloads under every selected
policy and repair cost, then scores each replay against the **hindsight
oracle**. Each synthetic memory has one stale episode, so the oracle cost for a
memory with `k` future queries is `min(k * c_v, c_r)`. Regret is
`C(policy) - C(oracle)`; the CSV also reports `C(policy) / C(oracle)`. Rows
are written for all memories and for the cold, warm, and hot groups. The
evaluator reads the workload's demands only after a replay finishes; policies
never receive them.

`--measure-latency` records wall-clock milliseconds per query, from retrieval
through query-time VERIFY/REPAIR. It excludes correction-time and scheduled
repairs. Timing is off by default because it is the only non-deterministic
field; it measures the deterministic substrate, not an LLM. The CSV reports
p50/p95/p99 latency and p95 latency separately for queries with and without
maintenance (nearest-rank percentiles).

`summarize-experiment` averages result CSVs across seeds with 95% Student-t
confidence intervals. Both commands refuse to overwrite an existing file.

```bash
for seed in 1 2 3 4 5; do
  python -m verify_repair.cli generate-synthetic --memories 10000 --seed $seed --output /tmp/w10k-$seed.json
done
python -m verify_repair.cli run-experiment --workload /tmp/w10k-*.json \
  --verify-cost 1 --repair-costs 2 5 10 --measure-latency --output /tmp/results.csv
python -m verify_repair.cli summarize-experiment --input /tmp/results.csv --output /tmp/summary.csv
python -m pip install '.[analysis]'
python scripts/plot_experiment.py --summary /tmp/summary.csv --output-dir /tmp/figures
```

Scheduled policies use `--refresh-interval-events` (default 100) and
`--repair-slots-per-tick` (default 1). A 10,000-memory replay takes roughly
45 seconds on one laptop core; separate seeds can run as parallel processes
and be summarized together. The oracle formula assumes the synthetic-v1
contract (one correction per memory before its queries); repeated corrections
and a budget-aware oracle are future work.

## Tutorial notebooks
- to be added -

## Standards for each code file
When adding a code file, put a short header or docstring describing its purpose, inputs, outputs, and how to run it. Explain non-obvious variables and policy choices; use clear names, fixed seeds for randomized experiments, and explicit units for costs and latency. Put the exact run command and dependencies in this README. Describe which data were used for development and which were held out. Ask another teammate to review changes in a pull request.


## Links to major documents and presentations
| Deliverable | Planning page | Finished artifact |
| --- | --- | --- |
| Proposal | [Checklist](docs/proposal.md) | Link pending |
| Midterm report | [Checklist](docs/midterm.md) | Link pending |
| Artifact audit | [Checklist](docs/artifact-audit.md) | Link pending |
| Final presentation | [Checklist](docs/final-presentation.md) | Link pending |
| Final paper-style report | [Checklist](docs/final-report.md) | Link pending |

## Project planning 
https://github.com/users/shohinirheasarkar/projects/3/views/1

## Team
- Shohini Rhea Sarkar — [@shohinirheasarkar](https://github.com/shohinirheasarkar)
- Amelia Harn — [@aharn3](https://github.com/aharn3)
- Arik Gershman — [@arikgershman](https://github.com/arikgershman)
