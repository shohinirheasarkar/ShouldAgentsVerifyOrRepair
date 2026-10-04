# Pre-LLM Implementation Plan
## Should Agents Verify or Repair?

**Status:** Pre-LLM phase accepted by the project owner. Milestones 0–10 are
implemented and tested. Stop here before the next project phase (Section 19).

### Milestone status at a glance

| Milestone | Status | Evidence / remaining work |
| --- | --- | --- |
| 0 — Project bootstrap | **Complete** | Python 3.11 install, CLI help, and core tests pass. |
| 1 — Source Fact Ledger | **Complete** | Immutable rows and temporal resolution tested. |
| 2 — Derived Summary Store and Provenance | **Complete** | Exact refs, reverse lookup, and cycle rejection tested. |
| 3 — Staleness Propagation | **Complete** | Transitive propagation and episode reset tested. |
| 4 — Deterministic VERIFY and REPAIR | **Complete** | Query-local VERIFY and transactional REPAIR tested. |
| 5 — Policy Interface and Two Baselines | **Complete** | VerifyOnly and EagerRefresh pass end-to-end tests. |
| 6 — Deterministic Event Simulator | **Complete** | Replay and stable logs tested. |
| 7 — Toy Dataset and Demo | **Complete** | Both CLI demos run on the toy trace. |
| 8 — Keyword Retrieval | **Complete** | FTS5 search and repair index update tested. |
| 9 — Hybrid Retrieval and RRF | **Complete** | Pure RRF and fake-semantic hybrid tests pass. |
| 10 — Optional BGE-M3 + FAISS | **Complete** | Marked integration test and hybrid toy demo pass with the cached model. |

**Acceptance:** The project owner explicitly approved the deterministic
pre-LLM substrate. This acceptance does not constitute research findings.

**Latest checks:** `pytest -q` on Python 3.11: 23 passed, 1 optional semantic
test deselected. With semantic dependencies installed on Python 3.13, the
marked test passes and the hybrid toy demo completes.

**Primary goal:** Build and validate the fixed memory substrate and the first two maintenance baselines before adding any generative LLM calls, learned policies, hindsight oracle, repair-budget optimization, real-history benchmarks, or large-scale experiments.

This document is intended to be executable by a coding agent such as Codex. Work through the milestones in order. Do not skip acceptance gates.

---

## 1. Scope

### 1.1 In scope

Implement the following:

1. Python 3.11 project/package structure.
2. SQLite-backed append-only source fact ledger.
3. Derived summary store with explicit fact-version provenance.
4. Fact-to-summary and summary-to-summary dependency tracking.
5. Deterministic staleness propagation.
6. Deterministic pre-LLM implementations of VERIFY and REPAIR.
7. Maintenance policy interface.
8. `VerifyOnly` baseline.
9. `EagerRefresh` baseline.
10. Deterministic correction/query event simulator.
11. Reproducible toy dataset and toy event traces.
12. Structured experiment logging and nominal cost accounting.
13. Keyword retrieval.
14. Hybrid retrieval abstraction with reciprocal rank fusion.
15. Optional final pre-LLM semantic retrieval adapter using BGE-M3 + FAISS, isolated from core tests.
16. Comprehensive unit/integration tests for all of the above.
17. A small CLI/demo proving the complete deterministic path works end to end.

### 1.2 Explicitly out of scope

Do **not** implement any of the following in this phase:

- Llama, Qwen, GPT, or any other generative LLM call.
- LLM-based VERIFY.
- LLM-based REPAIR.
- Hosted-LLM synthetic text generation.
- P1 accumulated-threshold policy.
- P2 predicted-threshold policy.
- P3 learned demand-aware policy.
- XGBoost/LightGBM training.
- Hedged learned policies.
- Repair-budget scheduling/knapsack logic.
- Hindsight oracle.
- Semantic dirty-bit check based on generated answers.
- Periodic Refresh, Repair-on-First-Access, or Oldest-First unless explicitly requested after this plan is complete.
- LongMemEval or RealTime QA.
- 10K/100K/1M research-scale experiments.
- Real research conclusions about latency or LLM cost.
- Any definition of “access uncertainty” not already formally specified by the project owners.

**Project-owner clarification:** Access uncertainty means uncertainty about how
frequently future queries access a specific fact or memory component. Modeling
that uncertainty remains outside this deterministic pre-LLM phase.

The purpose of this phase is to make the state transitions, provenance, staleness logic, maintenance operations, retrieval interfaces, and simulator trustworthy before model behavior is introduced.

---

## 2. Source-of-truth behavior from the proposal

The implementation must preserve these project semantics:

1. **Source facts retain history.** New information is represented as a new version/event; historical information must remain recoverable.
2. **Derived summaries record provenance.** Every summary records the exact fact versions from which it was built.
3. **Corrections propagate staleness.** If a fact changes, every summary that depends on the affected fact must be marked stale. If summaries depend on other summaries, staleness propagates transitively.
4. **VERIFY is query-local.** It obtains the current source evidence needed for the current query but does not rewrite the stored summary and does not clear staleness.
5. **REPAIR is persistent.** It rewrites/rebuilds the summary from current facts, updates provenance and retrieval indexes, and clears the stale flag.
6. **Policies differ only in maintenance choice/timing.** The substrate must not change between policy runs.
7. **Experiments are replayable.** Fixed input events and fixed random seeds must produce identical state transitions and logs.

