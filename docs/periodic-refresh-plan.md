# Periodic Refresh Baseline: Implementation Plan and Future Work

## Purpose

This document specifies a deterministic **Periodic Refresh** baseline for the
verify-versus-repair experiment. It is a plan, not an implementation.

The first version uses a refresh interval of **100 processed simulator events**.
A simulator event is a correction, a temporary-exception expiry, or a query.
When a query reaches a stale summary before the next refresh, the policy verifies
current source facts for that query. At events 100, 200, 300, and so on, the
simulator repairs every summary that is still stale.

This baseline answers a simple comparison question:

> Is fixed-schedule maintenance cheaper or more expensive than deciding when to
> repair each stale summary based on access?

## Current memory representation

The repository models external agent memory in three main layers.

1. **Immutable fact ledger**
   - `fact_versions` stores every version of each logical fact.
   - A correction, update, retraction, or temporary exception appends a new row.
   - Old fact rows are never updated or deleted.
   - The ledger is the authoritative source of truth.

2. **Derived summary store**
   - `summaries` stores short derived text used for retrieval.
   - A summary may be fresh or stale.
   - `summary_slots` records which logical facts fill the summary template.
   - `summary_fact_sources` records the exact fact versions used to build the
     current summary.
   - `summary_fts` is the keyword-search index.

3. **Dependency and invalidation metadata**
   - `summary_fact_sources` also acts as a reverse lookup from a changed fact
     to directly affected summaries.
   - `summary_dependencies` records summary-to-summary dependencies.
   - `stale_events` prevents one correction from resetting the same stale
     episode more than once.
   - `is_stale`, `stale_since`, and `stale_episode_id` store the current
     invalidation state.

This is a controlled research representation, not a complete production-agent
memory. Real systems may store raw conversation turns, documents, extracted
facts, vectors, graph nodes, caches, tool results, and model-written summaries.
The current design intentionally keeps those details fixed so policy comparisons
remain understandable.

## Why invalidation does not scan the whole database

When a fact changes, the system does not inspect every summary. It performs a
reverse-index lookup:

```text
changed fact
    -> directly dependent summaries
    -> their dependent summaries
```

The existing flow is:

```python
affected = set(store.get_direct_summaries_for_fact(changed_fact_id))

for summary_id in list(affected):
    affected.update(store.descendants(summary_id))

for summary_id in sorted(affected):
    mark_summary_stale(summary_id)
```

There is still unavoidable work proportional to the correction's **fan-out**:
if one fact truly affects 10,000 summaries, all 10,000 must eventually be
identified or protected. The reverse index avoids work proportional to the
entire store when only a small region is affected.

Periodic Refresh does not remove invalidation work. It postpones expensive
summary rebuilding. Marking a database row stale is expected to be much cheaper
than regenerating text, recomputing embeddings, and updating indexes.

## Version 1 behavior

### Fixed choices

- Refresh interval: 100 processed events.
- Events counted: corrections, effective expiries, and queries.
- Tick placement: process the event first, then run maintenance if
  `event_count % 100 == 0`.
- Before a refresh: verify a stale summary when queried.
- At a refresh: repair every currently stale summary.
- Fresh summaries: no action.
- Ordering: repair stale summaries in sorted `summary_id` order.
- Costs: use the existing nominal verify and repair costs.
- Budget: unlimited in Version 1.
- The policy only chooses actions; services continue to own storage mutations.

### Boundary convention

Processing the event before the tick means a correction at event 100 is eligible
for repair during the event-100 refresh. This convention must be frozen in tests
because "before event 100" and "after event 100" produce different results.

## Repository changes

### 1. Extend run configuration

File: `src/verify_repair/simulator/engine.py`

Add a refresh interval to `RunConfig`. `None` disables scheduled maintenance
for policies that do not use it.

```python
@dataclass(frozen=True)
class RunConfig:
    seed: int = 12345
    dataset_id: str = "toy-v1"
    verify_cost_units: float = 1.0
    repair_cost_units: float = 5.0
    refresh_interval_events: int | None = None
```

Validation:

```python
if refresh_interval_events is not None and refresh_interval_events <= 0:
    raise ValueError("refresh interval must be positive")
```

### 2. Extend the policy interface

File: `src/verify_repair/policies/base.py`

Add an optional scheduled-maintenance hook. The hook returns decisions and does
not perform database writes.

Recommended protocol:

