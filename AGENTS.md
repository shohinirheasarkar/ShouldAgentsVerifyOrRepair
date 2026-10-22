# AGENTS.md

## Purpose

This repository implements the research project **“Can Agents Learn When to Verify or Repair Memory Under Budget Constraints and Access Uncertainty?”**

The core research problem is a verify-versus-repair decision for stale derived memories:

- **VERIFY**: consult current source facts for the current query only; the stored summary remains unchanged and stale.
- **REPAIR**: rebuild the stored summary from current source facts; update its provenance/retrieval representation and clear its stale state.

The source fact history is authoritative. Derived summaries are caches/derived state and may become stale.

## Read this first

1. Read `IMPLEMENTATION_PLAN_PRE_LLM.md` for the accepted substrate's invariants
   and completed milestones. Do not restart milestones 0–10.
2. Read `README.md` for current setup, CLI commands, and limitations.
3. Read the synthetic workload and P1/P2 sections of `README.md` for usage
   and behavior, and the contracts below for implementation constraints.
4. Read `docs/research-grade-memory-handoff.md` when planning future research.
   It is a roadmap, not authorization to implement all its phases. Its original
   current-state list and unchecked boxes predate the additional baselines and
   Step 3; check the implementation and tests before treating an item as missing.

The original plan's handoff and scope exclusions describe the original phase.
Later scoped extensions below are already implemented. Preserve the accepted
invariants and these extensions; do not revert them to match historical plan
details. Follow the current user's scope and preserve established repository
conventions.

## Current project state and scope

The project owner **approved the deterministic pre-LLM substrate**. It includes
the immutable temporal ledger, derived summaries, exact provenance, dependency
DAG, staleness propagation, deterministic VERIFY/REPAIR, replayable simulator,
nominal cost logs, toy YAML inputs, keyword retrieval, RRF/hybrid retrieval,
and optional BGE-M3 + FAISS retrieval.

Subsequent scoped work adds three comparison baselines and **Step 3:
deterministic synthetic workloads**, followed by **Step 4: P1 threshold
maintenance** and **Step 5: P2 prediction-assisted
threshold**. The current checkout implements seven
policies and generate/save/load/replay CLI commands. Controlled synthetic workload validation has covered
up to 10,000 memories. Inspect `git status` before editing: work
may be uncommitted. Implementation and passing tests do not themselves imply
owner acceptance, a commit, or a push.

The project owners define **access uncertainty** as uncertainty about how
frequently future queries access a specific fact or memory component. The
synthetic generator controls those future counts; P2 estimates future reuse from
past accesses with a simple moving average. Learned prediction remains future
work. Oldest-First has repair slots per tick, but a shared research budget and
its horizon are not yet formally defined or implemented.

Work beyond the implemented extensions needs a separately scoped request and
plan. In particular, do not start these merely because the roadmap lists them:

- P3 or trained demand predictors;
- hindsight oracle, regret analysis, or shared-budget optimization/scheduling;
- generative LLM calls, hosted APIs, LLM VERIFY/REPAIR/answer generation, or
  hosted-LLM synthetic text generation;
- semantic dirty-bit LLM evaluation or external benchmark integration;
- research experiments beyond the scoped controlled workload checks, including
  100K/1M scale runs.

The existing 100/1,000/10,000-memory Step 3 checks are controlled development
validation. They establish no held-out accuracy, measured model cost, or
research conclusion.

## Repository map

| Location | Responsibility |
| --- | --- |
| `src/verify_repair/db.py`, `models.py` | SQLite schema/transactions, domain types, UTC timestamp helpers |
| `src/verify_repair/memory/` | Immutable ledger, summary/provenance store, dependency DAG, staleness |
| `src/verify_repair/maintenance/` | Deterministic template builder and VERIFY/REPAIR execution |
| `src/verify_repair/policies/` | Storage-free decisions and scheduled maintenance hooks |
| `src/verify_repair/retrieval/` | FTS5 keyword retrieval, RRF/hybrid, optional semantic adapter |
| `src/verify_repair/simulator/` | Typed events, engine/logging/accounting, toy loader, synthetic workloads |
| `src/verify_repair/cli.py`, `config.py` | CLI dispatch and toy configuration loading |
| `tests/` | Flat test layout; use the existing files rather than the original plan's suggested subdirectories |
| `data/toy/`, `configs/toy.yaml` | Inspectable toy facts/summaries/events and nominal costs |

