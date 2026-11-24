# Timeline

Progress against Table I (Project Timeline and Deliverables) of the
[research proposal](../CMSC848N_Research_Proposal%20(1).pdf) and the updated
schedule in the midterm report (Table 2), as of **November 23, 2026**
(start of Week 13). The final paper and presentation are due in Week 15
(about December 7–11).

The step-by-step handoff plan to the final submission is in
[docs/final-plan/README.md](final-plan/README.md).

Status key: **Done** · **Partial** · **Not started**

## Summary

| Weeks | Milestone | Status |
| --- | --- | --- |
| 1–2 | Group formation and project selection | Done |
| 3–5 | Proposal, repository, ledger, summary store, hybrid search, Eager Refresh and Verify-Only | Done |
| 6–7 | Correction stream, staleness marking, remaining baselines | Done |
| 8–9 | 10K generator, P1, P2, oracle and regret, midterm report | Done |
| 10 | Repair budget, budget-aware oracle, evolving workloads, stale-use Periodic Refresh | Done (post-midterm Steps 1–4) |
| 11 | LLM VERIFY/REPAIR, measured costs, cost replay, semantic dirty-bit check | Done, pending team review (post-midterm Steps 5–6); LLM VERIFY prompt/validator mismatch open |
| 12 | P3 on held-out and shifted traces; artifact audit | Partial: prompt-injection test done; P3, CI, `pip-audit` remaining |
| 13–14 | LongMemEval subset, 100K cost replay, multi-seed evaluation, paper draft | Not started |
| 15 | Final paper and presentation | Not started |

## Completed work

### Weeks 1–7: Substrate and baselines — Done

- Append-only fact ledger (corrections, updates, retraction tombstones,
  expiring temporary exceptions) with current and historical lookups.
- Summaries with exact fact-version provenance, a reverse index, and transitive
  staleness through a dependency DAG; stale episodes with counters.
- FTS5 keyword retrieval, reciprocal rank fusion, optional BGE-M3 + FAISS.
- Deterministic VERIFY and REPAIR; replayable simulator with stable logs.
- Baselines: Verify-Only, Eager Refresh, Repair-on-First-Access, Periodic
  Refresh, Oldest-First.

Evidence: [IMPLEMENTATION_PLAN_PRE_LLM.md](../IMPLEMENTATION_PLAN_PRE_LLM.md)
milestones 0–10; commits `183f783`, `e779771`, `97c3851`, `b2c24ac`.

### Weeks 8–9: Midterm round — Done

- `synthetic-v1` workloads: one correction per memory, cold/warm/hot demand.
- P1 accumulated threshold and P2 predicted threshold.
- Hindsight oracle, regret, opt-in latency, `run-experiment` /
  `summarize-experiment`.
- 10K experiments (7 policies, ρ ∈ {2, 5, 10}, 5 seeds, balanced and
  cold-heavy mixes) and the midterm report: [docs/midterm/](midterm/).

### Week 10: Post-midterm Steps 1–4 — Done

| Step | What | Docs |
| --- | --- | --- |
| 1 | `synthetic-v2` evolving workloads: repeated changes of all four types, 1–6 facts per summary, stable/bursty/shifting demand, fan-out | [data/synthetic_v2/](../data/synthetic_v2/README.md) |
| 2 | Multi-episode scoring, stale-use penalty λ, `periodic-refresh-stale` | [docs/step2/](step2/README.md) |
| 3 | Shared repair budget per window with a savings-ordered deferred queue | [docs/step3/](step3/README.md) |
| 4 | Budget-aware hindsight oracle (exact integer program) and budget sweep | [docs/step4/](step4/README.md) |

Development results (1 seed, 100 memories): P1 stays within 1.31–1.58× of
the oracle on every trace; P2 beats P1 at c_r ∈ {2, 5} but is worse at
c_r = 10 on three of four traces; ordering deferred repairs by predicted
savings lowers cost by up to 21%.

### Week 11: Post-midterm Steps 5–6 — Done, pending team review

- **Step 5.** Opt-in local LLM mode (`--mode llm`, Qwen 2.5 7B Instruct,
  4-bit NF4) for VERIFY, REPAIR, and a fixed answer reader shared by all
  policies; validators for citations and provenance; event-time answer
  scoring. Live Zaratan H100 run (job 24252063): live test and canonical
  Maryland → Boston check passed. See [docs/step5/](step5/README.md).