```python
class MaintenancePolicy(Protocol):
    name: str

    def on_correction(
        self,
        affected_summary_ids: list[str],
    ) -> list[tuple[str, MaintenanceAction]]:
        ...

    def on_stale_access(
        self,
        summary_id: str,
        query: str,
    ) -> MaintenanceAction:
        ...

    def on_maintenance_tick(
        self,
        stale_summary_ids: list[str],
    ) -> list[tuple[str, MaintenanceAction]]:
        ...
```

Existing policies can share a default mixin or implement:

```python
def on_maintenance_tick(self, stale_summary_ids):
    return []
```

A helper function such as `scheduled_actions(policy, stale_ids)` is another
option if changing every class immediately would add noise. Whichever design is
chosen should avoid runtime `hasattr` checks in the final code.

### 3. Add a deterministic stale-summary query

File: `src/verify_repair/memory/summaries.py`

```python
def list_stale_summary_ids(self) -> list[str]:
    rows = self.db.execute(
        """
        SELECT summary_id
        FROM summaries
        WHERE is_stale = 1
        ORDER BY summary_id
        """
    )
    return [row["summary_id"] for row in rows]
```

This reads the existing stale flags. It does not rediscover dependencies or scan
fact text. For scale experiments, add an SQLite index:

```sql
CREATE INDEX IF NOT EXISTS summaries_stale
ON summaries(is_stale, summary_id);
```

### 4. Add the Periodic Refresh policy

New file: `src/verify_repair/policies/periodic_refresh.py`

```python
"""Verify stale accesses, then repair remaining stale summaries on each tick."""

from verify_repair.policies.base import MaintenanceAction


class PeriodicRefresh:
    name = "periodic-refresh"

    def on_correction(self, affected_summary_ids):
        # Invalidation already happened. Do not rebuild summaries now.
        return []

    def on_stale_access(self, summary_id, query):
        # Keep the current answer correct while waiting for the next refresh.
        return MaintenanceAction.VERIFY

    def on_maintenance_tick(self, stale_summary_ids):
        return [
            (summary_id, MaintenanceAction.REPAIR)
            for summary_id in sorted(stale_summary_ids)
        ]
```

### 5. Add event counting and tick execution

File: `src/verify_repair/simulator/engine.py`

The simulator, not the policy, owns the event counter because the simulator
already defines the ordered trace.

High-level loop:

```python
processed_event_count = 0

for event in ordered_events:
    process_normal_event(event)
    processed_event_count += 1

    interval = config.refresh_interval_events
    should_tick = (
        interval is not None
        and processed_event_count % interval == 0
    )

    if should_tick:
        stale_ids = summaries.list_stale_summary_ids()
        actions = policy.on_maintenance_tick(stale_ids)

        validate_tick_actions(actions, stale_ids)

        for summary_id, action in actions:
            maintenance.repair(summary_id, event.timestamp)
            record_repair_cost()
            log_scheduled_repair(
                trigger_event=event,
                summary_id=summary_id,
                event_count=processed_event_count,
            )
```

Validation should reject:

- IDs that were not in the stale set supplied to the policy.
- Actions other than `REPAIR`.
- Duplicate targets in one tick.
- Attempts to repair a summary that is already fresh.

### 6. Make logs distinguish scheduled repairs

Files: `src/verify_repair/simulator/engine.py` and possibly the log schema.

Add fields such as:

```python
maintenance_trigger = "periodic_tick"
maintenance_tick = processed_event_count
refresh_interval_events = 100
```

Do not invent a fake correction or query event. The log may keep the triggering
event ID while explicitly marking the maintenance trigger.

### 7. Add configuration

File: `configs/toy.yaml`

```yaml
maintenance:
  refresh_interval_events: 100
```

Because the current toy trace is shorter than 100 events, add a CLI override for
demonstration and tests:

```bash
python -m verify_repair.cli run-toy \
  --policy periodic-refresh \
  --refresh-interval-events 5
```

The research default remains 100.

### 8. Add CLI support

File: `src/verify_repair/cli.py`

Add `periodic-refresh` to the policy choices and construct it through the
existing policy mapping.

```python
policies = {
    "verify-only": VerifyOnly,
    "eager-refresh": EagerRefresh,
    "repair-on-first-access": RepairOnFirstAccess,
    "periodic-refresh": PeriodicRefresh,
}
```

Add:

```python
run.add_argument(
    "--refresh-interval-events",
    type=int,
    default=None,
)
```

Reject the interval when it is zero or negative. Decide whether supplying the
flag with a non-periodic policy is an error or is ignored; an error is clearer.