---

## 3. Important implementation interpretation: immutable fact ledger

The proposal uses both of the following ideas:

- the fact ledger is append-only and tests should ensure no record is edited/deleted;
- facts have validity intervals such as `valid_from` and `valid_to`.

For this phase, prioritize the stronger audit invariant: **never mutate or delete an existing fact-version row.**

### 3.1 Required representation

Represent facts as immutable version/event rows. Later rows determine when an earlier row stops being effective. Do not `UPDATE` an old fact row merely to set `valid_to`.

The public API may expose an **effective** `valid_to`, computed from later events. If an interval is known at insertion time, as with a temporary exception, it may be stored on that newly inserted row.

Add SQLite triggers that reject `UPDATE` and `DELETE` on the fact-version table. This makes the invariant mechanically testable.

### 3.2 Retractions

Represent a retraction as an immutable tombstone version/event rather than deleting the preceding fact. The current-record resolver may return the tombstone while the current-value resolver returns `None`.

### 3.3 Temporary exceptions

A temporary exception has a bounded validity interval. When it expires, the previous underlying value becomes effective again without creating an edit to that earlier row.

---

## 4. Recommended repository layout

If the repository already has an established package layout, preserve it and map these modules into the existing conventions. Do not perform a gratuitous repo-wide rename.

If there is not yet an implementation layout, use:

```text
.
├── AGENTS.md
├── IMPLEMENTATION_PLAN_PRE_LLM.md
├── README.md
├── pyproject.toml
├── configs/
│   ├── toy.yaml
│   └── toy_costs.yaml
├── data/
│   └── toy/
│       ├── facts.yaml
│       ├── summaries.yaml
│       └── events.yaml
├── src/
│   └── verify_repair/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── db.py
│       ├── models.py
│       ├── memory/
│       │   ├── __init__.py
│       │   ├── ledger.py
│       │   ├── summaries.py
│       │   ├── dependencies.py
│       │   └── staleness.py
│       ├── maintenance/
│       │   ├── __init__.py
│       │   ├── operations.py
│       │   └── summary_builder.py
│       ├── policies/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── verify_only.py
│       │   └── eager_refresh.py
│       ├── retrieval/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── keyword.py
│       │   ├── semantic.py
│       │   ├── hybrid.py
│       │   └── rrf.py
│       └── simulator/
│           ├── __init__.py
│           ├── events.py
│           ├── engine.py
│           ├── evaluator.py
│           └── logging.py
└── tests/
    ├── conftest.py
    ├── unit/
    │   ├── test_ledger.py
    │   ├── test_summaries.py
    │   ├── test_dependencies.py
    │   ├── test_staleness.py
    │   ├── test_operations.py
    │   ├── test_policies.py
    │   ├── test_rrf.py
    │   └── test_keyword_retrieval.py
    └── integration/
        ├── test_toy_end_to_end.py
        ├── test_reproducibility.py
        └── test_semantic_retrieval_optional.py
```

---

## 5. Engineering rules for this phase

1. Python version: **3.11**.
2. Core state must be stored in SQLite, not process-global dictionaries.
3. Domain objects may use `dataclasses` or a lightweight validation library, but avoid unnecessary framework complexity.
4. All timestamps must use one documented representation. Prefer UTC-aware ISO 8601 at interfaces and integer/ISO serialization in SQLite.
5. Every externally meaningful ID must be stable and deterministic in tests.
6. Do not mix policy logic into ledger, summary-store, retrieval, or simulator classes.
7. Do not mix retrieval logic into the maintenance policies.
8. Do not put LLM-specific fields in core APIs yet. Add clean interfaces that an LLM implementation can satisfy later.
9. Heavy semantic-retrieval dependencies must be optional so ordinary unit tests do not download models.
10. Every bug fix affecting an invariant must add a regression test.
11. All experiment runs must record enough configuration to replay them exactly.
12. No API keys or secrets in the repository.

---

# Milestone 0 — Project bootstrap

**Status: Complete.** Python 3.11 install, package import, CLI help, and bootstrap tests pass.

## Goal

Create a minimal, testable Python package before implementing research logic.

## Tasks

### 0.1 Project metadata

Create/update `pyproject.toml` with:

- Python `>=3.11,<3.12` unless the team intentionally supports more versions.
- package source under `src/`.
- `pytest` as a development dependency.
- `PyYAML` if YAML configs/data are used.
- optional retrieval extra for `faiss-cpu` and `FlagEmbedding`.

Do not add vLLM, transformers for generation, OpenAI SDK, or other generative-model dependencies in this phase.

### 0.2 Basic commands

The repository should support at least:

```bash
python -m pip install -e '.[dev]'
pytest -q
python -m verify_repair.cli --help
```

If the project already uses another environment manager, integrate with it rather than creating duplicate setup systems.

### 0.3 Database helper

Implement a small SQLite connection/factory layer that:

- enables foreign keys;
- supports temporary databases in tests;
- initializes schema deterministically;
- uses transactions around multi-step state transitions.

## Acceptance gate

- `pytest -q` runs successfully even if only a bootstrap smoke test exists.
- package imports work from an editable install.
- no model downloads or network access are needed.

---

# Milestone 1 — Source Fact Ledger

**Status: Complete.** Ledger immutability and temporal-resolution tests pass.

## Goal

Implement the immutable temporal fact history first. Nothing else should be built until this API is correct.

## 1.1 Domain model

Use the following logical model. Exact Python class names may differ, but semantics must match.

```python
FactVersionRef(
    fact_id: str,
    version: int,
)

FactVersion(
    fact_id: str,
    version: int,
    entity: str,
    attribute: str,
    value: str | None,
    change_type: ChangeType,
    valid_from: datetime,
    valid_to: datetime | None,
    supersedes: FactVersionRef | None,
    event_id: str,
)
```

`ChangeType` should include:

```text
INITIAL
CORRECTION
UPDATE
RETRACTION
TEMPORARY_EXCEPTION
```

### Semantics

- `(fact_id, version)` uniquely identifies a fact version.
- `fact_id` is stable across versions of the same logical fact.
- `entity + attribute` identify what the fact is about.
- versions increase monotonically for a `fact_id`.
- retractions use `value = NULL` or equivalent tombstone semantics.
- `supersedes` records the immediately preceding version when applicable.
- `event_id` groups changes that occurred as one simulator event.

## 1.2 SQLite schema

Create a table similar to:

```text
fact_versions
-------------
fact_id
version
entity
attribute
value
change_type
valid_from
valid_to
event_id
supersedes_fact_id
supersedes_version
created_at
```

Primary key:

```text
(fact_id, version)
```

Indexes should support:

- current lookup by `(entity, attribute)`;
- history lookup by `fact_id`;
- historical lookup by time;
- event audit by `event_id`.

### Immutability enforcement

Create triggers that reject:

```sql
UPDATE fact_versions ...
DELETE FROM fact_versions ...
```

These triggers are part of the research artifact, not just defensive programming.

## 1.3 Ledger API

Implement explicit methods instead of a generic “save” method:

```python
append_initial_fact(...)
append_correction(...)
append_update(...)
append_retraction(...)
append_temporary_exception(...)
get_version(FactVersionRef)
get_history(fact_id)
get_current_record(entity, attribute, at_time=None)
get_current_value(entity, attribute, at_time=None)
resolve_current_ref(FactVersionRef, at_time=None)
```

`resolve_current_ref(old_ref)` must return the version currently effective for the same logical fact, or a tombstone/none state if retracted.

## 1.4 Temporal resolution algorithm

Keep the resolver explicit and testable. Do not bury temporal semantics inside a single complex SQL query before tests exist.

Required behavior:

### Permanent correction/update

```text
t0: Alice.location = Maryland   v1
t5: Alice.location = Boston     v2, supersedes v1
```

Resolution:

```text
t <  t5 -> v1
 t >= t5 -> v2
```

### Retraction

```text
t0: deadline = Friday     v1
t5: RETRACTION            v2 tombstone
```

Resolution:

```text
t <  t5 -> Friday
 t >= t5 -> no active value
```

### Temporary exception

```text
t0: office = Room A                       v1
t5..t8: office = Room B (temporary)        v2
after t8: Room A becomes effective again
```

No mutation of v1 is allowed.

If a permanent update occurs during a temporary exception, define and test deterministic precedence. Prefer event-time ordering with the newest applicable permanent base plus any active bounded exception layered above it.

## 1.5 Required ledger tests

At minimum:

1. initial insert succeeds;
2. duplicate `(fact_id, version)` fails;
3. version gaps or non-monotonic versions fail if the API generates versions internally;
4. correction preserves old row byte-for-byte;
5. update preserves old row;
6. retraction never deletes old row;
7. temporary exception expires and old underlying value returns;
8. historical queries before and after each change return expected values;
9. SQLite `UPDATE` against `fact_versions` fails;
10. SQLite `DELETE` against `fact_versions` fails;
11. independent facts do not interfere;
12. same timestamp ordering is deterministic through a sequence/event tie-break rule.

## Acceptance gate

Do not start the summary store until all ledger tests pass.

---

# Milestone 2 — Derived Summary Store and Provenance

**Status: Complete.** Exact provenance, reverse lookup, and dependency checks pass.

## Goal

Store derived summaries and the exact fact versions used to build them.

## 2.1 Summary model

Implement a logical model similar to:

```python
SummaryRecord(
    summary_id: str,
    text: str,
    is_stale: bool,
    stale_since: datetime | None,
    stale_episode_id: str | None,
    queries_this_episode: int,
    verification_cost_this_episode: float,
    last_repair_at: datetime | None,
    created_at: datetime,
)
```

