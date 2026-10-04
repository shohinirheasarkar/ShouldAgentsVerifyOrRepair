"""Replay a fixed trace under either pure policy and emit stable transition logs."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from verify_repair.db import transaction
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.summaries import SummaryStore
from verify_repair.memory.staleness import mark_stale_from_fact_change
from verify_repair.maintenance.operations import MaintenanceService
from verify_repair.models import ChangeType, stamp
from verify_repair.policies.base import MaintenanceAction, MaintenancePolicy
from verify_repair.retrieval.base import Retriever
from verify_repair.retrieval.hybrid import HybridRetriever
from verify_repair.simulator.events import CorrectionEvent, ExpiryEvent, QueryEvent


@dataclass(frozen=True)
class RunConfig:
    seed: int = 12345
    dataset_id: str = "toy-v1"
    verify_cost_units: float = 1.0
    repair_cost_units: float = 5.0

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


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


class Simulator:
    def __init__(self, ledger: FactLedger, summaries: SummaryStore, retriever: Retriever,
                 policy: MaintenancePolicy, config: RunConfig = RunConfig()):
        self.ledger, self.summaries, self.retriever = ledger, summaries, retriever
        self.policy, self.config = policy, config
        semantic = retriever.semantic_backend if isinstance(retriever, HybridRetriever) else None
        def rebuild_semantic():
            if semantic is not None:
                semantic.rebuild({row["summary_id"]: row["text"] for row in
                                  summaries.db.execute("SELECT summary_id,text FROM summaries ORDER BY summary_id")})
        if semantic is not None:
            rebuild_semantic()
        self.maintenance = MaintenanceService(ledger, summaries, verify_cost_units=config.verify_cost_units,
                                              repair_cost_units=config.repair_cost_units,
                                              semantic_index_update=semantic.index_summary if semantic else None,
                                              semantic_index_rebuild=rebuild_semantic if semantic else None)

    def run(self, events: list[CorrectionEvent | QueryEvent]) -> RunResult:
        expiries = [ExpiryEvent(f"{event.event_id}:expiry", event.valid_to, -1,
                                event.fact_id, event.event_id)
                    for event in events if isinstance(event, CorrectionEvent)
                    and event.change_type == ChangeType.TEMPORARY_EXCEPTION and event.valid_to]
        ordered = sorted([*events, *expiries], key=lambda event: (stamp(event.timestamp), event.sequence_number))
        keys = [(stamp(event.timestamp), event.sequence_number) for event in ordered]
        if len(keys) != len(set(keys)) or len({event.event_id for event in ordered}) != len(ordered):
            raise ValueError("event order keys and event IDs must be unique")
        result = RunResult([], 0, 0, 0, 0, 0.0, 0.0, 0)
        trace_hash = hashlib.sha256(json.dumps([asdict(event) for event in ordered],
                                               sort_keys=True, default=str).encode()).hexdigest()
        run_id = hashlib.sha256((self.config.digest() + self.policy.name + trace_hash).encode()).hexdigest()[:16]

        def log(event, summary_id=None, stale_before=None, episode=None,
                action=MaintenanceAction.NO_OP, verify_cost=0.0, repair_cost=0.0,
                used_stale=False, required_current=None, evidence=None):
            result.logs.append(dict(run_id=run_id, seed=self.config.seed, policy_name=self.policy.name,
              dataset_id=self.config.dataset_id, event_id=event.event_id,
              event_timestamp=stamp(event.timestamp), event_type=("query" if isinstance(event, QueryEvent)
                else "expiry" if isinstance(event, ExpiryEvent) else "correction"),
              query_id=event.query_id if isinstance(event, QueryEvent) else None,
              summary_id=summary_id, stale_before=stale_before, stale_episode_id=episode,
              action=action.value, verify_cost_units=verify_cost, repair_cost_units=repair_cost,
              cumulative_verify_cost=result.cumulative_verify_cost,
              cumulative_repair_cost=result.cumulative_repair_cost, used_stale_source=used_stale,
              required_fact_current=required_current, current_evidence=evidence,
              latency_ms_instrumentation_only=None,
              config_hash=self.config.digest(), config_snapshot=asdict(self.config),
              trace_hash=trace_hash))

        for event in ordered:
            if isinstance(event, (CorrectionEvent, ExpiryEvent)):
                if isinstance(event, ExpiryEvent):
                    history = self.ledger.get_history(event.fact_id)
                    before = self.ledger.get_current_record(history[0].entity, history[0].attribute,
                                                            event.timestamp - timedelta(microseconds=1))
                    if before is None or before.event_id != event.temporary_event_id:
                        continue  # a later permanent event already displaced the exception
                    with transaction(self.ledger.db):
                        affected = mark_stale_from_fact_change(self.summaries, event.fact_id,
                                                               event.event_id, event.timestamp)
                else:
                  with transaction(self.ledger.db):
                    if event.change_type == ChangeType.CORRECTION:
                        self.ledger.append_correction(event.fact_id, event.value, event.timestamp, event.event_id, event.sequence_number)
                    elif event.change_type == ChangeType.UPDATE:
                        self.ledger.append_update(event.fact_id, event.value, event.timestamp, event.event_id, event.sequence_number)
                    elif event.change_type == ChangeType.RETRACTION:
                        self.ledger.append_retraction(event.fact_id, event.timestamp, event.event_id, event.sequence_number)
                    elif event.change_type == ChangeType.TEMPORARY_EXCEPTION:
                        self.ledger.append_temporary_exception(event.fact_id, event.value, event.timestamp,
                                                               event.valid_to, event.event_id, event.sequence_number)
                    else:
                        raise ValueError(f"unsupported change {event.change_type}")
                    affected = mark_stale_from_fact_change(self.summaries, event.fact_id,
                                                           event.event_id, event.timestamp)
                result.corrections += 1
                log(event)
                actions = self.policy.on_correction(affected)
                if {summary_id for summary_id, _ in actions} - set(affected):
                    raise ValueError("policy targeted unaffected summary")
                for summary_id, action in actions:
                    stale = self.summaries.get_summary(summary_id)
                    if action != MaintenanceAction.REPAIR:
                        raise ValueError("correction hook may only schedule REPAIR")
                    self.maintenance.repair(summary_id, event.timestamp)
                    result.repairs += 1
                    result.cumulative_repair_cost += self.config.repair_cost_units
                    log(event, summary_id, stale.is_stale, stale.stale_episode_id, action,
                        repair_cost=self.config.repair_cost_units)
            else:
                result.queries += 1
                hits = self.retriever.search(event.query_text, event.top_k)
                if not hits:
                    log(event, required_current=False)
                for hit in hits:
                    summary = self.summaries.get_summary(hit.summary_id)
                    action = MaintenanceAction.NO_OP
                    verify_cost = repair_cost = 0.0
                    evidence = None
                    evidence_refs = self.summaries.get_source_facts(hit.summary_id)
                    if summary.is_stale:
                        action = self.policy.on_stale_access(hit.summary_id, event.query_text)
                        if action == MaintenanceAction.VERIFY:
                            verified = self.maintenance.verify(hit.summary_id, event.timestamp)
                            evidence_refs = verified.current_source_refs
                            evidence = verified.current_evidence
                            verify_cost = self.config.verify_cost_units
                            result.verifications += 1
                            result.cumulative_verify_cost += verify_cost
                        elif action == MaintenanceAction.REPAIR:
                            repaired = self.maintenance.repair(hit.summary_id, event.timestamp)
                            evidence_refs = repaired.source_refs
                            repair_cost = self.config.repair_cost_units
                            result.repairs += 1
                            result.cumulative_repair_cost += repair_cost
                    # Ground-truth IDs are evaluation metadata, never passed into a policy.
                    required_current = all(
                        any(ref is not None and ref.fact_id == fact_id and
                            self.ledger.resolve_current_ref(ref, event.timestamp) == ref
                            for ref in evidence_refs.values()) for fact_id in event.expected_fact_ids)
                    used_stale = any(ref is not None and
                        self.ledger.resolve_current_ref(ref, event.timestamp) != ref
                        for ref in evidence_refs.values())
                    result.stale_source_uses += int(used_stale)
                    log(event, hit.summary_id, summary.is_stale, summary.stale_episode_id,
                        action, verify_cost, repair_cost, used_stale, required_current, evidence)
        return result
