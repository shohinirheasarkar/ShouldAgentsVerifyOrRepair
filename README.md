# Can Agents Learn When to Verify or Repair Memory Under Budget Constraints and Access Uncertainty?

CMSC 848N · Fall 2026 · [GitHub repository](https://github.com/shohinirheasarkar/ShouldAgentsVerifyOrRepair)

## Project intro
Long-running agents may retain summaries built from facts that later change. When a query touches a summary flagged as stale, the agent can **verify** the current source for this answer or **repair** the stored summary for later use. We aim to compare decision policies under limited cost and uncertain future reuse. The proposal suggests a controlled event stream, simple fixed memory components, and comparisons to verify-only, eager repair, first-access repair, a threshold policy, and a hindsight oracle. **Access uncertainty** means uncertainty about how frequently future queries will access a specific fact or memory component. A shared budget still needs a precise definition. This pre-LLM implementation does not model future query frequency.

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


## How to obtain, set up, and use the code

The active implementation is the deterministic pre-LLM phase in
[`IMPLEMENTATION_PLAN_PRE_LLM.md`](IMPLEMENTATION_PLAN_PRE_LLM.md). It uses
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
queries need no maintenance. All toy data are development/demo data; there is
no held-out benchmark in this phase.

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
