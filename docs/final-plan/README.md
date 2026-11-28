# 848N Project To-Do — From Midterm to Final Submission

The team's step-by-step plan from the midterm to the final submission, copied
from our planning page. Post-midterm Steps 1–5 here match `docs/step2/` to
`docs/step5/`.

> **Work through the steps in order.** Each step is one handoff: finish its
> implementation, tests, and output before the next person starts the
> following step. Resolve remaining implementation choices inside the
> relevant step.

**Repo status (November 28, 2026).** Steps 1–4 are merged (Shohini). Step 5's
initial code is merged (Arik); the Step 5/6 fixes and v6 pilot are on
`step5-6-llm-costs`, pending commit and merge. Arik completed the independent
teammate review, including the 14 dirty-bit cases, on November 28. Later scale
tasks remain separate.
`scripts/run_v2_eval.sh` (a
nominal-cost 10K synthetic-v2 runner) exists but has not been used for any
reported result; reconcile its seeds with the Step 7.2 split before use in
Step 9.

## Completed starting point

- [x] **Midterm report submitted.**
- [x] **Memory substrate and seven policies implemented:** immutable temporal ledger, exact provenance/staleness, deterministic VERIFY/REPAIR, keyword/hybrid retrieval, five baselines, P1, and P2.
- [x] **Synthetic-v1, single-episode oracle, and experiment tooling implemented.**
- [x] **Midterm 10K development experiments completed:** balanced and cold-heavy mixes, five seeds, and cost ratios 2/5/10. Costs are nominal; the balanced mix includes substrate latency.

Keep these working throughout. Each implementation handoff includes relevant regression tests, the complete core suite, an independent review of major changes, and a runnable command or saved artifact. Preserve existing defaults and saved workloads when adding new modes.

## Step 1 — Extend the synthetic generator to evolving workloads

Produce saved traces with multiple stale episodes and changing demand that later implementations can all replay.

- [x] **1.1** Add a new versioned workload format with corrections interleaved with queries, configurable Poisson correction rates, and repeated corrections. Keep existing synthetic-v1 files and CLI behavior working.
- [x] **1.2** Generate updates, retractions, and temporary exceptions with effective expiry using the existing ledger APIs.
- [x] **1.3** Add stable, cold-heavy, bursty, and shifting cold/warm/hot demand; configurable 1–20 facts per summary; and bounded dependency fan-out.
- [x] **1.4** Store exact episode access counts and shift schedules as evaluator-only metadata. Validate references, correction coverage, ordering, episode boundaries, and byte-identical generation for the same settings/seed.
- [x] **1.5** Add small regression traces for repeated corrections, expiry, fan-out, and a never-queried memory. Preserve immutable history, uninterrupted `stale_since`, and deterministic replay.

**Handoff:** A new generate/save/load/replay command or mode, representative saved traces, and passing workload regressions. Step 2 uses these traces.

**Code / reference:** `src/verify_repair/simulator/synthetic.py`; `tests/test_synthetic.py`.

## Step 2 — Extend scoring and add the stale-use scheduled baseline

Make the new traces score correctly and make scheduled-baseline differences observable before adding a shared budget.

- [x] **2.1** Generalize scoring to source-change episodes, counting accesses through the next relevant change or trace end, including accesses after a repair. Reconcile episode, summary, group, and run totals.
- [x] **2.2** Add a separately named Periodic Refresh variant that uses stale summaries between ticks. Preserve the current verifying periodic-refresh default and existing scheduled ordering.
- [x] **2.3** Log stale evidence use, stale exposure, time-to-repair, and backlog. Add a configured stale-use penalty λ; keep these evidence metrics separate from actual generated-answer errors added in Step 5.
- [x] **2.4** Extend the unbudgeted evaluator for repeated episodes and the selected action/penalty semantics. Apply min(k·cv, cr) only where its VERIFY/REPAIR assumptions hold.
- [x] **2.5** Replay every existing policy and the new variant on identical interleaved traces. Hand-check costs, stale-use counts, tick boundaries, and oracle totals against small exhaustive cases.

**Handoff:** A working multi-episode scorer, explicit stale-use baseline, and a small comparison CSV with hand-checked expected values. Step 3 adds resource constraints to this path.

**Code / reference:** `src/verify_repair/evaluation.py`; `src/verify_repair/policies/periodic_refresh.py`; `src/verify_repair/simulator/engine.py`.

## Step 3 — Implement the shared repair budget and deferred queue

Make all stale memories compete for the same repair resource, including query-time and scheduled repairs.