- **Step 6.** Component timing; 1K GPU pilot (job 24252220); `cost-profile-v1`
  files; measured-cost replay (`--cost-profile`) with a knapsack budget
  oracle; semantic-index profile; evaluator-only dirty-bit check. See
  [docs/step6/](step6/README.md).
- Pilot numbers (seed 2001, development only): REPAIR p50 3.6 s, VERIFY p50
  2.5 s. Repair/verify cost ratio 1.9 by wall time.
- About half of stale flags are false alarms: structural precision 0.52
  (8,734 accesses); semantic precision 0.55 (300 sampled).
- **Open issues:**
  - 51% of LLM VERIFY calls fail validation and fall back (the v4 prompt asks
    for query-scoped evidence; the validator requires every changed slot),
    which inflates the measured VERIFY cost;
  - the toy reader answers a different attribute after a retrieval miss.

## Differences from the proposal

| Area | Proposal | Current implementation |
| --- | --- | --- |
| Periodic Refresh | Answers from stale summaries between refreshes | Both versions exist: `periodic-refresh` verifies; `periodic-refresh-stale` answers stale and is charged λ |
| Oldest-First | Spends the repair budget B | Uses its own repair slots per tick; works under `--budget` like every policy |
| P1 | Also a randomized version (about 1.58× competitive) | Deterministic version only |
| P2 | Repairs immediately if k̂·cv > cr, otherwise verifies | Hedged: uses the forecast only when confident, otherwise P1 |
| Synthetic generator | d = 1–20 facts per summary; GPT-4o-mini text | d = 1–6 in the saved and evaluation traces (generator default 3); template text (no hosted API) |
| Synthetic retrieval | Hybrid BGE-M3 search | Keyword search with unique entity names (isolates the policy) |
| LLM in the loop | At 10K | Opt-in, toy and small scale; larger runs will replay measured costs |
| Testing | pytest on every commit via GitHub Actions | pytest locally; no CI workflow yet |

## Next steps

### Week 13 (now)

- [x] **Step 5 Zaratan run:** live LLM outputs in `docs/step5/`.
- [x] **Step 6: measured costs:** cost profiles, replay, dirty bits
      (`docs/step6/`); team review pending.
- [ ] **Decide the LLM VERIFY fix** (v5 prompt or query-slot validator),
      then rerun the 1K pilot to refresh the cost profiles.

### Week 14

- [ ] **Step 7: hedged P3.** Learned demand predictor (gradient-boosted
      trees) trained on separate seeds, with a fallback to P1; should fix
      P2's carry-over problem from earlier episodes.
- [ ] Semantic dirty-bit check (flag precision) at toy or 1K scale.
- [ ] LongMemEval knowledge-update subset (answer quality), if time allows.
- [ ] Artifact audit: GitHub Actions CI, `pip-audit`, secrets check.
- [ ] Paper draft, extending `docs/midterm/midterm_report.tex`.

### Week 15

- [ ] Final paper and presentation slides
      ([final-report.md](final-report.md), [final-presentation.md](final-presentation.md)).

## Deliverable tiers

| Tier | Deliverable | Status |
| --- | --- | --- |
| 1 (Week 9) | Memory system with hybrid search and tests | Done |
| 1 | Synthetic generator with control over change types and query frequency | Done (synthetic-v2) |
| 1 | P1 and P2 versus baselines at 10K, with cost and p95 latency | Done (midterm) |
| 2 (Week 15) | All baselines and the hindsight oracle with regret | Done |
| 2 | Semantic dirty-bit check | Not started |
| 2 | P3 with input analysis | Not started (Step 7) |
| 2 | Repair-budget experiments; stable vs. shifting workloads at 10K | Partial: 100-memory development results; 10K runs belong to final-plan Step 9 |
| 2 | LongMemEval and RealTime QA answer quality | Not started (RealTime QA moved to stretch) |
| 2 | 100K scaling; reproducible repository and final paper | Not started (needs Step 6 cost replay) |
| 3 (stretch) | 1M cost-replay, hedged P3 under adversarial shift, public trace release | Not started |
