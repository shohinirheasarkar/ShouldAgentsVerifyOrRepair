# Can Agents Learn When to Verify or Repair Memory Under Budget Constraints and Access Uncertainty?

CMSC 848N · Fall 2026 · [GitHub repository](https://github.com/shohinirheasarkar/ShouldAgentsVerifyOrRepair)

## Project intro
Long-running agents may retain summaries built from facts that later change. When a query touches a summary flagged as stale, the agent can **verify** the current source for this answer or **repair** the stored summary for later use. We aim to compare decision policies under limited cost and uncertain future reuse. The proposal suggests a controlled event stream, simple fixed memory components, and comparisons to verify-only, eager repair, first-access repair, a threshold policy, and a hindsight oracle. **Access uncertainty** means uncertainty about how frequently future queries will access a specific fact or memory component. A shared budget still needs a precise definition. The synthetic generator controls future question counts; predicting those counts remains future work.

## How to navigate this repo

| Location | Purpose | Status |
| --- | --- | --- |
| [README.md](README.md) | Project overview and contribution guide | Current |
| [docs/proposal.md](docs/proposal.md) | Proposal requirements| Draft checklist |
| [docs/midterm.md](docs/midterm.md) | Midterm requirements| Pending |
| [docs/artifact-audit.md](docs/artifact-audit.md) | Audit rubric and reproducibility evidence | Pending |
| [docs/final-presentation.md](docs/final-presentation.md) | Presentation rubric and slides | Pending |
| [docs/final-report.md](docs/final-report.md) | Final paper checklist | Pending |
| [docs/related-works.md](docs/related-works.md) | Papers to verify and annotated notes | In progress |
| [docs/research-grade-memory-handoff.md](docs/research-grade-memory-handoff.md) | Roadmap from the deterministic MVP to a research-grade memory experiment | Active handoff |
| [docs/synthetic-workload-plan.md](docs/synthetic-workload-plan.md) | Scoped Step 3 implementation and validation plan | Implemented |


## How to obtain, set up, and use the code

The shared substrate is the accepted deterministic pre-LLM phase in
[`IMPLEMENTATION_PLAN_PRE_LLM.md`](IMPLEMENTATION_PLAN_PRE_LLM.md), extended
with the requested simple baselines and Step 3 synthetic generator. It uses
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
All five existing policies can replay the same artifact. Policies receive
neither demand labels nor future counts. `--jsonl PATH` saves the existing
structured logs; `--refresh-interval-events` and `--repair-slots-per-tick`
retain their existing meanings. Hybrid retrieval remains available in the toy
demo.

These are controlled development workloads with independent memories and one
correction each. They are not a held-out benchmark or natural-language accuracy
evaluation. Costs remain nominal. P1, demand prediction, repeated corrections,
and dependency fan-out are outside this Step 3 change. The implementation and
acceptance plan is [docs/synthetic-workload-plan.md](docs/synthetic-workload-plan.md).

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
