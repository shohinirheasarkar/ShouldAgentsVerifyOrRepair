# Handoff: From Deterministic MVP to a Research-Grade Memory System

## Purpose

This document hands off the next phase of **Can Agents Learn When to Verify or Repair Memory Under Budget Constraints and Access Uncertainty?** It records what exists, what is missing, the recommended implementation order, and the evidence required before each phase is considered complete.

The goal is not to reproduce a private commercial architecture. The goal is to build a defensible external-memory experiment comparable in structure and evaluation rigor to systems such as Mem0, A-MEM, All-Mem, and LongMemEval-style pipelines while preserving this project's narrower contribution: deciding **when to verify a stale memory for one query and when to repair it permanently**.

---

## 1. Current state

The repository currently provides a deterministic pre-LLM substrate:

- immutable, versioned source facts in SQLite;
- derived summaries with exact fact-version provenance;
- summary-to-summary dependencies;
- transitive staleness propagation;
- deterministic VERIFY and REPAIR;
- VerifyOnly and EagerRefresh policies;
- a replayable correction/query simulator;
- nominal cost accounting and structured logs;
- toy YAML facts, summaries, and events;
- keyword retrieval;
- optional BGE-M3 + FAISS retrieval;
- regression and end-to-end tests.

### What it can currently test

It can test whether storage, staleness, verification, repair, and simple baseline behavior are mechanically correct under controlled events.

### What it cannot yet test

It cannot yet answer the full research question because it has no:

- RepairOnFirstAccess, threshold, or hindsight-oracle policy;
- uncertain future-access workloads;
- shared repair budget;
- regret calculation;
- held-out evaluation;
- natural-language memory extraction;
- LLM-backed verification, repair, or final answer generation;
- measured latency/token/API cost;
- realistic benchmark;
- learned demand policy;
- research-scale experiment.

### Known technical debt

- Semantic repair rebuilds and re-embeds the full vector index.
- Verify and repair costs are nominal constants, not measurements.
- Summaries are deterministic templates.
- Corrections arrive with perfect fact IDs and change labels.
- Provenance is perfect by construction.
- Toy queries closely resemble the stored text.
- SQLite is in-memory during the CLI demo.
- Documentation calls the substrate “accepted”; retain this wording only if the team has formally approved it.

---

## 2. Guiding experimental structure

Develop the system in three layers:

1. **Exact controlled simulator:** isolate policy behavior with known truth and known costs.
2. **Natural-language synthetic environment:** expose structured state through conversations while retaining hidden ground truth.
3. **External benchmark:** test whether conclusions transfer to realistic histories and questions.

Do not add the next layer until the prior layer produces a correct, reproducible result.

---

# Phase A — Freeze and verify the current substrate

- [ ] Tag or release the current deterministic MVP.
- [ ] Run `pytest -q` and record the exact result.
- [ ] Run VerifyOnly and EagerRefresh on the toy trace.
- [ ] Save their structured logs.
- [ ] Confirm the Maryland-to-Boston scenario behaves as documented.
- [ ] Confirm temporary exceptions expire correctly.
- [ ] Confirm no stale source is used after VERIFY or REPAIR.
- [ ] Create a concise known-limitations section.
- [ ] Move next-phase work to a feature branch and require PR review.

**Acceptance evidence**

- Clean installation from a fresh environment.
- Core tests pass.
- Identical inputs produce identical non-timing logs.
- One short sanity-check report lists commands and outputs.

---

# Phase B — Complete the controlled policy experiment

## B1. Policies

- [ ] Implement `RepairOnFirstAccess`.
- [ ] Implement an accumulated-verification-cost threshold policy.
- [ ] Freeze the exact switching convention.
- [ ] Implement a hindsight oracle.
- [ ] Ensure the oracle alone receives future-access information.
- [ ] Test every switching boundary.
- [ ] Confirm policies only return actions; storage remains owned by services.

Suggested threshold rule:

[
	ext{REPAIR if accumulated verification cost} + c_v geq c_r.
]

Oracle cost for a stale component with (k) future accesses:

[
C_{mathrm{oracle}} = min(kc_v,c_r).
]

## B2. Workloads

- [ ] Generate exact future-query counts (k=0,ldots,50).
- [ ] Add cold reuse: 0–2 future accesses.
- [ ] Add warm reuse: 3–10 future accesses.
- [ ] Add hot reuse: 10–50 future accesses.
- [ ] Add bursty access.
- [ ] Add repeated corrections before repair.
- [ ] Add variable dependency fan-out and depth.
- [ ] Add multiple simultaneously stale memories.
- [ ] Give every policy identical traces.

## B3. Cost sensitivity

- [ ] Sweep several (c_r/c_v) ratios.
- [ ] Allow component-specific costs.
- [ ] Vary costs by summary length and dependency count.
- [ ] Keep nominal costs clearly separated from measured costs.

## B4. Metrics and figures

- [ ] Total cost versus future access count.
- [ ] Regret versus oracle:
  [
  C_{mathrm{policy}}-C_{mathrm{oracle}}.
  ]
- [ ] Normalized regret.
- [ ] Verification and repair counts.
- [ ] Stale-source-use rate.
- [ ] Results across several seeds.
- [ ] Confidence intervals.
- [ ] Save the source CSV for every figure.

**Go/no-go gate**

Proceed only if:

- VerifyOnly wins at low reuse.
- Repair wins at high reuse.
- The crossover appears near (c_r/c_v).
- The oracle is always cheapest or tied.
- Threshold avoids the worst behavior of both extremes.
- Results are exactly reproducible.

**Required output**

- `cost_vs_reuse.csv`
- `cost_vs_reuse.png`
- `regret_vs_reuse.png`
- a short findings note.

---

# Phase C — Formalize budget constraints

The project title includes a budget, so the final system needs an explicit shared constraint.

## C1. Choose the budget unit

Decide which resource is constrained:

- [ ] monetary API cost;
- [ ] LLM tokens;
- [ ] wall-clock compute;
- [ ] repair calls;
- [ ] user-visible latency;
- [ ] storage;
- [ ] a weighted cost combination.

## C2. Choose the horizon

- [ ] per query;
- [ ] per user/session;
- [ ] per correction batch;
- [ ] per simulated day;
- [ ] per full episode.

## C3. Add competition for resources

- [ ] Multiple stale memories share one budget.
- [ ] Repairs can be deferred or denied.
- [ ] Policies log remaining budget.
- [ ] Add a budget-aware oracle.
- [ ] Compare greedy scheduling with predicted-value scheduling.
- [ ] Report accuracy at fixed budget and cost at fixed accuracy.

One possible objective is:

[
min_pi mathbb{E}[C_v+C_r+lambda_sE_{mathrm{stale}}+lambda_lL]
quad	ext{subject to}quad C_{mathrm{total}}leq B.
]

**Required output**

A written budget definition, unit tests, and a budget-versus-quality curve.

---

# Phase D — Upgrade the memory representation

## D1. Preserve raw evidence

- [ ] Store every raw conversation turn, document passage, or tool output.
- [ ] Assign immutable source IDs.
- [ ] Record timestamp, session, speaker, user/environment, and source type.
- [ ] Never delete raw evidence when derived memory changes.

## D2. Add structured memory records

Store:

- [ ] subject/entity;
- [ ] attribute/relation;
- [ ] value;
- [ ] valid-from and valid-to;
- [ ] source IDs;
- [ ] extraction confidence;
- [ ] memory type;
- [ ] active/inactive status;
- [ ] superseded version;
- [ ] scope and privacy metadata.

## D3. Add memory tiers

- [ ] raw observations;
- [ ] extracted events;
- [ ] structured facts;
- [ ] session summaries;
- [ ] user/profile summaries;
- [ ] procedural or strategy memories.

## D4. Add typed links