Do not encode provenance as a comma-separated JSON-ish string in the summary row. Use normalized tables.

## 2.2 Provenance tables

Use:

```text
summary_fact_sources
--------------------
summary_id
fact_id
fact_version
slot_name   # optional but useful for deterministic rebuilding
```

This table is both:

- the summary's `source_facts` list; and
- the fact-to-summary inverted index when indexed by `(fact_id, fact_version)` or logical `fact_id`.

Also create:

```text
summary_dependencies
--------------------
parent_summary_id
child_summary_id
```

This supports the proposal's requirement that staleness propagate through chains of summaries.

## 2.3 API

Implement:

```python
create_summary(text, source_facts, ...)
get_summary(summary_id)
get_source_facts(summary_id)
get_direct_summaries_for_fact(fact_id)
add_summary_dependency(parent_summary_id, child_summary_id)
get_summary_children(summary_id)
replace_summary_contents_and_sources(...)
```

`replace_summary_contents_and_sources` is allowed to mutate the **derived summary store** during REPAIR. The immutable constraint applies to the source fact ledger.

## 2.4 Integrity checks

- A summary cannot reference a nonexistent fact version.
- A summary dependency cannot point to a nonexistent summary.
- Duplicate provenance edges are rejected.
- Summary dependency cycles are rejected. Treat the summary dependency graph as a DAG for this phase.

## 2.5 Required tests

1. summary can be created with multiple fact versions;
2. provenance is returned exactly;
3. reverse lookup fact -> summaries is correct;
4. replacing summary provenance removes stale reverse links and creates new ones;
5. direct summary dependency works;
6. transitive dependency graph can be traversed;
7. cycle insertion is rejected;
8. deleting a referenced fact version is impossible because ledger deletion is impossible.

## Acceptance gate

Given a fact-version reference, the system can deterministically list every directly dependent summary.

---

# Milestone 3 — Staleness Propagation

**Status: Complete.** Direct/transitive staleness and stale-episode tests pass.

## Goal

A correction/update/retraction/temporary-exception event must mark all affected summaries stale, including transitive summary descendants.

## 3.1 Staleness service

Implement a dedicated service, for example:

```python
mark_stale_from_fact_change(
    changed_fact_id: str,
    correction_event_id: str,
    timestamp: datetime,
) -> StalenessPropagationResult
```

The service should:

1. find summaries whose provenance includes any prior version of the changed logical fact;
2. perform BFS/DFS through `summary_dependencies`;
3. mark each affected summary stale exactly once for the correction event;
4. assign/reset episode metadata for this new correction event;
5. return a deterministic ordered list/set of affected summary IDs for logging and tests.

## 3.2 Stale-episode semantics

A new relevant correction starts a new stale episode for an affected summary, even if that summary was already stale because it had not yet been repaired.

Therefore, for a new correction event affecting a summary:

```text
is_stale = true
stale_since = correction timestamp
stale_episode_id = correction/event-derived episode ID
queries_this_episode = 0
verification_cost_this_episode = 0
```

If the same correction event reaches one summary through multiple dependency paths, reset episode state only once.

## 3.3 Required tests

1. direct dependency becomes stale;
2. unrelated summary remains fresh;
3. chain A -> B -> C marks all descendants;
4. diamond dependency does not duplicate/reset twice;
5. propagation output order is deterministic;
6. repeating the same propagation event is idempotent;
7. a later correction creates a new episode and resets counters;
8. correction to any one of several source facts marks the summary stale;
9. repairing a parent does not silently clear stale descendants unless separately repaired.

## Acceptance gate

The following toy operation works with no retrieval and no policy yet:

```text
create facts -> create summaries -> correct one fact -> inspect stale summaries
```

---

# Milestone 4 — Deterministic Summary Builder, VERIFY, and REPAIR

**Status: Complete.** VERIFY/REPAIR behavior and rollback tests pass.

## Goal

Implement the maintenance semantics without an LLM so all state transitions can be tested exactly.

## 4.1 Summary builder abstraction

Create an interface/protocol:

```python
class SummaryBuilder(Protocol):
    def build(self, spec, current_facts) -> str: ...
```

Implement only:

```text
DeterministicTemplateSummaryBuilder
```

Do **not** implement `LLMSummaryBuilder` yet.

A toy summary may have a rebuild specification such as:

```yaml
summary_id: alice_profile
text_template: "{name} is a {job} living in {location}."
slots:
  name: person.name
  job: person.job
  location: person.location
```

The builder resolves the current values from the ledger and formats the template deterministically.

If a source fact is retracted, use a documented deterministic missing-value behavior rather than asking a model to infer wording.

## 4.2 VERIFY operation

Create an operation result object, e.g.:

```python
VerificationResult(
    summary_id: str,
    stale_episode_id: str,
    old_source_refs: list[FactVersionRef],
    current_source_refs: list[FactVersionRef | None],
    changed_sources: list[SourceChange],
    cost_units: float,
)
```

