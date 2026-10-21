# Step 3: Deterministic synthetic workloads

This is the separately scoped extension requested after the accepted pre-LLM
substrate and the three additional comparison baselines. The accepted milestone
plan remains complete. Step 4 (P1) is outside this change.

## Implementation

1. Add a generator under `src/verify_repair/simulator/`, using a local seeded
   random number generator, stable IDs, and fixed UTC event times.
2. Generate people, projects, and places, with three initial facts and one
   template summary per entity. Correct one fact per summary exactly once.
3. Assign memories to cold (0–2), warm (3–10), and hot (11–50) groups. Default
   to balanced group sizes; support explicit sizes and inclusive query ranges
   within each group, including exact counts via equal endpoints.
4. Place all corrections before all questions. Randomly interleave questions
   afterwards, preserving each memory's exact future-access count. This first
   workload intentionally has independent memories and one stale episode each.
5. Save a versioned JSON workload containing configuration, facts, summary
   templates, typed event data, and evaluator-only demand metadata. Rehydrate
   initial state through existing ledger/summary APIs into a fresh SQLite DB;
   replay events with the existing simulator and policies. Policies receive no
   group labels or future counts.
6. Expose separate CLI commands to generate/save and load/replay workloads.
   Preserve the toy command and all existing public APIs. Add no dependencies.

## Validation, in order

- Establish the existing core-test baseline.
- Generate and replay 100 memories. Check exact provenance, correction-driven
  staleness, current query evidence, retrieval before/after repair, cold zero
  access, policy-independent inputs, immutable history, and deterministic logs.
- Increase to 1,000 memories and check structure and replay.
- Increase to 10,000 memories. Generate twice with identical configuration and
  seed and compare artifact SHA-256 hashes; replay the saved experiment.
- Cover range/count validation, changed seeds, local RNG isolation, saved
  workload round trips, CLI behavior, and deterministic generation in tests.
- Run `pytest -q`. Run the existing optional semantic checks if dependencies
  and the cached model are available.

Generated workloads and logs belong in temporary/output directories, not Git.
These are controlled development workloads; costs remain nominal, and this
step establishes no research findings or held-out evaluation.

## Completed validation

Validation progressed through these sizes in order, using seed `12345` and
the default balanced population/ranges. Each saved workload was replayed with
Repair-on-First-Access; the 100-memory tests also exercised all five policies.

| Memories | Questions | First-access repairs | Stale-source uses |
| --- | --- | --- | --- |
| 100 | 1,313 | 90 | 0 |
| 1,000 | 12,354 | 875 | 0 |
| 10,000 | 126,181 | 8,854 | 0 |

Two separately generated 10,000-memory artifacts had identical bytes and
SHA-256 `0ff28bf5b9c0add74dbfe8ff8e903231ddd821096486ba1dd1da66f0c43f57ca`.
Replay of the saved artifact confirmed every per-memory access count, current
evidence for every question, and preservation of all 30,000 initial fact rows
alongside the 10,000 appended correction rows.

- Baseline, Python 3.11: `pytest -q` — 39 passed, 1 deselected.
- Initial Step 3 implementation, Python 3.11: `pytest -q` — 80 passed,
  1 deselected. See the additional audit below for the current result.
- Python 3.13 with cached BGE-M3, `RUN_SEMANTIC=1 pytest -o addopts='' -m semantic`
  — 1 passed. The hybrid EagerRefresh toy demo also passed. Both were run with
  `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` to use the existing cache.
- Fact-ledger immutability, deterministic simulator replay, VERIFY non-mutation,
  and REPAIR provenance/index checks remain covered by passing tests. The ledger,
  simulator engine, policy implementations, and dependencies were unchanged.

No generative LLM calls/dependencies were added. There were no scope deviations;
the accepted substrate milestone sequence was not restarted.

## Additional completion audit

The requested follow-up audit found one input-validation gap: a saved artifact
could report a future-access count that disagreed with its query events. Added
`SyntheticWorkload.validate()` before saving, reading, or loading SQLite state.
It rejects inconsistent populations/group sizes, missing/shared source facts,
invalid correction coverage, mismatched access counts, and duplicate or
out-of-order event/query IDs. Valid artifacts retain their original bytes and
SHA-256. Regression tests cover the failure cases and rejection before any
output file or SQLite connection is created.

| Check | Result |
| --- | --- |
| Python 3.11 core suite | 148 passed, 1 optional semantic test deselected |
| Python 3.13 core suite | 148 passed, 1 optional semantic test deselected |
| Network independence | Python 3.11 core suite passed with socket connections and DNS disabled in the test process |
| Optional BGE-M3/FAISS test | 1 passed using the cached model with offline flags |
| Existing toy CLI | All five keyword policies and the hybrid EagerRefresh demo passed |
| Valid query counts | Every exact count from 0 through 50 replayed under VerifyOnly, EagerRefresh, and RepairOnFirstAccess |
| Saved inputs | Corrupt access counts rejected by the installed CLI with an error and no traceback |
| Packaging | Local package builds in both environments passed; `pip check` passed; installed generator source matched the checkout |
| Static checks | `compileall` and `git diff --check` passed |

The independent scale audit replayed **all five policies at 100, 1,000, and
10,000 memories**, in that order. For every query it checked the intended
summary ID and current evidence, as well as per-memory access counts, original
fact rows, exact provenance, stored stale/fresh text behavior, and retrieval
after repair. Direct ledger UPDATE and DELETE attempts were rejected.
Policies received the same trace hash at each size, and saved input artifacts
were unchanged by replay.

At 10,000 memories, all policies processed the same 126,181 questions:

| Policy (default schedule) | Verifications | Repairs | Stale-source uses |
| --- | --- | --- | --- |
| VerifyOnly | 126,181 | 0 | 0 |
| EagerRefresh | 0 | 10,000 | 0 |
| RepairOnFirstAccess | 0 | 8,854 | 0 |
| PeriodicRefresh | 0 | 10,000 | 0 |
| OldestFirst | 116,682 | 1,361 | 0 |

At all three sizes, generation in independent processes on Python 3.11 and
3.13 with different `PYTHONHASHSEED` values produced byte-identical artifacts.
A full 10,000-memory RepairOnFirstAccess replay also produced identical logs
on both Python versions: log SHA-256
`28d2ab5bca206ba423e8429ade2c3df913054b2c3a89f721a313c5a03f254fa3`.
The repeated full replay matched again, and 100-memory replays under three
policies passed for seven additional seeds: `0`, `1`, `7`, `42`, `54321`, `-1`,
and `9223372036854775808`.

The audit changed only synthetic validation, its regression tests, and related
documentation. The accepted ledger, maintenance services, simulator engine,
policy implementations, dependency list, and completed milestone plan were
unchanged. Step 4 was not started.