- [ ] `DERIVED_FROM`
- [ ] `SUPERSEDES`
- [ ] `CONTRADICTS`
- [ ] `TEMPORARILY_OVERRIDES`
- [ ] `DEPENDS_ON`
- [ ] `SAME_ENTITY_AS`
- [ ] `RELATED_TO`

**Acceptance evidence**

Given any derived claim, the system can trace it to exact raw evidence and reconstruct its version history.

---

# Phase E — Add natural-language ingestion and update detection

## E1. Memory extraction

- [ ] Define a `MemoryExtractor` interface.
- [ ] Preserve a deterministic mock for tests.
- [ ] Add an LLM-backed implementation.
- [ ] Require schema-validated JSON.
- [ ] Log model, prompt version, tokens, latency, and cost.
- [ ] Decide whether information is stable, temporary, hypothetical, quoted, sensitive, or not worth storing.

## E2. Entity and temporal resolution

- [ ] Resolve pronouns and repeated entities.
- [ ] Extract event time separately from message time.
- [ ] Represent uncertain dates.
- [ ] Link candidate memories to existing entities.

## E3. Change classification

Classify each candidate as:

- [ ] compatible new fact;
- [ ] correction;
- [ ] update;
- [ ] retraction;
- [ ] temporary exception;
- [ ] duplicate;
- [ ] ambiguous conflict;
- [ ] unrelated fact.

## E4. Safe ambiguity handling

- [ ] Add `NEEDS_CONFIRMATION`.
- [ ] Avoid permanent change at low confidence.
- [ ] Allow clarification questions.
- [ ] Preserve competing interpretations.
- [ ] Retrieve original evidence before superseding a memory.

## E5. Component evaluation

Measure:

- [ ] fact precision/recall;
- [ ] entity-resolution accuracy;
- [ ] temporal accuracy;
- [ ] change-classification accuracy;
- [ ] incorrect-supersession rate;
- [ ] false-memory creation rate;
- [ ] confidence calibration.

**Required output**

A labeled extraction/update test set and an evaluation report independent of downstream QA.

---

# Phase F — Add LLM-backed VERIFY, REPAIR, and answer generation

## F1. LLM-backed repair

- [ ] Keep the deterministic template builder.
- [ ] Add an LLM builder implementing the same interface.
- [ ] Provide the old summary, current evidence, and explicit changed fields.
- [ ] Require source citations.
- [ ] Prevent unsupported new facts.
- [ ] Preserve unrelated correct content.
- [ ] Retry or reject failed validation.
- [ ] Record prompt, model, tokens, latency, and cost.

Measure:

- [ ] update success;
- [ ] factual consistency;
- [ ] completeness;
- [ ] locality/unrelated-content preservation;
- [ ] citation correctness;
- [ ] repair failure rate;
- [ ] variability across repeated generations.

## F2. LLM-backed verification

- [ ] Retrieve current authoritative evidence.
- [ ] Build a query-specific evidence packet.
- [ ] Do not mutate the stored summary.
- [ ] Record evidence sources and costs.

## F3. Frozen reader model

- [ ] Use the same reader model for every policy.
- [ ] Freeze prompt and decoding settings.
- [ ] Generate the final natural-language answer.
- [ ] Separate evidence-retrieval correctness from answer correctness.
- [ ] Measure abstention when evidence is insufficient.

**Acceptance evidence**

A correction arrives in natural language; the system extracts it, marks affected memory stale, chooses an action, gathers current evidence, and produces a scored answer.

---

# Phase G — Make retrieval and index maintenance realistic

## G1. Persistent versioned embeddings

Store:

- [ ] summary ID and version;
- [ ] text hash;
- [ ] embedding model/version;
- [ ] vector;
- [ ] active/inactive state;
- [ ] creation timestamp.

## G2. Incremental updates

- [ ] Embed only changed text.
- [ ] Insert the new vector.
- [ ] mark the old vector inactive or replace by stable ID.
- [ ] Avoid re-embedding unchanged memories.
- [ ] Cache embeddings by text hash and model version.
- [ ] Periodically compact the index.
- [ ] Reserve full rebuild for scheduled maintenance or recovery.

