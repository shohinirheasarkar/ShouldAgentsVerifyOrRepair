# Timeline

Progress against Table I (Project Timeline and Deliverables) of the
[research proposal](../CMSC848N_Research_Proposal%20(1).pdf), as of
**October 25, 2026**. We are at the Weeks 8–9 block, with the midterm
(Tier 1 deliverables) due at the end of Week 9.

Status key: **Done** · **Partial** · **Not started**

## Summary

| Weeks | Proposal milestone | Status |
| --- | --- | --- |
| 1–2 | Group formation and project selection | Done |
| 3–5 | Proposal, repository, ledger, summary store, hybrid search, Eager Refresh and Verify-Only | Done (costs are nominal, not measured) |
| 6–7 | Correction stream, staleness marking, remaining baselines | Done |
| 8–9 | 10K generator, P1, P2, re-run baselines, midterm report | Partial: code done; results, plots, latency, and report remaining |
| 10–11 | Hindsight oracle, semantic dirty-bit check, P3, repair budget | Not started |
| 12 | Correction-rate and shifting workloads, varied facts per summary, artifact audit | Not started |
| 13–14 | LongMemEval and RealTime QA, 100K scale, repository cleanup, paper draft | Not started |
| 15 | Final paper and presentation | Not started |

## Completed work

### Weeks 1–2: Project selection — Done

- Team formed and the verify-or-repair project selected.

### Weeks 3–5: Proposal and memory substrate — Done

- Proposal submitted; repository set up.
- Source fact ledger: append-only SQLite table with corrections, updates,
  retractions (tombstones), and expiring temporary exceptions; current and
  historical lookups.
- Derived summary store with exact fact-version provenance and a
  fact-to-summary reverse lookup.
- Hybrid search: FTS5 keyword retrieval, reciprocal rank fusion, and optional
  BGE-M3 + FAISS semantic retrieval.
- Eager Refresh and Verify-Only baselines.
- Toy dataset (20 facts, 10 summaries) with a CLI demo that shows each
  summary's source facts.
- Deterministic VERIFY and REPAIR (template-based, no LLM yet).
- **Gap:** costs are fixed nominal units (verify = 1, repair = 5). The
  proposal's "first cost measurements" (GPU time, tokens) require the LLM
  operations, which are not built yet.

Evidence: commit `183f783`; [IMPLEMENTATION_PLAN_PRE_LLM.md](../IMPLEMENTATION_PLAN_PRE_LLM.md) milestones 0–10.

### Weeks 6–7: Corrections and remaining baselines — Done

- Replayable correction/query simulator with stable structured logs.
- Staleness marking that passes through chains of summaries, with stale
  episodes and episode counters.
- Repair-on-First-Access, Periodic Refresh, and Oldest-First baselines.
- Baseline comparisons on the toy dataset.

Evidence: commits `e779771`, `97c3851`, `b2c24ac`; [evaluation protocol](evaluation_protocol.md) (`95e7090`).

### Weeks 8–9: Synthetic workloads, P1, P2 — Partial

Done:

- Seeded synthetic workload generator (`generate-synthetic`) with cold
  (0–2), warm (3–10), and hot (11–50) query groups, saved as reproducible
  JSON. Replay is checked at 100, 1,000, and 10,000 memories.
- P1 accumulated threshold: repairs on query `⌈cr/cv⌉` of an episode.
- P2 predicted threshold: a time-decayed moving average of past accesses
  triggers an early repair when predicted reuse is high and confident;
  otherwise P2 behaves exactly like P1.
- All seven policies replay the same saved workload through the CLI.

Remaining for the midterm:

- [ ] Re-run all baselines, P1, and P2 at 10K on identical traces and save
      results (CSV with seed, costs, and policy settings).
- [ ] Cost plots by policy and by cold/warm/hot group.
- [ ] p50/p95 query latency. Wall-clock latency is not recorded yet; the log
      field `latency_ms_instrumentation_only` is always null.
- [ ] Midterm report ([checklist](midterm.md)).

Evidence: commits `c95af5a`, `af7a995`, `3704039`.

## Differences from the proposal so far

Decide whether to keep each difference and describe it in the midterm report,
or to change the code to match the proposal.