VERIFY must:

1. read the summary's stored source fact versions;
2. resolve the current effective versions;
3. identify which source facts changed;
4. return fresh evidence for this query;
5. increment verification counters/cost accounting;
6. **not** modify summary text;
7. **not** replace provenance;
8. **not** clear `is_stale`.

VERIFY does not need to generate a natural-language answer in this phase.

## 4.3 REPAIR operation

REPAIR must execute atomically:

1. read the summary's logical source slots/dependencies;
2. resolve current effective fact versions;
3. rebuild text with `DeterministicTemplateSummaryBuilder`;
4. replace `summary_fact_sources` with current fact-version refs;
5. update keyword retrieval index;
6. update semantic retrieval index if that optional backend is enabled;
7. clear `is_stale`;
8. clear/reset stale-episode state as appropriate;
9. set `last_repair_at`;
10. return a structured `RepairResult`.

If any required step fails, roll back the transaction; do not leave text, provenance, and indexes inconsistent.

## 4.4 Cost accounting

Real `c_v` and `c_r` will be measured later with actual model calls. In this phase, use **nominal cost units only** to validate accounting.

Example config:

```yaml
cost_mode: nominal
verify_cost_units: 1.0
repair_cost_units: 5.0
```

Never present these values as measured research results.

## 4.5 Required tests

VERIFY:

- detects changed versions;
- returns current evidence;
- leaves text identical;
- leaves source refs identical;
- leaves `is_stale = true`;
- increments counters exactly once.

REPAIR:

- rewrites deterministic text to current facts;
- replaces old refs with current refs;
- clears stale flag;
- updates reverse provenance index;
- updates keyword index;
- rolls back completely on injected failure.

## Acceptance gate

A unit test must demonstrate:

```text
Alice lives in Maryland -> summary built -> location corrected to Boston

VERIFY:
  returns Boston as current evidence
  stored summary still says Maryland
  summary remains stale

REPAIR:
  stored summary now says Boston
  provenance points to Boston fact version
  summary is fresh
```

---

# Milestone 5 — Policy Interface and Two Baselines

**Status: Complete.** Both pure policies pass the canonical end-to-end scenario.

## Goal

Policies choose maintenance behavior without owning storage or retrieval code.

## 5.1 Policy API

Use an interface with both correction-time and query-time hooks because Eager Refresh acts on correction while Verify-Only acts on access.

For example:

```python
class MaintenancePolicy(Protocol):
    name: str

    def on_correction(self, ctx, affected_summary_ids) -> list[MaintenanceAction]: ...

    def on_stale_access(self, ctx, summary_id, query) -> MaintenanceAction: ...
```

Possible actions in this phase:

```text
NO_OP
VERIFY
REPAIR
```

The engine, not the policy, executes actions.

## 5.2 VerifyOnly

Behavior:

- `on_correction`: no repair.
- `on_stale_access`: VERIFY.
- fresh summary access: no maintenance.

Expected behavior:

- total repairs = 0;
- repeated queries to a stale summary repeatedly pay VERIFY;
- the summary remains stale until another mechanism repairs it.

## 5.3 EagerRefresh

Behavior:

- after each correction and staleness propagation, REPAIR every affected summary immediately;
- query-time accesses generally see fresh summaries and require no maintenance.

Expected behavior:

- can repair summaries that are never queried again;
- verification count should be zero in ordinary deterministic traces unless a repair fails.

## 5.4 Required policy tests

1. VerifyOnly returns VERIFY for stale access;
2. VerifyOnly does nothing for fresh access;
3. EagerRefresh schedules every affected summary after correction;
4. EagerRefresh does not repair unrelated summaries;
5. the same event trace can be replayed under both policies with a clean database reset;
6. policy classes never directly mutate database tables.

## Acceptance gate

Given one correction followed by repeated queries:

```text
VerifyOnly -> repeated verification count increases
EagerRefresh -> one early repair, later accesses have no maintenance cost
```

---

# Milestone 6 — Deterministic Event Simulator

**Status: Complete.** Ordered replay and non-wall-clock log reproducibility pass.

## Goal

Replay correction/query streams in time order and record every state transition required for later research experiments.

## 6.1 Event types

Define typed events. Minimum:

```python
CorrectionEvent(...)
QueryEvent(...)
```

A `CorrectionEvent` should contain enough information to call one of the ledger append methods and should identify `change_type`.

A `QueryEvent` should include:

```text
query_id
 timestamp
 query_text
 top_k
 expected/target fact keys for deterministic ground truth, if applicable
```

For the toy phase, it is acceptable to include structural ground-truth metadata that will not exist in real benchmarks. Keep it clearly separated from data visible to a policy.

## 6.2 Ordering

Sort by:

```text
(timestamp, sequence_number)
```

Never rely on Python dictionary order or database row insertion accident for event ordering.

## 6.3 Simulator flow

### Correction event

```text
1. append immutable fact event/version
2. propagate staleness
3. call policy.on_correction(...)
4. execute returned maintenance actions
5. emit structured logs
```