## G3. Retrieval baselines

- [ ] BM25 over raw turns;
- [ ] dense retrieval;
- [ ] hybrid retrieval;
- [ ] time-aware retrieval;
- [ ] session-level retrieval;
- [ ] graph expansion;
- [ ] agent-controlled raw-log search;
- [ ] oracle evidence retrieval.

## G4. Retrieval metrics

- [ ] Recall@k
- [ ] Precision@k
- [ ] MRR
- [ ] evidence completeness
- [ ] stale-evidence retrieval rate
- [ ] retrieval latency
- [ ] index-update latency
- [ ] storage size

**Required output**

Incremental repair no longer rebuilds all embeddings. Full-rebuild and incremental backends are compared so implementation inefficiency is not mistaken for intrinsic repair cost.

---

# Phase H — Measure genuine operation costs

Instrument the complete pipeline.

## Record per operation

- [ ] database reads and writes;
- [ ] retrieval latency;
- [ ] authoritative-source latency;
- [ ] summary-generation latency;
- [ ] embedding latency;
- [ ] index-update latency;
- [ ] final answer-generation latency;
- [ ] input/output tokens;
- [ ] API dollar cost;
- [ ] affected-summary count;
- [ ] dependency depth/fan-out;
- [ ] cold/warm model and cache status.

## Report distributions

- [ ] p50
- [ ] p95
- [ ] p99
- [ ] throughput
- [ ] peak memory
- [ ] index size
- [ ] storage growth

Separate:

[
	ext{retrieval}+	ext{maintenance}+	ext{answer generation}
=	ext{total query latency}.
]

Also separate background repair latency from user-visible latency.

Model repair cost as:

[
c_r=c_{mathrm{generate}}+c_{mathrm{embed}}+
c_{mathrm{index}}+c_{mathrm{dependency}}.
]

**Required output**

Measured costs replace—or complement—nominal costs without tying the policy conclusion to one machine.

---

# Phase I — Build realistic evolving-memory workloads

## I1. Structured hidden state

Generate:

- [ ] user profiles and preferences;
- [ ] locations and employment;
- [ ] relationships;
- [ ] appointments and plans;
- [ ] temporary conditions;
- [ ] environment procedures;
- [ ] tool behavior and recurring failures.

## I2. State evolution

Include:

- [ ] corrections;
- [ ] genuine updates;
- [ ] retractions;
- [ ] temporary exceptions;
- [ ] reversions;
- [ ] conflicting sources;
- [ ] ambiguous updates;
- [ ] delayed corrections;
- [ ] corrections never queried again.

## I3. Natural-language realization

- [ ] Convert hidden states into varied multi-session conversations.
- [ ] Include paraphrases and indirect references.
- [ ] Include distractors and hypotheticals.
- [ ] Preserve hidden structured truth for exact scoring.

## I4. Demand distributions

- [ ] stationary access;
- [ ] bursty access;
- [ ] heavy-tailed popularity;
- [ ] sudden hot memories;
- [ ] topic drift;
- [ ] distribution shift;
- [ ] adversarial demand.

**Required output**

A scalable natural-language benchmark where the truth, updates, and future accesses remain exactly known.

---

# Phase J — Integrate external benchmarks

Select benchmarks only where the verify/repair mapping is defensible.

## Candidates

- [ ] **LongMemEval:** knowledge updates, temporal reasoning, multi-session reasoning, information extraction, abstention.
- [ ] **LoCoMo:** conversational single-hop, temporal, multi-hop, and open-domain QA.
- [ ] **MemoryAgentBench:** retrieval, test-time learning, long-range understanding, selective forgetting.
- [ ] **LongMemEval-V2:** dynamic environment state, workflow knowledge, and recurring failure modes.

## Adapter requirements