| Area | Proposal | Current implementation |
| --- | --- | --- |
| Periodic Refresh | Uses stale summaries as-is between refreshes, which incurs the stale-answer penalty λ | Verifies stale accesses between refreshes, so it never gives a stale answer |
| Oldest-First | Spends the repair budget B | Uses a fixed number of repair slots per tick; no budget B exists yet |
| P1 | Also a randomized version (about 1.58× competitive) | Deterministic version only |
| P2 | Repairs immediately if k̂·cv > cr, otherwise verifies | Repairs early only when the forecast is confident; otherwise falls back to P1 |
| Synthetic generator | Poisson corrections with a mix of change types, d = 1–20 facts per summary, GPT-4o-mini text | One correction per memory, all before any query; 3 facts per summary; template text |
| Synthetic retrieval | Hybrid BGE-M3 search | Keyword search with unique entity names (isolates policy behavior from retrieval errors) |
| VERIFY / REPAIR | Local Llama 3.1 8B or Qwen 2.5 7B | Deterministic templates |
| Testing | pytest on every commit via GitHub Actions | pytest locally; no CI workflow yet |

Because every correction comes before the first query, Periodic Refresh
repairs every memory at its first tick. On synthetic workloads it therefore
matches Eager Refresh. A fair comparison of the scheduled baselines needs
corrections spread through the trace (planned for Week 12).

## Next steps

### Now → end of Week 9: Midterm (Tier 1)

1. Build an experiment runner that replays one saved workload under every
   policy and writes a results CSV.
2. Add wall-clock latency instrumentation for p50/p95.
3. Run at 10K across several seeds and cost ratios ρ = cr/cv; produce cost
   and latency plots.
4. Write the midterm report: preliminary results, the differences above, and
   the updated plan.

### Weeks 10–11

- [ ] Hindsight oracle: per-episode cost `min(k·cv, cr)`, computed by the
      evaluator from the saved true demand; checked against brute force on
      small hand-built cases. Report regret and `C(π)/C(oracle)` by group.
- [ ] Repair budget B that resets each time window: skipped repairs go to a
      waiting list ordered by predicted savings; a budget-aware oracle
      (knapsack, exact at 10K).
- [ ] P3 learned demand policy (XGBoost or LightGBM) trained on separate
      traces; analysis of which inputs matter most.
- [ ] Semantic dirty-bit check and flag precision.
- [ ] Decide when to add the local 8B model for VERIFY and REPAIR. The
      proposal does not schedule it explicitly, but measured costs, the
      dirty-bit check, and answer quality all depend on it.

### Week 12

- [ ] Workloads with varying correction rates, corrections spread through
      the trace, shifting demand (memories moving between groups), and a
      varied number of facts per summary.
- [ ] Artifact audit: peer review, regression check, GitHub Actions CI,
      `pip-audit`, a check that no secrets are committed, and a
      prompt-injection test.

### Weeks 13–14

- [ ] LongMemEval and RealTime QA subsets for answer quality.
- [ ] 100K-scale runs on the RTX 5070 machine or Zaratan; find where the
      system slows down.
- [ ] Clean up the repository for reproducibility; draft the paper.

### Week 15

- [ ] Final paper and presentation slides.

## Deliverable tiers

| Tier | Deliverable | Status |
| --- | --- | --- |
| 1 (Week 9) | Memory system with hybrid search and tests | Done |
| 1 | Synthetic generator with control over change types and query frequency | Partial: query frequency only; corrections only, no other change types |
| 1 | P1 and P2 versus Eager Refresh, Verify-Only, and Repair-on-First-Access at 10K, with cost and p95 latency | Partial: policies done; results and latency remaining |
| 2 (Week 15) | All baselines, dirty-bit check, hindsight oracle and regret | Baselines done; rest not started |
| 2 | P3 with input analysis | Not started |
| 2 | Repair-budget experiments; stable vs. shifting workloads at 10K | Not started |
| 2 | LongMemEval and RealTime QA answer quality | Not started |
| 2 | 100K scaling; reproducible repository and final paper | Not started |
| 3 (stretch) | 1M cost-replay, hedged P3, public trace release | Not started |
