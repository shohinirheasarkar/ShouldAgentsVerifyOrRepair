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
from verify_repair.simulator.budget import BudgetState, RepairBudget
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
    # Budget accounting (deferrals, queue, waits); None unless a budget is set.
    budget: dict | None = None
    # Per-query generated answers; empty unless an AnswerReader is configured.
    answers: list[dict] = field(default_factory=list)


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
        budget: RepairBudget | None = None,
        summary_builder=None,
        verifier=None,
        answer_reader=None,
        llm_config=None,
        llm_runtime_info: dict | None = None,
        cost_profile=None,
    ):
        """Set ``measure_latency`` to time each query from retrieval through
        query-time maintenance (and answer generation when ``answer_reader`` is
        configured). It excludes correction-time and scheduled repairs
        (background work). Timing is the only non-deterministic log field, so
        it stays off unless requested. With timing on, each row also carries
        ``timing_ms`` (retrieval, operation components, reader) and
        ``query_visible`` (False for correction-time, tick, and queue repairs).

        ``cost_profile`` (a ``CostProfile``) switches to measured-cost replay:
        each VERIFY/REPAIR is charged a keyed draw from the profile instead of
        the fixed ``config`` costs, which policies keep seeing as estimates.
        """
        self.ledger = ledger
        self.summaries = summaries
        self.retriever = retriever
        self.policy = policy
        self.config = config
        self.measure_latency = measure_latency
        self.budget = budget
        self.verifier = verifier
        self.answer_reader = answer_reader
        self.llm_config = llm_config
        self.llm_runtime_info = dict(llm_runtime_info) if llm_runtime_info is not None else None
        self.cost_profile = cost_profile
        self._fact_counts: dict[str, int] = {}
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
            builder=summary_builder,
            verify_cost_units=config.verify_cost_units,
            repair_cost_units=config.repair_cost_units,
            semantic_index_update=(
                semantic.index_summary if semantic else None
            ),
            semantic_index_rebuild=(
                rebuild_semantic if semantic else None
            ),
            verifier=verifier,
        )

    def _fact_count(self, summary_id: str) -> int:
        if summary_id not in self._fact_counts:
            self._fact_counts[summary_id] = len(self.summaries.get_slots(summary_id))
        return self._fact_counts[summary_id]

    def _verify_cost(self, event_id: str, summary_id: str) -> float:
        if self.cost_profile is None:
            return self.config.verify_cost_units
        from verify_repair.costs import verify_key
        return self.cost_profile.draw("verify", verify_key(event_id, summary_id),
                                      self.config.seed, self._fact_count(summary_id))

    def _repair_cost(self, summary_id: str, episode_id: str | None) -> float:
        if self.cost_profile is None:
            return self.config.repair_cost_units
        from verify_repair.costs import repair_key
        return self.cost_profile.draw("repair", repair_key(summary_id, episode_id),
                                      self.config.seed, self._fact_count(summary_id))

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
        if self.budget is not None:
            config_snapshot["budget"] = self.budget.snapshot()
        if self.llm_config is not None:
            config_snapshot["llm"] = self.llm_config.snapshot()
        if self.cost_profile is not None:
            config_snapshot["cost_profile"] = self.cost_profile.snapshot()
        # Unbudgeted/deterministic runs keep their logs and hashes byte-identical.
        state = None
        if self.budget is not None:
            origin = self.budget.origin or (ordered[0].timestamp if ordered else None)
            state = BudgetState(self.budget, self.config.repair_cost_units, origin) if origin else None
        config_hash = hashlib.sha256(json.dumps(config_snapshot, sort_keys=True).encode()).hexdigest()

        run_data = (
            config_hash
            + self.policy.name
            + trace_hash
        ).encode()
        run_id = hashlib.sha256(run_data).hexdigest()[:16]

        def _merge_usage(first: dict | None, second: dict | None) -> dict | None:
            if first is None:
                return dict(second) if second is not None else None
            if second is None:
                return dict(first)
            wall_a, wall_b = first.get("wall_ms"), second.get("wall_ms")
            gpu_a, gpu_b = first.get("gpu_ms"), second.get("gpu_ms")
            return {
                "prompt_tokens": int(first.get("prompt_tokens", 0)) + int(second.get("prompt_tokens", 0)),
                "completion_tokens": int(first.get("completion_tokens", 0)) + int(second.get("completion_tokens", 0)),
                "total_tokens": int(first.get("total_tokens", 0)) + int(second.get("total_tokens", 0)),
                "wall_ms": (wall_a + wall_b) if (wall_a is not None and wall_b is not None) else None,
                "gpu_ms": (gpu_a + gpu_b) if (gpu_a is not None and gpu_b is not None) else None,
            }

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
            budget_outcome=None,
            llm_usage=None,
            llm_metadata=None,
            timing=None,
            query_visible=None,
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
            if state is not None:
                result.logs[-1].update({
                    "budget_window": state.window,
                    "budget_remaining": state.remaining,
                    "budget_outcome": budget_outcome,
                })
            if llm_usage is not None:
                result.logs[-1]["llm_usage"] = llm_usage
            if llm_metadata is not None:
                result.logs[-1]["maintenance_llm_metadata"] = llm_metadata
            if self.measure_latency and (timing is not None or query_visible is not None):
                result.logs[-1]["timing_ms"] = dict(timing or {})
                result.logs[-1]["query_visible"] = query_visible

        def execute_repair(summary_id, at_time, cost):
            """Repair now, reserving ``cost`` from the budget; None if the budget is exhausted."""
            if state is None:
                return self.maintenance.repair(summary_id, at_time)
            if not state.can_afford(cost):
                return None
            state.reserve(cost)
            try:
                repaired = self.maintenance.repair(summary_id, at_time)
            except BaseException:
                state.refund(cost)
                raise
            state.queue.pop(summary_id, None)
            return repaired

        def charged():
            return "charged" if state is not None else None

        def drain_queue(event):
            """Spend a freshly reset budget on deferred repairs, best first."""
            for entry in state.ordered(event.timestamp, self.config.verify_cost_units):
                summary = self.summaries.get_summary(entry.summary_id)
                if not summary.is_stale or summary.stale_episode_id != entry.episode_id:
                    del state.queue[entry.summary_id]  # repaired or superseded elsewhere
                    state.invalidated += 1
                    continue
                cost = self._repair_cost(entry.summary_id, summary.stale_episode_id)
                if not state.can_afford(cost):
                    break
                repaired = execute_repair(entry.summary_id, event.timestamp, cost)
                state.executed_from_queue += 1
                state.waits_seconds.append((event.timestamp - entry.enqueued_at).total_seconds())
                result.repairs += 1
                result.cumulative_repair_cost += cost
                log(
                    event,
                    summary_id=entry.summary_id,
                    stale_before=True,
                    episode=summary.stale_episode_id,
                    action=MaintenanceAction.REPAIR,
                    repair_cost=cost,
                    maintenance_trigger="deferred_queue",
                    budget_outcome="charged",
                    llm_usage=repaired.llm_metadata.get("usage") if repaired.llm_metadata else None,
                    llm_metadata=repaired.llm_metadata,
                    timing=repaired.timing_ms,
                    query_visible=False,
                )

        processed_event_count = 0

        for event in ordered:
            if state is not None and state.roll(event.timestamp):
                drain_queue(event)
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

                targeted = [summary_id for summary_id, _ in actions]

                # Validate the whole batch before any repair executes.
                if set(targeted) - set(affected):
                    raise ValueError(
                        "policy targeted unaffected summary"
                    )

                if any(action != MaintenanceAction.REPAIR for _, action in actions):
                    raise ValueError(
                        "correction hook may only schedule REPAIR"
                    )

                if len(targeted) != len(set(targeted)):
                    raise ValueError("correction hook targeted a summary twice")

                if state is not None:
                    for summary_id in affected:  # queued requests follow the new episode
                        state.retag(summary_id, self.summaries.get_summary(summary_id).stale_episode_id)

                for summary_id, action in actions:
                    stale = self.summaries.get_summary(summary_id)

                    cost = self._repair_cost(summary_id, stale.stale_episode_id)
                    repaired = execute_repair(summary_id, event.timestamp, cost)
                    if repaired is None:
                        state.enqueue(summary_id, stale.stale_episode_id, stale.stale_since,
                                      event.timestamp, "correction")
                        log(
                            event,
                            summary_id=summary_id,
                            stale_before=stale.is_stale,
                            episode=stale.stale_episode_id,
                            budget_outcome="deferred",
                        )
                        continue

                    result.repairs += 1
                    result.cumulative_repair_cost += cost

                    log(
                        event,
                        summary_id=summary_id,
                        stale_before=stale.is_stale,
                        episode=stale.stale_episode_id,
                        action=action,
                        repair_cost=cost,
                        budget_outcome=charged(),
                        llm_usage=repaired.llm_metadata.get("usage") if repaired.llm_metadata else None,
                        llm_metadata=repaired.llm_metadata,
                        timing=repaired.timing_ms,
                        query_visible=False,
                    )

            else:
                result.queries += 1
                first_row = len(result.logs)
                started = time.perf_counter()

                hits = self.retriever.search(
                    event.query_text,
                    event.top_k,
                )
                retrieval_ms = (time.perf_counter() - started) * 1000.0

                question_text = None
                if self.verifier is not None or self.answer_reader is not None:
                    from verify_repair.llm.reader import render_reader_question

                    question_text = render_reader_question(event, self.ledger)

                if not hits:
                    log(event, required_current=False)

                query_summary_ids: list[str] = []
                query_actions: list[str] = []
                query_evidence_blocks: list[str] = []
                query_evidence_modes: list[str] = []
                query_source_refs: dict[str, object] = {}
                query_used_stale = False
                query_required_current = False

                for hit in hits:
                    summary = self.summaries.get_summary(
                        hit.summary_id
                    )

                    action = MaintenanceAction.NO_OP
                    verify_cost = 0.0
                    repair_cost = 0.0
                    evidence = None
                    outcome = None
                    verified = None
                    repaired = None
                    maint_meta = None

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

                            if action == MaintenanceAction.REPAIR:
                                planned_repair_cost = self._repair_cost(hit.summary_id, summary.stale_episode_id)
                                repaired = execute_repair(hit.summary_id, event.timestamp, planned_repair_cost)
                                if repaired is None:
                                    # Out of budget: answer from current facts, repair later.
                                    state.enqueue(hit.summary_id, summary.stale_episode_id,
                                                  summary.stale_since, event.timestamp, "query")
                                    action = MaintenanceAction.VERIFY
                                    outcome = "deferred"
                                else:
                                    outcome = charged()

                            if action == MaintenanceAction.VERIFY:
                                if self.verifier is not None:
                                    verified = self.maintenance.verify(
                                        hit.summary_id,
                                        event.timestamp,
                                        query_text=question_text,
                                    )
                                else:
                                    verified = self.maintenance.verify(
                                        hit.summary_id,
                                        event.timestamp,
                                    )

                                evidence_refs = (
                                    verified.current_source_refs
                                )
                                evidence = verified.current_evidence
                                maint_meta = verified.llm_metadata
                                verify_cost = self._verify_cost(event.event_id, hit.summary_id)

                                result.verifications += 1
                                result.cumulative_verify_cost += (
                                    verify_cost
                                )

                            elif action == MaintenanceAction.REPAIR:
                                evidence_refs = repaired.source_refs
                                maint_meta = repaired.llm_metadata
                                repair_cost = planned_repair_cost

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

                    # Operation components are prefixed "verify_" / "repair_".
                    operation = verified if verified is not None else repaired
                    query_timing = {"retrieval": retrieval_ms}
                    if operation is not None and operation.timing_ms:
                        prefix = "verify" if operation is verified else "repair"
                        query_timing.update({f"{prefix}_{k}": v for k, v in operation.timing_ms.items()})

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
                        budget_outcome=outcome,
                        llm_usage=maint_meta.get("usage") if maint_meta else None,
                        llm_metadata=maint_meta,
                        timing=query_timing,
                        query_visible=True,
                    )
                    if state is not None:
                        state.observe(hit.summary_id, event.timestamp)

                    if self.answer_reader is not None:
                        from verify_repair.llm.reader import format_authoritative_evidence

                        query_summary_ids.append(hit.summary_id)
                        query_actions.append(action.value)
                        query_used_stale = query_used_stale or used_stale
                        query_required_current = query_required_current or required_current
                        if action == MaintenanceAction.VERIFY and verified is not None:
                            hit_mode = "verified_evidence"
                            hit_text = (
                                verified.verified_context
                                if verified.verified_context is not None
                                else format_authoritative_evidence(
                                    verified.current_evidence,
                                    verified.current_source_refs,
                                )
                            )
                        elif action == MaintenanceAction.REPAIR and repaired is not None:
                            hit_mode = "repaired_summary"
                            hit_text = repaired.text
                        else:
                            hit_mode = "stale_summary" if summary.is_stale else "fresh_summary"
                            hit_text = summary.text
                        query_evidence_modes.append(hit_mode)
                        query_evidence_blocks.append(
                            f"[{hit.summary_id}] {hit_text}" if len(hits) > 1 else hit_text
                        )
                        for slot, ref in evidence_refs.items():
                            query_source_refs[f"{hit.summary_id}:{slot}"] = ref

                if self.answer_reader is not None:
                    from verify_repair.llm.reader import EvidencePacket, format_ref_labels

                    if not hits:
                        packet_mode = "no_retrieval"
                        packet_text = ""
                        ref_labels: tuple[str, ...] = ()
                    else:
                        packet_mode = (
                            query_evidence_modes[0]
                            if len(set(query_evidence_modes)) == 1
                            else "mixed"
                        )
                        packet_text = "\n".join(query_evidence_blocks)
                        ref_labels = format_ref_labels(query_source_refs)

                    packet = EvidencePacket(
                        query_id=event.query_id,
                        question_text=question_text or event.query_text,
                        summary_ids=tuple(query_summary_ids),
                        evidence_mode=packet_mode,
                        evidence_text=packet_text,
                        source_ref_labels=ref_labels,
                    )
                    reader_started = time.perf_counter()
                    answer = self.answer_reader.answer(packet)
                    reader_ms = (time.perf_counter() - reader_started) * 1000.0
                    answer_record = {
                        "query_id": answer.query_id,
                        "event_id": event.event_id,
                        "event_timestamp": stamp(event.timestamp),
                        "summary_ids": list(query_summary_ids),
                        "actions": list(query_actions),
                        "evidence_mode": answer.evidence_mode,
                        "used_stale_source": query_used_stale,
                        "required_fact_current": query_required_current if hits else False,
                        "question_text": answer.question_text,
                        "answer_text": answer.answer_text,
                        "abstained": answer.abstained,
                        "cited_sources": list(answer.cited_sources),
                        "evidence_refs": list(answer.evidence_refs),
                        "prompt_id": answer.prompt_id,
                        "prompt_version": answer.prompt_version,
                        "llm_metadata": answer.llm_metadata,
                    }
                    result.answers.append(answer_record)
                    last_row = result.logs[-1]
                    last_row["answer"] = answer.answer_text
                    last_row["answer_abstained"] = answer.abstained
                    last_row["answer_cited_sources"] = list(answer.cited_sources)
                    last_row["evidence_refs"] = list(answer.evidence_refs)
                    last_row["evidence_mode"] = answer.evidence_mode
                    last_row["reader_llm_metadata"] = answer.llm_metadata
                    last_row["llm_usage"] = _merge_usage(
                        last_row.get("llm_usage"),
                        answer.llm_metadata.get("usage"),
                    )
                    if self.measure_latency and "timing_ms" in last_row:
                        last_row["timing_ms"]["reader"] = reader_ms

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

                    cost = self._repair_cost(summary_id, stale.stale_episode_id)
                    repaired = execute_repair(summary_id, event.timestamp, cost)
                    if repaired is None:
                        state.enqueue(summary_id, stale.stale_episode_id, stale.stale_since,
                                      event.timestamp, "tick")
                        log(
                            event,
                            summary_id=summary_id,
                            stale_before=stale.is_stale,
                            episode=stale.stale_episode_id,
                            maintenance_trigger="scheduled_tick",
                            maintenance_tick=processed_event_count,
                            budget_outcome="deferred",
                        )
                        continue

                    result.repairs += 1
                    result.cumulative_repair_cost += cost

                    log(
                        event,
                        summary_id=summary_id,
                        stale_before=stale.is_stale,
                        episode=stale.stale_episode_id,
                        action=action,
                        repair_cost=cost,
                        maintenance_trigger="scheduled_tick",
                        maintenance_tick=processed_event_count,
                        budget_outcome=charged(),
                        llm_usage=repaired.llm_metadata.get("usage") if repaired.llm_metadata else None,
                        llm_metadata=repaired.llm_metadata,
                        timing=repaired.timing_ms,
                        query_visible=False,
                    )

        if state is not None:
            waits = sorted(state.waits_seconds)
            result.budget = {
                "units": self.budget.units,
                "window_seconds": self.budget.window_seconds,
                "queue_order": self.budget.queue_order,
                "spent": state.spent,
                "max_window_spend": max(state.spent_by_window.values(), default=0.0),
                "deferrals": state.deferrals,
                "executed_from_queue": state.executed_from_queue,
                "requeued": state.requeued,
                "invalidated": state.invalidated,
                "still_queued": len(state.queue),
                "peak_queue": state.peak_queue,
                "mean_wait_s": sum(waits) / len(waits) if waits else None,
                "max_wait_s": waits[-1] if waits else None,
            }
        return result