- [ ] Document what counts as authoritative evidence.
- [ ] Document what counts as a derived memory.
- [ ] Document how a correction is identified.
- [ ] Document future accesses.
- [ ] Document how verify and repair costs are measured.
- [ ] Reject examples without a valid mapping.
- [ ] Keep external-benchmark results separate from synthetic results.

**Required output**

At least one external benchmark demonstrates whether controlled conclusions transfer.

---

# Phase K — Add strong comparison systems

## Policy baselines

- [ ] stale-use/no maintenance;
- [ ] VerifyOnly;
- [ ] EagerRefresh;
- [ ] RepairOnFirstAccess;
- [ ] periodic refresh;
- [ ] oldest-first;
- [ ] threshold;
- [ ] hindsight oracle.

## Memory baselines

- [ ] current context only;
- [ ] full history;
- [ ] sliding window;
- [ ] periodic summaries;
- [ ] BM25 RAG;
- [ ] dense RAG;
- [ ] hybrid/time-aware RAG;
- [ ] raw-log agentic search;
- [ ] Mem0-style extracted memory;
- [ ] graph-memory system where reproducible;
- [ ] A-MEM or All-Mem where reproducible.

## Fair comparison

- [ ] Same reader model
- [ ] Same histories and questions
- [ ] Same context/evidence budget
- [ ] Same hardware and timeout
- [ ] Same maximum retrieval calls
- [ ] Same embedding model where possible

**Required output**

A baseline table showing quality, cost, and latency under matched conditions.

---

# Phase L — Learn future demand

Only begin after the simple threshold is competitive.

## Features

- [ ] access count and recency;
- [ ] recent access frequency;
- [ ] time since correction;
- [ ] correction type;
- [ ] dependency count and depth;
- [ ] summary length;
- [ ] topic and session;
- [ ] historical popularity;
- [ ] verification and repair cost;
- [ ] remaining budget.

## Models

- [ ] logistic regression;
- [ ] gradient-boosted trees;
- [ ] calibrated probabilities;
- [ ] robust fallback to threshold at low confidence.

## Protocol

- [ ] Train only on training traces.
- [ ] Tune only on development traces.
- [ ] Freeze before held-out testing.
- [ ] Evaluate under workload shift.
- [ ] Compare prediction error with downstream decision regret.
- [ ] Report whether learning materially beats the simple threshold.

**Required output**

A learned policy earns inclusion only if it improves held-out cost/quality, not merely prediction accuracy.

---

# Phase M — Evaluation and rigor

## Metrics

### Final-answer quality
- [ ] exact match/F1;
- [ ] semantic score;
- [ ] documented LLM judge;
- [ ] human-reviewed subset;
- [ ] abstention accuracy;
- [ ] citation correctness.

### Memory quality
- [ ] current-state accuracy;
- [ ] stale-memory rate;
- [ ] incorrect supersession;
- [ ] repair correctness;
- [ ] provenance completeness;
- [ ] unrelated-memory damage.

### Policy quality
- [ ] total cost;
- [ ] regret and competitive ratio;
- [ ] verify/repair/defer counts;
- [ ] budget violations;
- [ ] accuracy at fixed cost;
- [ ] cost at fixed accuracy.

## Data splits

- [ ] training;
- [ ] development;
- [ ] held-out test;
- [ ] distribution-shift test;
- [ ] no test-set tuning.

## Statistical protocol

- [ ] multiple seeds;
- [ ] paired comparisons on identical traces;
- [ ] bootstrap confidence intervals;
- [ ] effect sizes;
- [ ] per-category results;
- [ ] correction for multiple comparisons where required.

## Ablations

- [ ] no provenance;
- [ ] no dependency propagation;
- [ ] no temporal filtering;
- [ ] keyword only;
- [ ] dense only;
- [ ] no demand features;
- [ ] fixed versus measured costs;
- [ ] correction rate;
- [ ] dependency fan-out;
- [ ] budget;
- [ ] LLM and embedding backbone.

## Failure taxonomy

