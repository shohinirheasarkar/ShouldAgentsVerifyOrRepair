# Step 3: shared repair budget and deferred-repair queue

Every stale summary now competes for one repair resource. The budget is
opt-in: without `--budget`, runs, logs, and hashes are byte-identical to before.

## Rules

| Question | Rule |
|---|---|
| Unit | Same nominal units as `c_r`. Every REPAIR reserves exactly `c_r`, whatever triggered it (correction hook, query, scheduled tick, or the queue). |
| Window | `[origin + w*W, origin + (w+1)*W)`; `W` = `--budget-window-seconds` (default 3600). Experiments use the synthetic trace start as `origin`. |
| Reset | Budget resets to `B` when the first event of a new window is processed. No carry-over. |
| Verification | Free with respect to `B` (still costs `c_v` in the scored total). |
| Denied query-time repair | The query is **verified instead** (the answer stays current) and the repair is queued. |
| Denied correction-time or tick repair | Logged as a deferred `NO_OP` and queued. |
| Failure | The reservation is refunded and the error propagates; repair itself is one SQLite transaction. |
| Batches | A correction hook or tick batch is validated in full (targets, action types, duplicates) before any repair runs. Within a valid batch, repairs run in the policy's order until the budget runs out; the rest are queued. |
| Queue | One entry per stale summary. A later source change re-tags the entry with the new stale episode (`requeued`); any repair removes it. At each reset the queue drains, best first, until the budget runs out. Entries whose summary is no longer stale or whose episode no longer matches are dropped (`invalidated`). |
| Priority | `predicted-savings` (default): `forecast_accesses * c_v - c_r`, from past accesses only (P2's decayed-count estimator). `oldest`: by `stale_since`. Ties by `stale_since`, then summary ID. |

The scorer attributes queue repairs to the episode that was current just
before the draining event, because the queue drains before that event is
processed.

## Logs and metrics

With a budget, each log row adds `budget_window`, `budget_remaining`, and
`budget_outcome` (`charged` or `deferred`). `RunResult.budget` and the experiment
CSV (`all` rows) report `deferrals`, `executed_from_queue`, `requeued`,
`still_queued`, `peak_queue`, `mean_wait_s`, `max_wait_s`, and
`max_window_spend` (never above `B`; enforced in the tests).

## Commands

```bash
python -m verify_repair.cli run-synthetic --workload data/synthetic_v2/fanout-100-seed1.json \
  --policy eager-refresh --repair-cost 5 --budget 10
python -m verify_repair.cli run-experiment --workload data/synthetic_v2/{stable,bursty,shifting,fanout}-100-seed1.json \
  --repair-costs 5 --budget 15 --queue-order predicted-savings --output budget.csv
```

## `budget-compare.csv` (development data)

All 8 policies on the four saved 100-memory traces, `c_v = 1`, `c_r = 5`,
`B ∈ {5, 15}` per hour, both queue orders. Mean total cost over the four traces:

| Policy | B=5 oldest | B=5 savings | B=15 oldest | B=15 savings |
|---|---|---|---|---|
| eager-refresh | 1105 | 966 | 1145 | 900 |
| repair-on-first-access | 1077 | 960 | 1052 | 879 |
| periodic-refresh | 1099 | 970 | 1144 | 924 |
| p1 | 1012 | 971 | 903 | 884 |
| p2 | 1012 | 968 | 906 | 859 |
| oldest-first | 1080 | 1080 | 1078 | 1078 |
| verify-only | 1052 | 1052 | 1052 | 1052 |

- Ordering deferred repairs by predicted savings lowers cost for every policy
  that queues work (by up to 21% for eager refresh at B = 15). Oldest-first
  ordering spends scarce repairs on memories that are rarely asked again.
- Under a tight budget, repair-heavy policies stop being much better than
  Verify-Only, and the budget-aware oracle (Step 4) is needed to say how far
  each one is from the best achievable cost.
- Oldest-First's own tick slots (1 per 100 events) rarely exceed `B`, so the
  queue order barely affects it.
