"""Replay a fixed trace under a maintenance policy and emit stable logs."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from collections.abc import Iterator
from copy import deepcopy
from math import isfinite
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from verify_repair.db import transaction
from verify_repair.demand import DemandPrediction, predict_accesses
from verify_repair.memory.access import AccessHistoryStore
from verify_repair.maintenance.operations import MaintenanceService
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.staleness import mark_stale_from_fact_change
from verify_repair.memory.summaries import SummaryStore
from verify_repair.models import ChangeType, stamp
from verify_repair.policies.base import (
    MaintenanceAction, MaintenanceCandidate, MaintenancePolicy, StaleAccessContext,
)
from verify_repair.retrieval.base import Retriever
from verify_repair.retrieval.hybrid import HybridRetriever
from verify_repair.simulator.events import (
    CorrectionEvent,
    ExpiryEvent,
    QueryEvent,
)


@dataclass(frozen=True)
class RunConfig:
    seed: int = 12345
    dataset_id: str = "toy-v1"
    verify_cost_units: float = 1.0
    repair_cost_units: float = 5.0

    def __post_init__(self) -> None:
        for cost in (self.verify_cost_units, self.repair_cost_units):
            if not isfinite(cost) or cost < 0:
                raise ValueError("nominal costs must be finite and nonnegative")

    def digest(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass
class RunResult:
    logs: list[dict]
    queries: int
    corrections: int
    verifications: int
    repairs: int
    cumulative_verify_cost: float
    cumulative_repair_cost: float
    stale_source_uses: int
    # Wall-clock milliseconds per query, in trace order; empty unless measured.
    query_latency_ms: list[float] = field(default_factory=list)


class Simulator:
    def __init__(
        self,
        ledger: FactLedger,
        summaries: SummaryStore,
        retriever: Retriever,
        policy: MaintenancePolicy,
        config: RunConfig = RunConfig(),
        *,
        measure_latency: bool = False,
    ):
        """Set ``measure_latency`` to time each query from retrieval through
        query-time maintenance. It excludes correction-time and scheduled
        repairs (background work). Timing is the only non-deterministic log
        field, so it stays off unless requested.
        """
        self.ledger = ledger
        self.summaries = summaries
        self.retriever = retriever
        self.policy = policy
        self.config = config
        self.measure_latency = measure_latency
        self.prediction_config = getattr(policy, "prediction_config", None)
        self.access_history = (
            AccessHistoryStore(summaries.db, self.prediction_config.half_life_seconds)
            if self.prediction_config is not None else None
        )

        semantic = (
            retriever.semantic_backend
            if isinstance(retriever, HybridRetriever)
            else None
        )

        def rebuild_semantic():
            if semantic is not None:
                semantic.rebuild(
                    {
                        row["summary_id"]: row["text"]
                        for row in summaries.db.execute(
                            """
                            SELECT summary_id, text
                            FROM summaries
                            ORDER BY summary_id
                            """
                        )
                    }
                )

        self._rebuild_semantic = rebuild_semantic if semantic is not None else None
        if semantic is not None:
            rebuild_semantic()

        self.maintenance = MaintenanceService(
            ledger,
            summaries,
            verify_cost_units=config.verify_cost_units,
            repair_cost_units=config.repair_cost_units,
            semantic_index_update=(
                semantic.index_summary if semantic else None
            ),
            semantic_index_rebuild=(
                rebuild_semantic if semantic else None
            ),
        )

    @contextmanager
    def _track_access(self, summary_id: str, at_time: datetime) -> Iterator[DemandPrediction | None]:
        """Supply a prior-only forecast and commit one observation with maintenance."""
        if self.access_history is None:
            yield None
            return
        try:
            with transaction(self.summaries.db):
                prediction = predict_accesses(
                    self.access_history.get(summary_id), at_time, self.prediction_config,
                )
                self.access_history.observe(summary_id, at_time)
                yield prediction
        except BaseException:
            # Also recover vectors if the outer access transaction fails after
            # the maintenance service's nested transaction has succeeded.
            if self._rebuild_semantic is not None:
                self._rebuild_semantic()
            raise

    def run(
        self,
        events: list[CorrectionEvent | QueryEvent],
    ) -> RunResult:
        expiries = [
            ExpiryEvent(
                f"{event.event_id}:expiry",
                event.valid_to,
                -1,
                event.fact_id,
                event.event_id,
            )
            for event in events
            if isinstance(event, CorrectionEvent)
            and event.change_type == ChangeType.TEMPORARY_EXCEPTION
            and event.valid_to
        ]

        ordered = sorted(
            [*events, *expiries],
            key=lambda event: (
                stamp(event.timestamp),
                event.sequence_number,
            ),
        )

        keys = [
            (stamp(event.timestamp), event.sequence_number)
            for event in ordered
        ]

        event_ids = {event.event_id for event in ordered}

        if len(keys) != len(set(keys)) or len(event_ids) != len(ordered):
            raise ValueError(
                "event order keys and event IDs must be unique"
            )

        result = RunResult(
            logs=[],
            queries=0,
            corrections=0,
            verifications=0,
            repairs=0,
            cumulative_verify_cost=0.0,
            cumulative_repair_cost=0.0,
            stale_source_uses=0,
        )

        trace_data = json.dumps(
            [asdict(event) for event in ordered],
            sort_keys=True,
            default=str,
        ).encode()
        trace_hash = hashlib.sha256(trace_data).hexdigest()

        config_snapshot = asdict(self.config)
        if self.prediction_config is not None:
            config_snapshot["prediction"] = asdict(self.prediction_config)
        config_hash = hashlib.sha256(json.dumps(config_snapshot, sort_keys=True).encode()).hexdigest()

        run_data = (
            config_hash
            + self.policy.name
            + trace_hash
        ).encode()
        run_id = hashlib.sha256(run_data).hexdigest()[:16]

        def log(
            event,
            summary_id=None,
            stale_before=None,
            episode=None,
            action=MaintenanceAction.NO_OP,
            verify_cost=0.0,
            repair_cost=0.0,
            used_stale=False,
            required_current=None,
            evidence=None,
            maintenance_trigger=None,
            maintenance_tick=None,
        ):
            if isinstance(event, QueryEvent):
                event_type = "query"
            elif isinstance(event, ExpiryEvent):
                event_type = "expiry"
            else:
                event_type = "correction"

            result.logs.append(
                {
                    "run_id": run_id,
                    "seed": self.config.seed,
                    "policy_name": self.policy.name,
                    "dataset_id": self.config.dataset_id,
                    "event_id": event.event_id,
                    "event_timestamp": stamp(event.timestamp),
                    "event_type": event_type,
                    "query_id": (
                        event.query_id
                        if isinstance(event, QueryEvent)
                        else None
                    ),
                    "summary_id": summary_id,
                    "stale_before": stale_before,
                    "stale_episode_id": episode,
                    "action": action.value,
                    "maintenance_trigger": maintenance_trigger,
                    "maintenance_tick": maintenance_tick,
                    "verify_cost_units": verify_cost,
                    "repair_cost_units": repair_cost,
                    "cumulative_verify_cost": (
                        result.cumulative_verify_cost
                    ),
                    "cumulative_repair_cost": (
                        result.cumulative_repair_cost
                    ),
                    "used_stale_source": used_stale,
                    "required_fact_current": required_current,
                    "current_evidence": evidence,
                    "latency_ms_instrumentation_only": None,
                    "config_hash": config_hash,
                    "config_snapshot": deepcopy(config_snapshot),
                    "trace_hash": trace_hash,
                }
            )

        processed_event_count = 0

        for event in ordered:
            if isinstance(event, (CorrectionEvent, ExpiryEvent)):
                if isinstance(event, ExpiryEvent):
                    history = self.ledger.get_history(event.fact_id)

                    before = self.ledger.get_current_record(
                        history[0].entity,
                        history[0].attribute,
                        event.timestamp - timedelta(microseconds=1),
                    )

                    if (
                        before is None
                        or before.event_id != event.temporary_event_id
                    ):
                        continue

                    with transaction(self.ledger.db):
                        affected = mark_stale_from_fact_change(
                            self.summaries,
                            event.fact_id,
                            event.event_id,
                            event.timestamp,
                        )
                else:
                    with transaction(self.ledger.db):
                        if event.change_type == ChangeType.CORRECTION:
                            self.ledger.append_correction(
                                event.fact_id,
                                event.value,
                                event.timestamp,
                                event.event_id,
                                event.sequence_number,
                            )
                        elif event.change_type == ChangeType.UPDATE:
                            self.ledger.append_update(
                                event.fact_id,
                                event.value,
                                event.timestamp,
                                event.event_id,
                                event.sequence_number,
                            )
                        elif event.change_type == ChangeType.RETRACTION:
                            self.ledger.append_retraction(
                                event.fact_id,
                                event.timestamp,
                                event.event_id,
                                event.sequence_number,
                            )
                        elif (
                            event.change_type
                            == ChangeType.TEMPORARY_EXCEPTION
                        ):
                            self.ledger.append_temporary_exception(
                                event.fact_id,
                                event.value,
                                event.timestamp,
                                event.valid_to,
                                event.event_id,
                                event.sequence_number,
                            )
                        else:
                            raise ValueError(
                                f"unsupported change {event.change_type}"
                            )

                        affected = mark_stale_from_fact_change(
                            self.summaries,
                            event.fact_id,
                            event.event_id,
                            event.timestamp,
                        )

                result.corrections += 1
                log(event)

                actions = self.policy.on_correction(affected)

                targeted_ids = {
                    summary_id
                    for summary_id, _ in actions
                }

                if targeted_ids - set(affected):
                    raise ValueError(
                        "policy targeted unaffected summary"
                    )

                for summary_id, action in actions:
                    stale = self.summaries.get_summary(summary_id)

                    if action != MaintenanceAction.REPAIR:
                        raise ValueError(
                            "correction hook may only schedule REPAIR"
                        )

                    self.maintenance.repair(
                        summary_id,
                        event.timestamp,
                    )

                    result.repairs += 1
                    result.cumulative_repair_cost += (
                        self.config.repair_cost_units
                    )

                    log(
                        event,
                        summary_id=summary_id,
                        stale_before=stale.is_stale,
                        episode=stale.stale_episode_id,
                        action=action,
                        repair_cost=self.config.repair_cost_units,
                    )

            else:
                result.queries += 1
                first_row = len(result.logs)
                started = time.perf_counter()

                hits = self.retriever.search(
                    event.query_text,
                    event.top_k,
                )

                if not hits:
                    log(event, required_current=False)

                for hit in hits:
                    summary = self.summaries.get_summary(
                        hit.summary_id
                    )

                    action = MaintenanceAction.NO_OP
                    verify_cost = 0.0
                    repair_cost = 0.0
                    evidence = None

                    evidence_refs = self.summaries.get_source_facts(
                        hit.summary_id
                    )

                    with self._track_access(hit.summary_id, event.timestamp) as prediction:
                        if summary.is_stale:
                            action = self.policy.on_stale_access(
                                hit.summary_id,
                                event.query_text,
                                context=StaleAccessContext(
                                    verifications_this_episode=summary.queries_this_episode,
                                    verify_cost_units=self.config.verify_cost_units,
                                    repair_cost_units=self.config.repair_cost_units,
                                    demand_prediction=prediction,
                                ),
                            )

                            if action == MaintenanceAction.VERIFY:
                                verified = self.maintenance.verify(
                                    hit.summary_id,
                                    event.timestamp,
                                )

                                evidence_refs = (
                                    verified.current_source_refs
                                )
                                evidence = verified.current_evidence
                                verify_cost = (
                                    self.config.verify_cost_units
                                )

                                result.verifications += 1
                                result.cumulative_verify_cost += (
                                    verify_cost
                                )

                            elif action == MaintenanceAction.REPAIR:
                                repaired = self.maintenance.repair(
                                    hit.summary_id,
                                    event.timestamp,
                                )

                                evidence_refs = repaired.source_refs
                                repair_cost = (
                                    self.config.repair_cost_units
                                )

                                result.repairs += 1
                                result.cumulative_repair_cost += (
                                    repair_cost
                                )

                    required_current = all(
                        any(
                            ref is not None
                            and ref.fact_id == fact_id
                            and self.ledger.resolve_current_ref(
                                ref,
                                event.timestamp,
                            )
                            == ref
                            for ref in evidence_refs.values()
                        )
                        for fact_id in event.expected_fact_ids
                    )

                    used_stale = any(
                        ref is not None
                        and self.ledger.resolve_current_ref(
                            ref,
                            event.timestamp,
                        )
                        != ref
                        for ref in evidence_refs.values()
                    )

                    result.stale_source_uses += int(used_stale)

                    log(
                        event,
                        summary_id=hit.summary_id,
                        stale_before=summary.is_stale,
                        episode=summary.stale_episode_id,
                        action=action,
                        verify_cost=verify_cost,
                        repair_cost=repair_cost,
                        used_stale=used_stale,
                        required_current=required_current,
                        evidence=evidence,
                    )

                if self.measure_latency:
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    result.query_latency_ms.append(elapsed_ms)
                    for row in result.logs[first_row:]:
                        row["latency_ms_instrumentation_only"] = elapsed_ms

            processed_event_count += 1
            interval = self.policy.refresh_interval_events

            if (
                interval is not None
                and processed_event_count % interval == 0
            ):
                candidates = [
                    MaintenanceCandidate(summary_id, stale_since)
                    for summary_id, stale_since in
                    self.summaries.list_stale_maintenance_candidates()
                ]
                stale_ids = {candidate.summary_id for candidate in candidates}

                actions = self.policy.on_maintenance_tick(
                    candidates
                )

                action_ids = [
                    summary_id
                    for summary_id, _ in actions
                ]

                if len(action_ids) != len(set(action_ids)):
                    raise ValueError(
                        "maintenance tick targeted a summary twice"
                    )

                if set(action_ids) - set(stale_ids):
                    raise ValueError(
                        "maintenance tick targeted a fresh summary"
                    )

                if any(action != MaintenanceAction.REPAIR for _, action in actions):
                    raise ValueError("maintenance tick may only schedule REPAIR")

                for summary_id, action in actions:
                    stale = self.summaries.get_summary(summary_id)

                    self.maintenance.repair(
                        summary_id,
                        event.timestamp,
                    )

                    result.repairs += 1
                    result.cumulative_repair_cost += (
                        self.config.repair_cost_units
                    )

                    log(
                        event,
                        summary_id=summary_id,
                        stale_before=stale.is_stale,
                        episode=stale.stale_episode_id,
                        action=action,
                        repair_cost=self.config.repair_cost_units,
                        maintenance_trigger="scheduled_tick",
                        maintenance_tick=processed_event_count,
                    )

        return result