- [ ] extraction failure;
- [ ] entity/temporal resolution failure;
- [ ] update-classification failure;
- [ ] retrieval failure;
- [ ] verification failure;
- [ ] repair hallucination;
- [ ] reader ignored correct evidence;
- [ ] policy made the wrong economic decision;
- [ ] budget prevented repair.

**Required output**

A results package containing raw runs, processed tables, figures, uncertainty, ablations, and representative failures.

---

# Phase N — Reproducibility

- [ ] Pin dependency versions.
- [ ] Add a lockfile.
- [ ] Store experiment configurations.
- [ ] Record Git SHA, prompts, model versions, seeds, and hardware.
- [ ] Provide one command per main table/figure.
- [ ] Separate raw and processed results.
- [ ] Add schema migrations.
- [ ] Add run checkpointing and resumption.
- [ ] Add API retry/error handling.
- [ ] Prevent secrets and personal data from entering logs.
- [ ] Document dataset licenses.
- [ ] Add CI for core tests.
- [ ] Keep network/model tests opt-in.
- [ ] Add a small end-to-end smoke test.

**Acceptance evidence**

A teammate can reproduce the main result from a clean checkout without undocumented steps.

---

# Recommended semester scope

## Must have

- [ ] Verified deterministic substrate
- [ ] First-access, threshold, and oracle policies
- [ ] Controlled cold/warm/hot workloads
- [ ] Multiple cost ratios
- [ ] Explicit shared budget
- [ ] Cost and regret analysis
- [ ] Incremental semantic updates
- [ ] Raw evidence and exact provenance
- [ ] One LLM extractor
- [ ] One LLM summary builder
- [ ] One frozen answer model
- [ ] Natural-language synthetic evolving-memory workload
- [ ] One external benchmark
- [ ] Strong RAG/full-history/raw-log baselines
- [ ] Accuracy, latency, token, and dollar-cost measurements
- [ ] Held-out evaluation and confidence intervals

## Strong target

- [ ] Predicted-demand policy
- [ ] Hybrid/time-aware retrieval
- [ ] Component-specific repair costs
- [ ] Multiple stale memories sharing a budget
- [ ] Distribution-shift evaluation
- [ ] Human review of a sample
- [ ] Detailed failure analysis

## Stretch

- [ ] Full graph/topology memory
- [ ] SPLIT/MERGE/UPDATE maintenance
- [ ] RL-trained memory controller
- [ ] Multiple external benchmarks
- [ ] 100K+ memory stress test
- [ ] Formal result beyond the simplified ski-rental setting

---

# Immediate next milestone

Do not begin by building the full SOTA-like architecture. The next milestone is:

> Compare VerifyOnly, EagerRefresh, RepairOnFirstAccess, Threshold, and Oracle on identical traces with (k=0,ldots,50), several (c_r/c_v) ratios, and controlled cold/warm/hot access regimes; then produce cost and regret figures.

After the go/no-go pattern is confirmed, formalize the budget. Only then add natural-language ingestion, LLM operations, incremental semantic indexing, and external benchmarks.

---

# Suggested ownership split

Use this only as a starting point; rotate research responsibilities across later phases.

- **Policy/experiments owner:** policies, oracle, workload generator, cost/regret figures.
- **Memory/LLM owner:** raw evidence, extraction, update detection, LLM repair.
- **Retrieval/evaluation owner:** incremental embeddings, retrieval baselines, benchmark adapters, metrics.

Every major PR should have a reviewer who did not author it. No teammate should be limited to configuration work; each should contribute to implementation, experiments, interpretation, and writing.

---

# Definition of a successful final result

A successful final project demonstrates, on held-out evolving-memory workloads, that a query-triggered verify-or-repair policy maintains answer correctness while reducing total maintenance cost or user-visible latency relative to strong fixed policies. The claim must remain supported under measured costs, at least one realistic memory benchmark, appropriate baselines, uncertainty estimates, and transparent failure analysis.