### Query event

```text
1. retrieve summaries
2. for each retrieved summary:
      if fresh: use directly
      if stale: call policy.on_stale_access(...)
                execute VERIFY or REPAIR
3. compute structural correctness/provenance result
4. update episode counters
5. emit structured logs
```

Use one well-defined place to increment query counters. Avoid double counting inside both the policy and engine.

## 6.4 Structural answer correctness before LLMs

Do not fake natural-language model accuracy.

For deterministic toy queries, evaluate correctness from provenance/current fact values. Log fields such as:

```text
used_stale_source: bool
required_fact_current: bool
maintenance_action
```

If the simulator intentionally allows a stale baseline later, `used_stale_source` provides a deterministic stale-answer proxy. For VerifyOnly and EagerRefresh, the expected stale-use rate should be zero in the toy system.

## 6.5 Experiment log schema

At minimum record:

```text
run_id
seed
policy_name
event_id
event_timestamp
event_type
query_id
summary_id
stale_before
stale_episode_id
action
verify_cost_units
repair_cost_units
cumulative_verify_cost
cumulative_repair_cost
used_stale_source
latency_ms_instrumentation_only
config_hash
```

Latency in this phase is engineering instrumentation only, not a final research result.

Store logs as JSONL, CSV, or SQLite/DuckDB-friendly records. Prefer a stable schema that later experiment analysis can ingest directly.

## 6.6 Reproducibility

Every run must record:

- seed;
- config snapshot or hash;
- policy name;
- dataset/trace identifier;
- code version if easily available (e.g. git SHA, but do not fail if unavailable in unit tests).

## 6.7 Required tests

1. events replay in deterministic order;
2. two runs with same seed/config/trace produce identical state-transition logs, excluding wall-clock fields;
3. policy swap changes actions but not source event stream;
4. database resets correctly between runs;
5. counters are not shared across runs;
6. exceptions leave the database transactionally consistent.

## Acceptance gate

The whole toy trace runs under both `VerifyOnly` and `EagerRefresh` and generates deterministic logs.

---

# Milestone 7 — Toy Dataset and End-to-End Demo

**Status: Complete.** The inspectable trace and both CLI demos run successfully.

## Goal

Create a small, inspectable dataset that demonstrates the research problem before any scale or LLM complexity.

## 7.1 Recommended toy content

Use approximately:

- 20–50 logical facts;
- 10–20 summaries;
- several entities;
- summaries with 1–5 source facts;
- at least one summary-to-summary dependency chain;
- at least one correction;
- one update over time;
- one retraction;
- one temporary exception;
- repeated queries to both hot and cold stale summaries.

Use deterministic human-readable templates, not generated text.

## 7.2 Mandatory canonical scenario

Include this or an equivalent scenario in fixtures and documentation:

```text
Fact v1:
  Alice.location = Maryland

Summary S1:
  "Alice is a researcher living in Maryland."
  source_facts includes Alice.location v1

Correction:
  Alice.location = Boston (v2)

Expected:
  S1 is marked stale
```

Then prove:

### VerifyOnly

```text
Query current location
-> retrieve S1
-> S1 is stale
-> VERIFY resolves Boston
-> stored S1 still says Maryland
-> S1 remains stale
```

### EagerRefresh

```text
Correction arrives
-> S1 becomes stale
-> policy immediately REPAIRs S1
-> S1 now says Boston
-> provenance references v2
-> later query requires no maintenance
```

## 7.3 CLI/demo

Implement a simple command, for example:

```bash
python -m verify_repair.cli run-toy --policy verify-only
python -m verify_repair.cli run-toy --policy eager-refresh
```

Output should summarize:

```text
queries
corrections
verifications
repairs
nominal verification cost
nominal repair cost
stale-source uses
```

Add an optional verbose mode showing event-by-event transitions.

## Acceptance gate

A new contributor can clone the repo, install dependencies, run the toy demo, and understand why verify-versus-repair is a meaningful tradeoff without any model/API access.

---

# Milestone 8 — Keyword Retrieval

**Status: Complete.** FTS5 retrieval and index replacement after REPAIR pass.

## Goal

Replace hard-coded summary selection with a real retrieval interface while keeping the system deterministic and lightweight.

## 8.1 Retriever interface

```python
class Retriever(Protocol):
    def index_summary(self, summary_id: str, text: str) -> None: ...
    def remove_summary(self, summary_id: str) -> None: ...
    def search(self, query_text: str, top_k: int) -> list[RetrievalHit]: ...
```

`RetrievalHit` should include at least:

```text
summary_id
score
rank
backend
```

## 8.2 Keyword backend

Prefer SQLite FTS5/BM25 if supported cleanly in the target environment. Otherwise use a small explicit BM25 implementation/library. Do not hide fallback behavior.

Requirements:

- index all summary text;
- deterministic ranking with stable tie breaking by `summary_id`;
- REPAIR updates the indexed text in the same logical transaction boundary or via a clearly tested post-commit update mechanism;
- stale status must not silently remove a summary from retrieval.