CLI replay uses fresh in-memory SQLite; the database disappears at the end of
the run. Saved workload JSON is input data, not a persisted simulator database.

## Policy behavior to preserve

| CLI policy | Stale query access | Correction / scheduled repair |
| --- | --- | --- |
| `verify-only` | VERIFY on every stale access | No repair |
| `eager-refresh` | Normally already fresh | Repair all affected summaries immediately after a change |
| `repair-on-first-access` | REPAIR once, then use the persistent fresh summary | No repair until queried |
| `periodic-refresh` | VERIFY until repaired | Repair all stale summaries at each tick, ordered by summary ID |
| `oldest-first` | VERIFY until repaired | Repair up to the slot limit, ordered by `(stale_since, summary_id)` ascending |
| `p1` | REPAIR when the next VERIFY would meet/exceed one repair's cost; otherwise VERIFY | No background repair; reset count on relevant changes or repair |
| `p2` | REPAIR early for a confident high forecast; otherwise follow P1 exactly | No background repair; preserve popularity history across correction/repair |

Scheduled policies default to one tick per **100 processed events**;
Oldest-First defaults to **one repair slot per tick**. Both settings must be
positive. Ticks run after the triggering event has been processed. Preserve
the engine's effective-expiry handling and deterministic event order.
Policies return actions; the engine validates and executes them. Keep scheduled
action validation before any repair so invalid batches cannot partly execute.

P1 uses fixed nominal run costs and the persisted successful-verification count
in `queries_this_episode`. The engine supplies an immutable `StaleAccessContext`
through the stale-access hook's `context` keyword. Policies receive no future
demand. For positive costs, repair on access `ceil(repair / verify)`, computed
using exact decimal-value ratios. Free repair wins immediately; free verification
with positive repair cost never switches. Both replay commands support
`--verify-cost` / `--repair-cost` and log the resolved costs. Variable costs within
an episode remain outside scope.

P2 stores per-summary access history separately from episode state in
`summary_access_history`. Only P2 enables tracking; count every successful
retrieved-summary access, including fresh ones, exactly once. Predict from
completed accesses before the current query using event time and exponential
decay. Defaults are half-life 3,600 seconds, rolling horizon 86,400 seconds, and
minimum three observations. Confidence also requires a last access within one
half-life and decayed count at least half the minimum. These are heuristic gates.
P1 remains a hard switching deadline, including its exact decimal and free-cost
rules. The optional immutable `demand_prediction` in `StaleAccessContext` contains
an estimate, never actual future demand. Failed maintenance must roll back the
observation, including recovery of semantic indexes after outer rollback. Log
all P2 settings in the config snapshot/hash; reject a different half-life when
reusing persisted history. See the P2 section of `README.md` and
`tests/test_prediction.py` for predictor details and regression coverage.

## Non-negotiable domain invariants

1. **Fact history is immutable.** Existing fact-version rows must never be updated or deleted. Corrections/updates/retractions are new immutable rows/events.
2. **History is queryable.** The system must resolve current and historical values.
3. **Provenance is exact.** Every derived summary records the exact fact versions used to build it.
4. **Staleness is dependency-driven.** A fact change marks direct and transitive dependent summaries stale.
5. **VERIFY is non-mutating with respect to summary content/provenance.** It may update counters/logs only.
6. **REPAIR is persistent.** It updates derived text, provenance, relevant retrieval indexes, and stale state atomically or with explicit consistency handling.
7. **Policies do not own storage.** Policies return decisions/actions; engine/services execute them.
8. **Same trace, same policy/settings, same config, same seed => same non-wall-clock results.**
9. **Core correctness tests never require network access or model downloads.**
10. **Toy nominal costs are not research measurements.**
11. **Future demand is evaluator-only.** Policies must not receive synthetic group labels, future-access counts, or future events.

## Temporal semantics

Support these change types:

- initial fact;
- correction;
- real update over time;
- retraction;
- temporary exception.

A temporary exception must be able to expire so the underlying prior value becomes effective again without mutating that prior fact row.

Retractions should be represented with an immutable tombstone/event, not deletion.

Public timestamps must be timezone-aware; storage normalizes them to UTC ISO
8601. A bounded exception overrides its permanent base while active; a later
permanent event takes precedence. Effective exception expiry is a simulator
transition that can mark dependents stale and trigger maintenance. Preserve
historical resolution and deterministic `(timestamp, sequence_number)` ordering.