- [x] **3.1** Add explicit budget-unit, B, and reset-window configuration. Document boundary timing, carry-over, verification charges, and failure/refund behavior alongside the implementation.
- [x] **3.2** Enforce atomic repair reservation/charging in the engine across all policy triggers. Log starting, spent, and remaining budget; reject invalid action batches before any repair executes.
- [x] **3.3** Queue denied repairs, VERIFY stale queries while they wait, and order deferred work by past-only predicted savings with deterministic ties.
- [x] **3.4** Invalidate or recompute queued requests after another correction, expiry, or repair. Prevent requests from an old episode from executing against the wrong state.
- [x] **3.5** Apply the same B/window rules to the existing policies and compare Oldest-First with predicted-savings scheduling. Preserve the old unlimited-budget modes.
- [x] **3.6** Test zero, tight, exact-fit, and ample budgets; window resets; stale queue entries; failed repairs; rollback; and zero overspending. Add defer counts, wait time, and peak backlog to logs.

**Handoff:** Budgeted replay commands and queue/accounting tests, plus a tiny trace where multiple summaries compete for B. Step 4 scores this exact implementation.

**Code / reference:** `src/verify_repair/simulator/engine.py`; `src/verify_repair/policies/base.py`; `src/verify_repair/policies/oldest_first.py`.

## Step 4 — Implement the budget-aware oracle

Provide a defensible hindsight reference under the same constraints as the budgeted policies.

- [x] **4.1** Implement the optimization for the actual reset windows, repeated corrections, allowed actions, and cost/λ objective from Steps 2–3. Keep all future information inside the evaluator.
- [x] **4.2** Use exact knapsack at 10K where the problem reduction is valid. Account for cross-window effects explicitly; if exactness requires restrictions, report them and any solver gap.
- [x] **4.3** Validate the oracle against exhaustive search on small traces, including zero demand, tight budgets, repeated corrections, and repairs that lose value before the next reset.
- [x] **4.4** Add budget-matched regret/ratio outputs to the experiment runner. Label any large-scale greedy comparator as a heuristic rather than an optimal oracle.
- [x] **4.5** Run a small budget sweep and save cost, defer/backlog, and budget-versus-cost curves for the baselines and P1/P2.

**Handoff:** A validated budget-aware evaluator and runnable budget sweep with saved results. Later policy implementations reuse the same scorer.

**Code / reference:** `src/verify_repair/evaluation.py`; `src/verify_repair/experiments.py`; `tests/test_evaluation.py`.

## Step 5 — Implement local LLM VERIFY, REPAIR, and answer generation

Replace template-only operations in an optional mode while preserving the deterministic implementation.

- [x] **5.1** Get one report-listed local model running: 4-bit Qwen 2.5 7B or Llama 3.1 8B on the RTX 5090/Colab, with Zaratan as fallback. Save model/runtime/quantization versions, decoding settings, and a working toy command.
- [x] **5.2** Implement opt-in LLM VERIFY using current authoritative evidence without mutating stored summary text or provenance.
- [x] **5.3** Implement LLM REPAIR with validated, supported claims and exact source links; preserve unrelated correct content. Update text, provenance, retrieval indexes, and stale state with rollback/recovery.
- [x] **5.4** Add one fixed answer reader and prompt for every policy. Save answers and evidence references; score synthetic exact match/token F1, abstention, and outdated-fact errors against event-time truth.
- [x] **5.5** Test model adapters with fake responses, including unsupported claims, timeout, failed validation, and rollback. Keep core tests offline and deterministic operations available.
- [x] **5.6** Run the canonical Maryland→Boston case end to end under VERIFY and REPAIR, including scored answers and persisted-summary checks.

**Handoff:** Working deterministic and local-LLM modes, versioned prompts, scored toy answers, and operation regression tests. Step 6 measures this implementation.

**Status (November 28, 2026):**

- **5.1 and 5.6 ran on Zaratan** (job 24252063, H100): the live test passed,
  and the canonical check passed for all four policies (`docs/step5/README.md`).
- **Toy reader issue.** V4 answered Alice's office question with her location
  after a retrieval miss. The prompt-only v5 fix regressed stale answering
  (job 24299576). V6 restores the v4 prompt and uses a source-identity
  support check; the focused live check passed according to the Zaratan run
  report. The archived v6 manifest reports 0.60 toy exact match for the three
  current-evidence policies, with no false abstentions; the retrieval miss remains.
- **LLM fallback.** Opt-in `llm_fallback` (deterministic VERIFY/REPAIR, reader
  abstains) keeps long pilots running after a validation failure. It is logged
  per row.