## 8.3 Required tests

1. relevant toy summary is retrieved for canonical query;
2. deterministic tie-breaking;
3. repaired text replaces old searchable text;
4. nonexistent summary is not returned;
5. top-k respected.

## Acceptance gate

Toy simulator uses keyword retrieval rather than hard-coded summary IDs.

---

# Milestone 9 — Hybrid Retrieval Abstraction and RRF

**Status: Complete.** RRF arithmetic and hybrid ranking pass with a fake semantic backend.

## Goal

Prepare the proposal's hybrid retrieval path without making heavy model downloads part of core correctness tests.

## 9.1 Reciprocal Rank Fusion

Implement RRF in a pure deterministic function.

For ranked lists `R_i`, score document `d` as:

```text
RRF(d) = sum_i 1 / (K + rank_i(d))
```

Make `K` configurable. A default such as 60 is an implementation choice, not a proposal-derived constant.

Stable tie-break by `summary_id`.

## 9.2 HybridRetriever

Implement:

```python
HybridRetriever(keyword_backend, semantic_backend, rrf_k=..., weights=None)
```

Initially, tests should use a `FakeSemanticRetriever` with fixed rankings. This makes RRF correctness independent of embedding libraries.

## 9.3 Required tests

1. hand-calculated RRF example matches exactly;
2. item present in both lists gets combined contribution;
3. item present in one list still appears;
4. stable tie-breaking;
5. hybrid top-k is correct;
6. query same input twice -> identical output.

## Acceptance gate

Hybrid retrieval logic is fully tested without downloading BGE-M3.

---

# Milestone 10 — Optional BGE-M3 + FAISS Integration

**Status: Complete.** The marked integration test and hybrid CLI demo pass
locally with BGE-M3 and FAISS installed on Python 3.13. Core CI remains
independent of those dependencies.

## Goal

Complete the proposal's pre-generative-LLM retrieval substrate while keeping heavy dependencies optional.

This is the **last milestone in this pre-LLM plan**.

## 10.1 Dependency isolation

Add semantic retrieval dependencies under an optional extra, for example:

```bash
python -m pip install -e '.[semantic]'
```

Normal:

```bash
pytest -q
```

must not require downloading BGE-M3.

## 10.2 Semantic backend

Implement an adapter that:

1. embeds summary text with BGE-M3;
2. stores/searches vectors with FAISS;
3. maps FAISS rows back to stable `summary_id`s;
4. updates/replaces a summary vector after REPAIR;
5. returns deterministic ordering for tied scores where feasible.

Keep semantic index persistence simple. Correctness is more important than large-scale optimization in this phase.

## 10.3 Integration tests

Mark heavy tests explicitly, e.g.:

```bash
pytest -m semantic
```

Tests should confirm:

- index build works;
- query returns expected toy candidates;
- repair updates vector/index mapping;
- hybrid retrieval combines keyword + semantic results.

Do not make CI fail merely because a model cannot be downloaded in a network-restricted environment. Core CI should use fake semantic rankings.

## Acceptance gate

When semantic dependencies/model are available, the toy demo can run with hybrid retrieval. When unavailable, all core system tests still pass.

---

# 11. Database consistency and transactions

The following operations must be atomic:

### Correction processing

```text
append fact version
+ propagate staleness
+ update episode state
```

If Eager Refresh is enabled, decide whether repair actions occur in the same transaction or separate per-summary transactions. Prefer separate repair transactions after the correction/staleness transaction so one failed repair does not erase the underlying correction.

### Repair

```text
rebuild text
+ replace provenance
+ update summary state
+ update retrieval index metadata
```

If the retrieval backend cannot participate in SQLite transactions (e.g. FAISS in memory/on disk), implement explicit consistency handling and a regression test. For the toy phase, it is acceptable to rebuild the semantic index after a failed update rather than designing a distributed transaction system.

---

# 12. Error handling requirements

Define explicit domain errors, for example:

```text
FactNotFound
FactVersionNotFound
InvalidVersionTransition
SummaryNotFound
DependencyCycleError
InvalidProvenanceError
RepairBuildError
RetrievalIndexError
```

Do not silently catch exceptions and continue with partially updated state.

Simulator logs should record a failed action, then either fail the run or follow a clearly documented fallback. For this phase, prefer failing fast on invariant violations.

---

# 13. Configuration

Use a small YAML/dataclass config. Suggested fields:

```yaml
seed: 12345
database: ":memory:"
retrieval:
  backend: keyword
  top_k: 3
  rrf_k: 60
costs:
  mode: nominal
  verify: 1.0
  repair: 5.0
logging:
  format: jsonl
```

Validate configuration at startup. Record the resolved config in each run artifact.

---

# 14. Test matrix

The complete pre-LLM phase is not done until these categories are covered.

