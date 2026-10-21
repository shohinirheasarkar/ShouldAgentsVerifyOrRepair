# Step 4: P1 verification threshold

This scoped extension implements the requested basic threshold policy after
the accepted substrate and Step 3. It does not implement an oracle, prediction,
shared budgets, measured costs, or new synthetic workload regimes.

## Switching convention

For fixed nominal verification cost `v` and repair cost `r`, repair on the first
stale access for which `(n + 1) * v >= r`, where `n` counts successful VERIFY
operations since the latest relevant correction. For positive costs this is
access `ceil(r / v)`. With `v=1, r=5`, accesses 1–4 verify and access 5 repairs;
the repair access does not also pay for verification. Later fresh accesses do
no maintenance. Free repair wins immediately, including when both costs are
zero; free verification with positive repair cost never reaches the threshold.
Negative and non-finite costs are invalid.

## Implementation scope

1. Reuse SQLite's `queries_this_episode` (incremented only by VERIFY) and its
   existing correction/repair resets. Keep the schema and float cost logs.
2. Extend the stale-access hook with an optional, immutable keyword context
   containing only the verification count and current fixed costs. Existing
   baseline calls with two arguments remain valid; custom policy hooks must
   accept the new keyword when used by the simulator. P1 requires this context.
3. Compute the threshold using exact rational arithmetic on the decimal string
   representations of configured costs. Reconstruct fixed-cost spending from
   the count to avoid accumulated binary-float drift. Variable costs within a
   stale episode remain outside this step.
4. Add storage-free `AccumulatedThreshold` as CLI policy `p1` for both replay
   commands. Add optional `--verify-cost` / `--repair-cost` overrides, retaining
   defaults and recording resolved costs through the existing config logs/hash.
5. Preserve ledger, staleness, maintenance transactions, retrieval, log schema,
   scheduled policies, and evaluator-only demand isolation.

## Validation

- Establish the pre-change Python 3.11 core baseline.
- Check exact switch positions for integer, fractional, non-integral ratios,
  close boundaries, zero costs, and invalid costs.
- Exercise episode resets before and after repair, independent memories,
  effective exception expiry, failed repair rollback, fresh/unqueried access,
  provenance/index updates, and deterministic policy reuse.
- Replay saved synthetic inputs, checking per-memory actions, input preservation,
  history immutability, and no future-demand leakage. Cover CLI cost overrides.
- Run focused tests, complete core suites, available offline semantic checks,
  toy/synthetic CLI smoke checks, and `git diff --check`.

All results are controlled development validation with nominal costs. Passing
tests do not imply research findings or owner acceptance.

## Completed validation

- Baseline: Python 3.11.16, `python -m pytest -q`: 148 passed, 1 deselected.
- Focused threshold, synthetic, and CLI tests: 176 passed.
- Final core suites on Python 3.11.16 and Python 3.13.3: **214 passed,
  1 optional semantic test deselected** on each interpreter.
- Cached BGE-M3/FAISS test on Python 3.13.3 with `RUN_SEMANTIC=1`,
  `HF_HUB_OFFLINE=1`, and `TRANSFORMERS_OFFLINE=1`: **1 passed**.
- All six toy keyword CLI policies passed. Offline hybrid toy runs passed for
  EagerRefresh and P1 (P1 costs 0.1/0.3 exercise a query-time repair).
- All six policies replayed the same saved 100-memory workload, seed `12345`,
  with identical trace hashes, unchanged input bytes, and current evidence on
  every question. Scheduled policies used interval 100 and one Oldest-First slot.
  At nominal costs 1/5, P1 processed 1,313 questions with 295 verifications,
  53 repairs, and zero stale-source uses. Repeating P1 in a separate process
  produced byte-identical JSONL logs.
- Tests check all exact future-access counts from 0 through 50, multiple cost
  ratios, immutable fact history, query-local VERIFY, REPAIR provenance and
  retrieval updates, reset/rollback behavior, and demand isolation.
- `git diff --check` passed. No dependencies or generative LLM calls were added.

CLI checks used `PYTHONPATH=src` with the selected environment's interpreter to
execute checkout code. Generated workloads/logs remained outside Git. The
implementation follows the scope above; no additional research phase began.