**Code / reference:** `src/verify_repair/maintenance/operations.py`; `src/verify_repair/maintenance/summary_builder.py`; `tests/test_operations_policies.py`.

## Step 6 — Measure LLM costs, implement cost replay, and evaluate dirty bits

Obtain the measured cost profiles and semantic-staleness evidence needed for the final study.

- [x] **6.1** Instrument VERIFY, REPAIR, retrieval, source lookup, generation, embeddings/index updates, and answer generation. Record tokens and GPU/compute time with explicit units; separate background work from query-visible latency.
- [x] **6.2** Run toy then 1K pilots. Save cost distributions, p50/p95/p99, warm/cold behavior, hardware/concurrency, answer quality, repair fidelity, failures, and representative examples.
- [x] **6.3** Profile full semantic-index rebuilds separately from generation cost. If they prevent fair runs, implement a scoped incremental/cache update with stable IDs and rollback recovery; retain the committed-summary rebuild fallback.
- [x] **6.4** Implement measured-cost replay at 10K/100K using saved profiles and reproducible keyed cost samples shared across policies. Define the estimates policies can see; preserve existing fixed-cost P1/P2 behavior and keep future realized costs hidden.
- [x] **6.5** Implement evaluator-only semantic dirty-bit checks on sampled toy/1K queries by comparing stale-memory and freshly rebuilt-memory answers against current truth. Report flag precision, sample size, uncertainty, and documented disagreement cases. Independent teammate confirmation is recorded below.
- [x] **6.6** Save cost profiles and exact replay commands. If 1K LLM runs are infeasible, use the midterm's toy-measurement fallback and record the resulting limitation; replayed costs must remain labeled as replay.

**Handoff:** Measured cost-profile files, a deterministic cost-replay mode, 1K pilot outputs, and dirty-bit results. Step 7 uses these costs when evaluating learned decisions.

**Status (November 28, 2026):** The v6 rerun completed on an H100 (job
24312071; scratch `step6-v6-20261128-001639/pilot`). All four toy runs, two
3,000-query cost runs, two 1,000-query quality runs, and three profile builds
completed. The local full core suite passed (860 passed, 2 deselected) and a
focused live reader check was reported as passed. Arik completed the
independent teammate review, including the 14-case dirty-bit audit.

- **Corrected VERIFY cost.** Query-scoped validation reduced cost-run VERIFY
  fallbacks from 451/883 (51.1%, v4) to 11/883 (1.25%, v6). V6 pooled wall-time
  estimates are 886 ms/VERIFY and 3,321 ms/REPAIR (ratio 3.75); the v4 profiles
  remain historical. Eleven VERIFY fallbacks show residual schema errors, so
  the new profile includes retries and fallbacks.
- **6.2.** The transferred v6 manifest reports 0.972 exact match for
  Verify-Only and 0.969 for Repair-on-First-Access on 1,000 queries each;
  both have zero outdated-fact errors. REPAIR used deterministic fallback in
  9/310 cost calls and 1/63 quality calls after validation failures. Toy
  exact match is 0.60 for the three current-evidence policies, now with zero
  false abstentions and zero outdated-fact errors. Validation enforces
  claim/source fidelity; an independent semantic audit of REPAIR prose was
  not part of this pilot.
- **6.3.** The earlier semantic-index profile found no incremental index
  needed for keyword-only experiments. The v6 semantic profile skipped because
  `venv-semantic` was absent; no new semantic timing was measured.
- **6.4.** Measured-cost replay and keyed draws are implemented and covered
  by offline tests. The v6 wall-time profile was replayed at 100 memories;
  a 10K development replay is recorded in `docs/step6/README.md`. The full
  100K scale evaluation remains Step 11.
- **6.5.** The v6 sample reproduces the same 14 flagged query IDs documented
  in `docs/step6/dirty_bit_review.md`: ten changed-back values and four
  false abstentions on “delta.” Arik checked and accepted the event-time audit
  of all 14 cases on November 28; none is unresolved. The four reader errors
  remain in the reported accuracy.
- **6.6.** The v6 manifest, three verified profile files, dirty-bit sample,
  job log, base commit, and exact source patch are archived in
  `docs/step6/zaratan_24312071/`. Replay commands are in
  `docs/step6/README.md`.

- [x] **Independent teammate review:** Arik Gershman reviewed the Step 5/6
  changes and saved v6 evidence, confirmed all 14 dirty-bit cases, and signed
  `docs/step6/dirty_bit_review.md` on November 28 (job 24312071).

**Code / reference:** `src/verify_repair/retrieval/semantic.py`; `src/verify_repair/experiments.py`; `docs/midterm/midterm_report.tex`.