### 9. Add tests

#### Policy unit test

- A correction returns no immediate actions.
- A stale access returns `VERIFY`.
- A maintenance tick returns one `REPAIR` per stale ID in stable order.

#### Store test

- `list_stale_summary_ids()` returns only stale summaries.
- Results are sorted.
- A repaired summary disappears from the result.

#### Simulator boundary test

With interval 5:

1. A correction before event 5 marks a summary stale.
2. A query before event 5 verifies and does not mutate the summary.
3. Event 5 is processed.
4. The tick repairs the summary.
5. A later query uses the fresh summary with `NO_OP`.

#### Event-100 convention test

- A correction at event 100 is repaired by the event-100 tick.
- A correction at event 101 waits until event 200 unless accessed earlier.
- A query at event 100 is processed before the refresh.

#### Correctness tests

- Only stale summaries are repaired.
- A fresh summary is never rebuilt.
- Multiple stale summaries are repaired in stable order.
- A repair failure preserves the existing transaction and index invariants.
- The same trace, configuration, and seed produce identical non-timing logs.
- `stale_source_uses == 0` for the safe baseline.
- Temporary-exception expiry participates in event counting only when the expiry
  actually changes the effective value.

#### Regression checks

```bash
pytest -q
python -m verify_repair.cli run-toy --policy verify-only
python -m verify_repair.cli run-toy --policy eager-refresh
python -m verify_repair.cli run-toy \
  --policy repair-on-first-access
python -m verify_repair.cli run-toy \
  --policy periodic-refresh \
  --refresh-interval-events 5
```

If optional semantic dependencies are available:

```bash
RUN_SEMANTIC=1 pytest -o addopts='' -m semantic
python -m verify_repair.cli run-toy \
  --policy periodic-refresh \
  --refresh-interval-events 5 \
  --retrieval hybrid
```

## Acceptance checklist for Version 1

- [ ] Repair-on-First-Access has been merged or the implementation branch is
      based on the correct feature branch.
- [ ] Refresh interval is explicit in configuration and the run digest.
- [ ] The default research interval is 100 processed events.
- [ ] The event-before-tick boundary convention is documented and tested.
- [ ] Corrections mark affected summaries stale but do not immediately repair.
- [ ] Stale queries before the tick use VERIFY.
- [ ] Every stale summary remaining at the tick is repaired exactly once.
- [ ] Fresh summaries are not repaired.
- [ ] Scheduled repairs use the existing `MaintenanceService`.
- [ ] Scheduled repairs update provenance and retrieval indexes.
- [ ] Logs identify scheduled repairs separately.
- [ ] Nominal verification and repair costs are counted correctly.
- [ ] Core tests require no network or model download.
- [ ] Deterministic replay still holds.
- [ ] README includes the exact run command.
- [ ] A teammate reviews the implementation PR.

## Week 9 experiment checklist

- [ ] Generate identical traces for every policy.
- [ ] Include at least cold, warm, and hot reuse groups.
- [ ] Run VerifyOnly.
- [ ] Run EagerRefresh.
- [ ] Run RepairOnFirstAccess.
- [ ] Run PeriodicRefresh with interval 100.
- [ ] Run the P1 accumulated-threshold policy.
- [ ] Run the P2 predicted-threshold policy.
- [ ] Save structured logs and processed CSV files.
- [ ] Report total nominal maintenance cost.
- [ ] Report verification and repair counts.
- [ ] Report stale-source-use rate.
- [ ] Add measured latency only after instrumentation is validated.
- [ ] Report p50, p95, and p99 latency separately.
- [ ] Use several seeds and paired traces.
- [ ] Plot cost by policy and reuse group.
- [ ] Plot latency by policy.
- [ ] Document failures and unexpected behavior.

## Down-the-line adjustments and improvements

These are not required for the first Periodic Refresh baseline.

### Schedule sensitivity

- [ ] Sweep intervals such as 10, 50, 100, 500, and 1,000 events.
- [ ] Compare event-count scheduling with simulated-time scheduling.
- [ ] Compare global ticks with per-summary deadlines.
- [ ] Randomize the initial tick offset to avoid favorable trace alignment.
- [ ] Test correction bursts immediately before and after a tick.
- [ ] Test query bursts immediately before and after a tick.

### Bounded maintenance

- [ ] Limit the number of repairs per tick.
- [ ] Add a repair-cost budget per tick or per simulated day.
- [ ] Carry unfinished repairs into a persistent queue.
- [ ] Define whether unused budget rolls over.
- [ ] Log skipped and deferred repairs.
- [ ] Compare oldest-first, hottest-first, cheapest-first, and predicted-value
      queue orderings.