## Stale episodes

A relevant correction starts a new stale episode for each affected summary.
Another correction before repair changes `stale_episode_id` and resets
`queries_this_episode` and `verification_cost_this_episode`. One event reaching
a summary through multiple dependency paths must reset it only once.

**`stale_since` is the start of the uninterrupted stale period**, not the start
of the latest episode. Further corrections preserve it until REPAIR clears it;
a later fresh-to-stale transition starts a new stale period. This implemented
Oldest-First behavior supersedes the original plan's `stale_since` reset detail.
Repairing a parent must not silently clear its stale descendants.

## Synthetic workload contract

- One memory is one independent entity summary over three initial facts;
  entities are people, projects, or places.
- Each memory gets exactly one correction; all corrections precede all queries.
  Queries are interleaved with a local seeded RNG, stable IDs, and fixed UTC times.
- Inclusive future-query bands are cold **0–2**, warm **3–10**, hot **11–50**.
  Defaults balance the groups, assigning any remainder to cold then warm.
  Explicit counts must sum to the memory count; equal range endpoints select
  exact query counts, including zero for cold memories.
- `synthetic-v1` JSON stores the resolved configuration, initial facts,
  templates/source slots, correction/query events, and evaluator-only demands.
  Identical configuration and seed must produce byte-identical artifacts.
- Validate workload structure, unique IDs/order, source references, correction
  coverage, and exact access counts before writing a file or opening SQLite.
  Generation refuses to overwrite an existing artifact. Replay/logging must
  preserve saved workload inputs, including through path aliases/hard links.
- Load initial state through the existing APIs with exact v1 provenance; replay
  the same saved events through the existing simulator for every policy.
- Synthetic replay uses keyword retrieval and a unique stable entity name per
  query so retrieval reaches the intended memory before and after repair.
  Hybrid retrieval is available in the toy demo. Do not add retrieval uncertainty,
  repeated corrections, dependency fan-out, or new workload regimes incidentally.
- Keep generated workloads and logs in temporary/output directories outside Git.

## Environment and commands

Support **Python 3.11+** as declared in `pyproject.toml`; keep code compatible
with 3.11. Reuse a suitable existing virtual environment. In this workspace,
`.venv311` has been used for core checks and `.venv` for optional semantic
checks; verify their interpreters/dependencies rather than assuming portability.

Run from the repository root using the same interpreter for installation,
tests, and CLI:

```bash
python -m pip install '.[dev]'
python -m pytest -q
python -m verify_repair.cli --help
python -m verify_repair.cli run-toy --policy verify-only
python -m verify_repair.cli run-toy --policy eager-refresh
python -m verify_repair.cli run-toy --policy repair-on-first-access
python -m verify_repair.cli run-toy --policy periodic-refresh --refresh-interval-events 100
python -m verify_repair.cli run-toy --policy oldest-first --refresh-interval-events 100 --repair-slots-per-tick 1
python -m verify_repair.cli run-toy --policy p1
python -m verify_repair.cli run-toy --policy p2
```

Editable installation works on normal setups. On this macOS workspace hidden
`.pth` files have caused editable imports to fail; see `README.md`. After code
changes, reinstall a regular install before CLI checks, or use `PYTHONPATH=src`
explicitly to run checkout code. Pytest already sets `pythonpath = ["src"]`.

For a synthetic smoke check, use a new output filename each time:

```bash
python -m verify_repair.cli generate-synthetic --memories 100 --seed 12345 --output /tmp/workload-100.json
python -m verify_repair.cli run-synthetic --workload /tmp/workload-100.json --policy repair-on-first-access
```

Use `--jsonl PATH` for structured logs and `--verbose` for transitions. Preserve
the stable log schema, trace/config hashes, and nominal cost accounting. Record
policy schedule options alongside seed/config when reporting replay commands.

## Testing expectations

Run focused tests for changed behavior, then the complete core suite:

```bash
python -m pytest -q
```

This is equivalent to `pytest -q` in the active environment. Report the actual
result and interpreter; historical test counts in plans are not current gates.
Every invariant or bug fix must have a regression test. Documentation-only
changes do not need new tests that merely mirror the documentation.