## Step 7 — Implement and train P3 and its hedged variant

Add causal learned demand prediction and a robust fallback, ready for held-out comparisons.

- [ ] **7.1** Export event-time feature rows from completed accesses: count/frequency/recency, correction history/type, time since correction, summary/dependency complexity, and available cost/budget estimates. Future accesses are labels only.
- [ ] **7.2** Create disjoint train/dev/test and unseen-shift traces before fitting. Keep midterm seeds 1–5 as development data; fit preprocessing on training data only and test that changing a future suffix cannot change earlier features or actions.
- [ ] **7.3** Train one XGBoost/LightGBM predictor; tune only on development traces. Save the feature schema, forecast horizon, dataset hashes, training seed, model, and selected settings.
- [ ] **7.4** Implement fully trusting P3 and a hedged variant with an explicit P1 fallback/trust rule. Policies return actions and use the same engine budget enforcement.
- [ ] **7.5** Test cold starts, bad high/low forecasts, corrections, repair, queueing, and budget denial. Record inference overhead and distinguish proven bounds from empirical robustness.
- [ ] **7.6** Run development ablations for feature groups, no prediction, no hedge, and trust sensitivity. Save feature-importance and decision-cost analyses, then freeze predictor/policy settings for final tests.

**Handoff:** Saved P3 model, both runnable policies, causal-feature tests, development ablations, and frozen settings. Step 8 checks the integrated artifact before final evaluation.

**Code / reference:** `src/verify_repair/demand.py`; `src/verify_repair/policies/predicted_threshold.py`; `tests/test_prediction.py`.

## Step 8 — Complete the artifact audit on the integrated implementation

Make the implementation testable and reproducible before running the final experiments. Target: November 22, 2026.

- [ ] **8.1** Add GitHub Actions for Python 3.11 core tests, covering new workload, budget, oracle, LLM-adapter, and causal-prediction regressions. Keep live model/semantic tests opt-in.
- [ ] **8.2** Create reproducible core/analysis/model environments and document commands, model/prompt revisions, seeds, hardware, and artifact locations. Include a small complete replay from workload generation through a result plot.
- [ ] **8.3** Run pip-audit, secrets/log checks, license review, and prompt-injection tests using malicious retrieved evidence. Record fixes and remaining findings.
- [ ] **8.4** Run the full core suite, available optional integrations, and diff checks; obtain independent review of major changes and resolve required findings.
- [ ] **8.5** Have a teammate reproduce the smoke study from a clean checkout. Submit the artifact audit with code revision, reproduction steps, tables/figures, review evidence, and submission link.

**Handoff:** A reviewed, reproducible integrated revision and the submitted audit package. Step 9 runs the primary held-out study on this implementation.

**Code / reference:** `docs/artifact-audit.md`; `AGENTS.md`; `pyproject.toml`.

## Step 9 — Run the final 10K controlled experiments

Produce held-out cost, quality, budget, and demand-shift comparisons for all required policies.

- [ ] **9.1** Extend result CSVs/plots to carry workload version/split, B/window, λ, model/predictor/cost-profile IDs, and timing units. Keep incompatible settings separate in summaries.
- [ ] **9.2** Save the runnable experiment configurations: all seven existing policies, new scheduled variant, P3/hedge, matched budget references, cost ratios 2/5/10, measured-cost profiles, and targeted workload/budget sweeps.
- [ ] **9.3** Replay the same saved histories, initial state, retrieval setup, and cost samples for every policy. Use at least five held-out seeds per primary setting; include stable/shifting demand, repeated corrections, and tight through nonbinding budgets.
- [ ] **9.4** Save per-seed outputs and produce paired differences with 95% intervals, including cold/warm/hot results, cost/regret, VERIFY/REPAIR/DEFER counts, backlog, and budget use.
- [ ] **9.5** Run targeted ablations for prediction/hedging, correction rate, summary complexity, budget, and nominal versus measured costs. Keep actual answer quality/latency from LLM runs distinct from cost-only replay outputs.
- [ ] **9.6** Collect forecast failures, unnecessary repairs, budget starvation, and model/retrieval errors; retain negative and null results.

**Handoff:** Raw/summary CSVs, reproducible plotting commands, held-out tables/figures, and failure examples. Steps 10–11 add external and scale checks.

**Code / reference:** `src/verify_repair/experiments.py`; `scripts/plot_experiment.py`; `docs/evaluation_protocol.md`.

## Step 10 — Implement and evaluate the LongMemEval subset

Check answer quality on realistic histories using the completed maintenance policies.