- [ ] Add a budget-aware oracle.

### Scalable dirty-state storage

- [ ] Add and benchmark the `summaries(is_stale, summary_id)` index.
- [ ] Replace full stale-list reads with a persistent dirty queue if needed.
- [ ] Deduplicate queue entries across repeated corrections.
- [ ] Store the newest stale episode/version on each queue item.
- [ ] Remove or supersede queue items atomically after repair.
- [ ] Page through large dirty sets instead of loading every ID into memory.
- [ ] Measure invalidation time separately from repair time.
- [ ] Measure work as a function of dependency fan-out and depth.

### Repair efficiency

- [ ] Re-embed only changed summaries.
- [ ] Replace the current full semantic-index rebuild fallback where safe.
- [ ] Cache embeddings by text hash and model version.
- [ ] Batch summary generation and embedding updates.
- [ ] Compare individual repair with correction-localized batch repair.
- [ ] Add retry and dead-letter handling for failed repairs.
- [ ] Preserve atomicity across summary, provenance, keyword index, and vector
      index updates.

### Better invalidation precision

- [ ] Distinguish "definitely stale" from "possibly affected."
- [ ] Add semantic dirty-bit checks only after exact dependency behavior is
      stable.
- [ ] Measure invalidation precision and recall.
- [ ] Avoid marking a summary stale when a changed fact cannot affect its
      rendered content.
- [ ] Handle summaries with conditional or aggregated dependencies.
- [ ] Measure false-positive repair cost.
- [ ] Preserve exact raw evidence for auditing.

### More realistic costs

- [ ] Keep nominal costs for controlled theory experiments.
- [ ] Separately measure database reads and writes.
- [ ] Measure verification-model tokens and latency.
- [ ] Measure summary-generation tokens and latency.
- [ ] Measure embedding and index-update latency.
- [ ] Separate user-visible latency from background maintenance latency.
- [ ] Record cold-cache and warm-cache results.
- [ ] Report hardware, concurrency, and model versions.

### More realistic memory

- [ ] Add immutable raw conversation turns and tool outputs.
- [ ] Add extracted events and structured facts.
- [ ] Add session and user-profile summaries.
- [ ] Add graph relationships and typed dependency edges.
- [ ] Preserve provenance from each derived item to raw evidence.
- [ ] Add confidence, scope, privacy, and access-control metadata.
- [ ] Support ambiguous corrections without immediately overwriting memory.

### Policy comparisons

- [ ] Add P1 accumulated threshold.
- [ ] Add P2 predicted threshold.
- [ ] Add P3 learned demand policy only after P1/P2 are strong.
- [ ] Add hindsight and budget-aware oracles.
- [ ] Compare periodic maintenance with query-triggered maintenance.
- [ ] Evaluate under stationary, bursty, heavy-tailed, and shifting demand.
- [ ] Report regret relative to the oracle.
- [ ] Test whether learning improves decisions, not merely predictions.

### Evaluation and reproducibility

- [ ] Scale from 100 to 1K, 10K, and 100K memories.
- [ ] Keep real-history QA separate from synthetic stress tests.
- [ ] Add held-out and distribution-shift traces.
- [ ] Use multiple seeds and paired comparisons.
- [ ] Compute bootstrap confidence intervals and effect sizes.
- [ ] Save one source CSV per figure.
- [ ] Record Git SHA, configuration hash, trace hash, model versions, hardware,
      and random seed.
- [ ] Add CI for core deterministic tests.
- [ ] Keep semantic/model tests opt-in.
- [ ] Provide one command per main table and figure.

## Open decisions before implementation

- [ ] Should the implementation extend the policy protocol directly or introduce
      a separate scheduled-maintenance protocol?
- [ ] Should all three event types count toward the interval? This plan says yes.
- [ ] Should a skipped expiry count? This plan says no because it is not an
      effective processed transition.
- [ ] Should a query with multiple retrieved summaries count once or once per
      summary? This plan says once per query event.
- [ ] Should event 100 be processed before or after maintenance? This plan says
      before.
- [ ] Should scheduled repair use the triggering event timestamp or a distinct
      deterministic tick timestamp?
- [ ] Should a repair failure stop the run or be logged and deferred?
- [ ] Should a non-periodic policy reject a configured refresh interval?

Resolve these choices in the implementation PR and freeze them with tests.
