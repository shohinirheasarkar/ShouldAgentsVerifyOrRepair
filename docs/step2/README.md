# Step 2: multi-episode scoring and the stale-use baseline

## What an episode is

For synthetic-v2 traces, a **source-change episode** for a summary starts at any
change to one of its source facts (correction, update, retraction, temporary
exception, or the effective expiry of an exception) and lasts until the next
such change or the end of the trace. `k` is the number of queries to the
summary in the episode, **including queries after a repair**: a repair pays
off for every later query until the next source change. Queries before a
summary's first change are "fresh" and cost nothing under every policy.

The episode metadata is written by the generator (Step 1). The scorer checks
that the engine's `stale_episode_id` on every VERIFY / REPAIR / stale-use row
matches the trace episode at that point in event order, and that per-episode,
per-memory, and run totals reconcile with the simulator's counters.

## Cost and oracle semantics

Total cost of a run: `C = c_v * N_verify + c_r * N_repair + λ * N_stale`,
where `N_stale` counts query answers whose evidence used a non-current source
version (`used_stale_source`). λ is set with `--stale-penalty` (default 10).
This is **stale evidence use**, not a measured answer error; generated-answer
errors arrive with the LLM reader in Step 5.

Two hindsight oracles, both per episode, summed over episodes:

| Oracle (`oracle` column) | Used for | Per-episode cost |
|---|---|---|
| `verify-repair` | policies that always VERIFY or REPAIR a stale summary (all seven midterm policies) | `min(k * c_v, c_r)` |
| `verify-repair-stale` | policies that may answer from a stale summary (`periodic-refresh-stale`) | `min(k * min(c_v, λ), c_r)`; `0` for expiry episodes |

**Why these formulas hold here.** Synthetic-v2 summaries have no summary-to-
summary dependencies, and each query retrieves exactly one summary, so
episodes are independent single rent-or-buy problems. Fan-out (one fact feeding
several summaries) does not break this without a budget: each dependent
summary gets its own episode and its own decision.

**Where they need care.** When a temporary exception expires, a summary last
built *before* the exception becomes correct again while still flagged stale;
answering from it is free and is not a stale use. The stale-allowed oracle
therefore uses a relaxed bound of 0 for expiry episodes (still a valid lower
bound), and `relaxed_oracle_episodes` reports how many episodes this affects.
Verify/repair-only policies must still act on a flagged summary, so their
oracle is unchanged. With a shared repair budget (Step 3) episodes stop being
independent; Step 4 adds a budget-aware oracle.

## New metrics (synthetic-v2 rows)

- `episodes`, `stale_cost` (= λ * stale uses)
- `stale_exposure_hours`: total time summaries spent flagged stale
- `repaired_episodes`, `mean_time_to_repair_s`, `p95_time_to_repair_s`: time from
  `stale_since` (the first unrepaired change) to the repair
- `peak_backlog`, `mean_backlog`: number of simultaneously stale summaries
  (time-weighted mean over the trace)

Groups (`cold`/`warm`/`hot`) are assigned per episode by the memory's group in
the demand phase where the episode starts, so a shifting memory can contribute
to different groups over the trace.

## Files

- `handcheck.csv`: all 8 policies on `data/synthetic_v2/handcheck-tiny.json`
  (c_v = 1, c_r = 3, λ = 10, tick every 4 events). Every number is derived by
  hand in `tests/test_scoring_v2.py` (`HAND_CHECKED`).
- `comparison-100.csv`: all 8 policies on the four saved 100-memory traces
  (stable, bursty, shifting, fan-out) at c_r ∈ {2, 5, 10}. Development data only.

Reproduce:

```bash
python -m verify_repair.cli run-experiment --workload data/synthetic_v2/handcheck-tiny.json \
  --repair-costs 3 --refresh-interval-events 4 --output handcheck.csv
python -m verify_repair.cli run-experiment \
  --workload data/synthetic_v2/{stable,bursty,shifting,fanout}-100-seed1.json \
  --repair-costs 2 5 10 --output comparison-100.csv
```

## First observations (development data, 1 seed, 100 memories)

- P1 stays within 1.31–1.58x of the oracle on every trace and cost ratio; it is
  again the most robust policy without prediction.
- P2 beats P1 at c_r = 2 and 5 but is *worse* than P1 at c_r = 10 on three of
  four traces (up to 1.73 vs 1.36). With repeated episodes, P2's access history
  carries over from earlier episodes, so a memory that was busy before a
  correction can trigger an early repair after it. This is a concrete target
  for the hedged P3 in Step 7.
- Stale-use periodic refresh is the most expensive policy at λ = 10: cheap
  maintenance is paid for in stale answers.