| Area | Required checks |
|---|---|
| Ledger | immutability, temporal lookup, correction, update, retraction, temporary exception |
| Provenance | exact fact-version refs, reverse lookup, replacement |
| Dependency graph | direct/transitive propagation, cycle rejection |
| Staleness | correct flags, episode reset, idempotence per event |
| VERIFY | fresh evidence, no stored-summary mutation |
| REPAIR | rebuilt text/provenance, stale cleared, rollback on failure |
| Policies | VerifyOnly and EagerRefresh behavior |
| Simulator | event ordering, clean reset, deterministic replay |
| Logs | stable schema, action/cost accounting |
| Keyword retrieval | ranking and repair-index update |
| RRF | exact hand-computed ranking |
| End-to-end | canonical Alice Maryland -> Boston scenario |
| Optional semantic | BGE-M3/FAISS adapter behind marked test |

---

# 15. Required end-to-end assertions

At least one integration test must explicitly assert all of the following.

## VerifyOnly run

After a correction and three queries to the same stale summary:

```text
repairs == 0
verifications == 3
summary.is_stale == true
stored summary still contains old value
verification evidence contains current value on every query
```

## EagerRefresh run

After the same correction and three queries:

```text
repairs == number of affected summaries repaired at correction time
verifications == 0
repaired summary contains current value
summary.is_stale == false
summary provenance references current fact version
later queries incur no maintenance action
```

Do not require a particular total cost ordering here because nominal `c_v/c_r` is configuration-dependent and not yet measured.

---

# 16. Suggested implementation/commit sequence

Codex should implement this as small reviewable increments rather than one huge patch.

### Commit/PR 1 — Bootstrap

- `pyproject.toml`
- package skeleton
- DB initialization
- pytest setup

### Commit/PR 2 — Immutable ledger

- schema
- ledger APIs
- temporal semantics
- ledger tests

### Commit/PR 3 — Summary/provenance store

- summaries
- provenance mapping
- dependency graph
- tests

### Commit/PR 4 — Staleness

- direct + transitive propagation
- episode bookkeeping
- tests

### Commit/PR 5 — Deterministic maintenance

- template builder
- VERIFY
- REPAIR
- transaction/rollback tests

### Commit/PR 6 — Policies

- policy protocol
- VerifyOnly
- EagerRefresh
- tests

### Commit/PR 7 — Simulator + toy data

- event types
- replay engine
- logging
- CLI
- end-to-end tests

### Commit/PR 8 — Keyword retrieval

- retriever protocol
- keyword index/search
- simulator integration

### Commit/PR 9 — RRF/hybrid abstraction

- RRF
- fake semantic backend
- hybrid tests

### Commit/PR 10 — Optional BGE-M3/FAISS

- optional dependencies
- semantic adapter
- marked integration tests

Each commit/PR must leave the repository passing its existing core tests.

---

# 17. Definition of done for the entire pre-LLM phase

The phase is complete only when all of the following are true:

- [x] Python 3.11 environment installs reproducibly.
- [x] Fact-version rows cannot be updated or deleted.
- [x] Current and historical fact resolution is correct for all four change types.
- [x] Summaries store exact fact-version provenance.
- [x] Fact-to-summary reverse lookup is correct.
- [x] Summary dependency cycles are rejected.
- [x] Corrections propagate staleness transitively.
- [x] New corrections start/reset the appropriate stale episode exactly once.
- [x] VERIFY returns fresh source evidence without modifying stored summary/provenance/stale state.
- [x] REPAIR deterministically rebuilds text, updates provenance/indexes, and clears staleness.
- [x] VerifyOnly works end to end.
- [x] EagerRefresh works end to end.
- [x] Same trace can be replayed under different policies.
- [x] Same seed/config produces identical non-wall-clock logs.
- [x] Toy dataset covers correction, update, retraction, and temporary exception.
- [x] Keyword retrieval works and is updated after repair.
- [x] RRF/hybrid logic is unit-tested with a fake semantic backend.
- [x] Optional BGE-M3/FAISS integration is isolated from core CI.
- [x] No generative LLM/API dependency has been introduced.
- [x] No P1/P2/P3/oracle/budget/real-benchmark work has leaked into this phase.
- [x] `pytest -q` passes.
- [x] README documents how to run the toy demo.

The semantic adapter also passed its marked live test and a hybrid toy demo
locally on Python 3.13. The project owner accepted this pre-LLM phase.

---

# 18. What Codex should report at completion

When this plan is complete, report:

1. files added/changed;
2. database schema implemented;
3. exact commands to install and test;
4. exact commands to run VerifyOnly and EagerRefresh toy demos;
5. test count and result;
6. any intentional deviations from this plan and why;
7. any unresolved ambiguity in proposal semantics;
8. whether optional BGE-M3/FAISS integration was tested locally;
9. confirmation that no generative LLM calls/dependencies were added.

Do not claim research findings from the toy nominal-cost runs.

---

# 19. Handoff point to the next project phase

Stop after this plan is complete.

The next phase should begin only after humans review the deterministic substrate. That later phase can add actual LLM-backed VERIFY/REPAIR, measure empirical `c_v` and `c_r`, and then proceed toward additional baselines and P1/P2. Those tasks are intentionally excluded here so model behavior cannot mask bugs in the memory system.
