# AGENTS.md

## Purpose

This repository implements the research project **“Can Agents Learn When to Verify or Repair Memory Under Budget Constraints and Access Uncertainty?”**

The core research problem is a verify-versus-repair decision for stale derived memories:

- **VERIFY**: consult current source facts for the current query only; the stored summary remains unchanged and stale.
- **REPAIR**: rebuild the stored summary from current source facts; update its provenance/retrieval representation and clear its stale state.

The source fact history is authoritative. Derived summaries are caches/derived state and may become stale.

## Read this first

Read the accepted substrate plan and the run instructions:

- `IMPLEMENTATION_PLAN_PRE_LLM.md`
- `README.md`

The plan records completed milestones and acceptance criteria. Preserve its
accepted behavior when maintaining the substrate. Do not restart its milestone
sequence for a new task.

If repository structure already differs from the suggested structure in the plan, preserve established conventions unless changing them is necessary for correctness.

## Project state and handoff

The project owner has **approved the deterministic pre-LLM substrate**. All
milestones 0–10 in `IMPLEMENTATION_PLAN_PRE_LLM.md` are complete. This work is
ready for the owner's commit and push; approval does not authorize an agent to
begin the next research phase without a separately scoped request and plan.

The accepted substrate includes:

- immutable temporal fact ledger;
- derived summary store;
- exact fact-version provenance;
- fact-to-summary and summary-to-summary dependencies;
- staleness propagation;
- deterministic VERIFY and REPAIR;
- VerifyOnly and EagerRefresh baselines;
- deterministic event simulator and logs;
- toy data/traces;
- keyword retrieval;
- RRF/hybrid retrieval abstraction;
- optional BGE-M3 + FAISS semantic retrieval isolated from core tests.

The inspectable toy inputs are `data/toy/facts.yaml`, `summaries.yaml`, and
`events.yaml`. The simulator uses in-memory SQLite; its database disappears
after a CLI run. `configs/toy.yaml` contains nominal costs, not measured
research costs. The keyword and hybrid demos are documented in `README.md`.

Latest acceptance checks: on Python 3.11, `pytest -q` passed 23 core tests with
one optional semantic test deselected. On Python 3.13 with the optional
dependencies and cached BGE-M3 model, the marked semantic test and hybrid toy
demo passed. The semantic adapter initializes BGE-M3 before importing FAISS to
avoid a native crash observed with the opposite import order on macOS.

## Outside the accepted substrate

Do not add these while maintaining this accepted phase or preparing its
commit. Future work on them needs an explicitly scoped next-phase request:

- generative LLM calls or hosted APIs;
- Llama/Qwen/vLLM generation;
- LLM-backed verification or repair;
- hosted-LLM synthetic data generation;
- P1, P2, or P3;
- learned predictors;
- hindsight oracle;
- repair-budget scheduling;
- semantic dirty-bit LLM evaluation;
- LongMemEval or RealTime QA;
- 10K/100K/1M research experiments.

The project owners define “access uncertainty” as uncertainty about how frequently future queries access a specific fact or memory component. This definition does not prescribe a probability model or predictor; neither is implemented in the accepted substrate.

## Non-negotiable domain invariants

1. **Fact history is immutable.** Existing fact-version rows must never be updated or deleted. Corrections/updates/retractions are new immutable rows/events.
2. **History is queryable.** The system must resolve current and historical values.
3. **Provenance is exact.** Every derived summary records the exact fact versions used to build it.
4. **Staleness is dependency-driven.** A fact change marks direct and transitive dependent summaries stale.
5. **VERIFY is non-mutating with respect to summary content/provenance.** It may update counters/logs only.
6. **REPAIR is persistent.** It updates derived text, provenance, relevant retrieval indexes, and stale state atomically or with explicit consistency handling.
7. **Policies do not own storage.** Policies return decisions/actions; engine/services execute them.
8. **Same trace, same config, same seed => same non-wall-clock results.**
9. **Core correctness tests never require network access or model downloads.**
10. **Toy nominal costs are not research measurements.**

## Temporal semantics

Support these change types:

- initial fact;
- correction;
- real update over time;
- retraction;
- temporary exception.

A temporary exception must be able to expire so the underlying prior value becomes effective again without mutating that prior fact row.

Retractions should be represented with an immutable tombstone/event, not deletion.

## Stale episodes

A relevant correction starts a new stale episode for an affected summary. If another relevant correction happens before repair, it starts a new episode/reset for that summary. One correction event reaching a summary through multiple dependency paths must not reset it multiple times.

## Testing expectations

Run the repository's established checks for substrate changes:

```bash
pytest -q
```

If optional semantic dependencies and BGE-M3 are available, also run:

```bash
RUN_SEMANTIC=1 pytest -o addopts='' -m semantic
python -m verify_repair.cli run-toy --policy eager-refresh --retrieval hybrid
```

The marked test is excluded from ordinary `pytest -q`. Run commands from the
repository root in a virtual environment where the package is installed.

Every invariant or bug fix must have a regression test.

Important test groups:

- immutable ledger;
- temporal resolution;
- exact provenance/reverse index;
- dependency cycle rejection;
- transitive staleness propagation;
- VERIFY non-mutation;
- REPAIR atomicity and index update;
- VerifyOnly/EagerRefresh behavior;
- deterministic simulator replay;
- keyword retrieval;
- RRF calculation;
- canonical end-to-end correction scenario.

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

## Coding guidance

- Target Python 3.11.
- Prefer simple, explicit code over premature abstractions.
- Keep policy, storage, retrieval, and simulator concerns separated.
- Use SQLite transactions for multi-step state changes.
- Keep heavy semantic retrieval dependencies optional.
- Do not add a dependency solely to avoid writing a small, clear function.
- Use stable IDs and deterministic tie-breaking in tests/retrieval.
- Fail loudly on invariant violations; do not silently repair corrupt state.
- Never commit secrets, API keys, generated model caches, or large experiment artifacts.

## Before modifying code

1. Read `IMPLEMENTATION_PLAN_PRE_LLM.md`.
2. Inspect the current repository and existing tests.
3. Determine whether the request maintains the accepted substrate or starts a separately authorized phase.
4. Preserve existing public APIs unless a change is necessary; if changed, update all tests/docs.

## Before declaring a task complete

1. Run relevant unit tests.
2. Run `pytest -q`.
3. For substrate maintenance, confirm no generative LLM dependency/call was introduced.
4. Confirm fact-ledger immutability still holds.
5. Confirm deterministic replay tests still hold.
6. Summarize changed files, tests run, and any deviation from the execution plan.

## Accepted phase boundary

Every checkbox in `IMPLEMENTATION_PLAN_PRE_LLM.md` Section 17 was satisfied and
the project owner approved the deterministic pre-LLM substrate. Stop at this
handoff for the current push; subsequent research work needs its own scope.