- [ ] **10.1** Select knowledge-update/temporal examples with a clear source-session → correction → later-question mapping. Save dataset version/license, inclusion rules, subset IDs, and exclusions.
- [ ] **10.2** Build the minimal adapter for immutable source evidence, derived summaries, timestamps, provenance, and query replay. Human-check temporal truth and correction mappings.
- [ ] **10.3** Run policies with the same frozen local reader, prompts, histories, retrieval/context limits, and official benchmark scoring. Include a source-evidence/RAG or full-history quality reference under stated resource limits.
- [ ] **10.4** Report answer quality, supported cost/latency measurements, uncertainty, and reviewed failures separately from synthetic results.
- [ ] **10.5** Use only observed reuse for economic comparisons. If the subset does not support a valid reuse/oracle mapping, report it as an answer-quality check and document that limitation.

**Handoff:** A runnable benchmark adapter, fixed subset manifest, official-metric results, and an error-analysis table. Step 11 completes the scale evaluation.

**Code / reference:** `docs/midterm/midterm_report.tex`; `docs/research-grade-memory-handoff.md`.

## Step 11 — Run and profile 100K cost replay

Show how the implemented system scales using measured-cost profiles.

- [ ] **11.1** Generate and validate 100K saved workloads using the final generator. Pilot runtime and memory on the RTX machine or Zaratan before starting the full selected grid.
- [ ] **11.2** Run the selected paired policies/seeds with the saved measured-cost profiles, shared B/window settings, and the appropriate exact or labeled heuristic reference.
- [ ] **11.3** Measure simulator runtime/throughput, peak RAM/storage/index size, backlog, and bottlenecks; compare with the corresponding 10K settings.
- [ ] **11.4** Save raw results, configurations, checksums, hardware details, and exact commands outside Git for large artifacts. Label the results as cost replay rather than 100K LLM-in-the-loop measurements.

**Handoff:** 100K results, a 10K-versus-100K scaling table/figure, and a bottleneck analysis. Step 12 combines all results into the paper draft.

**Code / reference:** `docs/midterm/midterm_report.tex`; `README.md`.

## Step 12 — Finish the analysis and write the paper draft

Turn the completed studies into a defensible final report. Target: December 6, 2026.

- [ ] **12.1** Regenerate final tables/figures from frozen synthetic, LLM, benchmark, and scaling outputs. Check units, denominators, uncertainty, oracle assumptions, and background versus query-visible latency.
- [ ] **12.2** Evaluate H1–H3 against the evidence, including the already-observed failure of strict per-workload H1 and any cases where P3 loses to simpler policies.
- [ ] **12.3** Write methods, budget/action semantics, causal feature/training protocol, data splits, measured costs, results, ablations, failures, limitations, and reproducibility details.
- [ ] **12.4** Verify related-work citations, mathematics, novelty statements, and every numerical claim. Separate nominal development results, held-out replay, and measured LLM/benchmark evidence.
- [ ] **12.5** Record individual contributions, deviations from the proposal/midterm, benchmark exclusions, fallbacks, and unimplemented stretch work. Obtain a teammate's review of the complete draft.

**Handoff:** A reviewed paper draft with reproducible figures and evidence for each main claim. Step 13 completes the deliverables and submissions.

**Code / reference:** `docs/final-report.md`; `docs/related-works.md`; `docs/midterm/references.bib`.

## Step 13 — Finalize the artifact, presentation, and submissions

Complete the project. Target: December 11, 2026.

- [ ] **13.1** Apply final review fixes, rerun affected checks, and update README/timeline with actual behavior, finished artifact links, limitations, and one command per main result.
- [ ] **13.2** Prepare the final release revision and package: reproducible environments, configurations, prompts/model/predictor IDs, data/cost manifests, result locations, plotting commands, and getting-started demo. Have a teammate reproduce a principal result.
- [ ] **13.3** Build slides covering motivation, approach, strongest results, budget/shift behavior, limitations, failures, and team contributions. Rehearse to the allotted time and check the presentation rubric.
- [ ] **13.4** Compile and visually inspect the final report/slides, meet course formatting/submission requirements, and submit both. Save final files and submission receipts/links.
- [ ] **13.5** Confirm audit findings are resolved, final code/results are accessible, required deliverables are submitted, and contributions are recorded. Record any agreed scope omissions explicitly in the report.

**Handoff:** Submitted final report and presentation, submitted artifact audit, and a reviewed reproducibility package that another teammate can run.

**Code / reference:** `docs/final-report.md`; `docs/final-presentation.md`; `docs/artifact-audit.md`.