| Tests | Coverage to retain |
| --- | --- |
| `test_ledger.py`, `test_more_invariants.py` | Immutability, temporal resolution, provenance, propagation, rollback |
| `test_summaries_staleness.py` | Exact provenance, reverse lookup, cycle rejection, episode resets |
| `test_operations_policies.py`, `test_simulator.py` | VERIFY non-mutation, atomic REPAIR/index updates, baselines, canonical scenario, deterministic replay |
| `test_oldest_first.py` | Stale-period age, ranking/ties, repair limits, scheduled action validation |
| `test_threshold.py` | P1 exact switching, cost boundaries, episode resets, expiry, independent counts, rollback |
| `test_prediction.py` | P2 decay/recency, hot/cold behavior, P1 fallback, causal history, persistence, resets, rollback |
| `test_hybrid.py` | Exact RRF arithmetic and hybrid ranking with fake semantic retrieval |
| `test_synthetic.py`, `test_cli.py` | Seed isolation, artifact validation/round trips, demand isolation, retrieval, replay, CLI input preservation |
| `test_bootstrap.py` | SQLite foreign keys/schema |
| `test_semantic_optional.py` | Opt-in live BGE-M3/FAISS integration |

If optional semantic dependencies and cached BGE-M3 are available, also run:

```bash
RUN_SEMANTIC=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest -o addopts='' -m semantic
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m verify_repair.cli run-toy --policy eager-refresh --retrieval hybrid
```

Core pytest excludes the semantic marker and must remain independent of model
downloads. Do not install/download large models merely for an unrelated change;
report optional checks as unrun when unavailable. A skipped semantic test is not
a passed integration check. Use the offline flags with an existing cache.

Initialize BGE-M3 **before importing FAISS**: reversing this native-library
order caused a macOS crash. Semantic repair currently rebuilds/re-embeds the
full index; do not describe it as incremental. On update failure, SQLite rolls
back and the vector index is rebuilt from committed summaries. Preserve this
consistency handling and its regression coverage.

## Canonical sanity scenario

Keep an end-to-end fixture equivalent to:

```text
Alice.location v1 = Maryland
summary = "Alice is a researcher living in Maryland."
correction -> Alice.location v2 = Boston
```

Expected:

- summary becomes stale;
- VerifyOnly sees Boston as current evidence but stored summary remains Maryland/stale;
- EagerRefresh repairs the summary to Boston, updates provenance to v2, and later queries require no maintenance.
- RepairOnFirstAccess leaves it stale until retrieval, repairs it once, and
  incurs no later maintenance until another relevant change; an unqueried
  memory incurs no repair.

## Coding guidance

- Preserve Python 3.11 compatibility.
- Prefer simple, explicit code over premature abstractions.
- Keep policy, storage, retrieval, and simulator concerns separated.
- Use SQLite transactions for multi-step state changes.
- Keep heavy semantic retrieval dependencies optional.
- Do not add a dependency solely to avoid writing a small, clear function.
- Use stable IDs and deterministic tie-breaking in tests/retrieval.
- Fail loudly on invariant violations; do not silently repair corrupt state.
- Add concise module docstrings describing purpose, inputs/outputs, and usage;
  document CLI/dependency changes in `README.md`.
- Never commit secrets, API keys, generated model caches, or large experiment artifacts.

## Before modifying code

1. Follow the reading order above and identify the relevant scoped plan.
2. Inspect `git status`, the implementation, and existing tests; preserve unrelated work.
3. Determine whether the request maintains existing behavior or authorizes a new phase.
4. Preserve existing public APIs unless a change is necessary; if changed, update all tests/docs.

## Before declaring a task complete

1. Run relevant unit tests.
2. Run `python -m pytest -q`; run available optional checks as described above.
3. For deterministic maintenance, confirm no generative LLM dependency/call was introduced.
4. Confirm fact-ledger immutability still holds.
5. Confirm deterministic replay tests still hold.
6. For workload/policy changes, confirm identical saved inputs across policies and no future-demand leakage.
7. Check `git diff --check` and review the final diff for unrelated changes.
8. Summarize changed files, tests/results, optional checks skipped, limitations,
   and any deviation from the scoped plan. Keep development validation distinct
   from research findings and owner approval. Leave commit/push to the user
   unless requested; major PRs should receive review from a teammate.

## Phase boundary

The accepted original milestone plan remains complete. Maintain the implemented
baselines and Steps 3–5 within the requested task; start subsequent research work
only under its own scope. Updating this file does not authorize the next phase.
