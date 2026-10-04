"""Execute VERIFY without cache mutation or REPAIR as an atomic cache transition."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from verify_repair.db import transaction
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.summaries import SummaryStore
from verify_repair.maintenance.summary_builder import DeterministicTemplateSummaryBuilder, SummaryBuilder
from verify_repair.models import FactVersionRef


@dataclass(frozen=True)
class SourceChange:
    slot_name: str
    old_ref: FactVersionRef
    current_ref: FactVersionRef | None
    current_value: str | None


@dataclass(frozen=True)
class VerificationResult:
    summary_id: str
    stale_episode_id: str | None
    old_source_refs: dict[str, FactVersionRef]
    current_source_refs: dict[str, FactVersionRef | None]
    current_evidence: dict[str, str | None]
    changed_sources: list[SourceChange]
    cost_units: float


@dataclass(frozen=True)
class RepairResult:
    summary_id: str
    text: str
    source_refs: dict[str, FactVersionRef]
    cost_units: float


class MaintenanceService:
    def __init__(self, ledger: FactLedger, summaries: SummaryStore,
                 builder: SummaryBuilder | None = None, verify_cost_units: float = 1.0,
                 repair_cost_units: float = 5.0,
                 semantic_index_update: Callable[[str, str], None] | None = None,
                 semantic_index_rebuild: Callable[[], None] | None = None):
        self.ledger = ledger
        self.summaries = summaries
        self.builder = builder or DeterministicTemplateSummaryBuilder()
        self.verify_cost_units = verify_cost_units
        self.repair_cost_units = repair_cost_units
        self.semantic_index_update = semantic_index_update
        self.semantic_index_rebuild = semantic_index_rebuild

    def verify(self, summary_id: str, at_time: datetime) -> VerificationResult:
        with transaction(self.summaries.db):
            summary = self.summaries.get_summary(summary_id)
            old = self.summaries.get_source_facts(summary_id)
            current: dict[str, FactVersionRef | None] = {}
            evidence: dict[str, str | None] = {}
            changes: list[SourceChange] = []
            for slot, old_ref in old.items():
                ref = self.ledger.resolve_current_ref(old_ref, at_time)
                record = self.ledger.get_version(ref) if ref else None
                current[slot] = ref
                evidence[slot] = record.value if record else None
                if ref != old_ref:
                    changes.append(SourceChange(slot, old_ref, ref, evidence[slot]))
            self.summaries.db.execute("""UPDATE summaries SET queries_this_episode=queries_this_episode+1,
              verification_cost_this_episode=verification_cost_this_episode+? WHERE summary_id=?""",
              (self.verify_cost_units, summary_id))
        return VerificationResult(summary_id, summary.stale_episode_id, old, current,
                                  evidence, changes, self.verify_cost_units)

    def repair(self, summary_id: str, at_time: datetime) -> RepairResult:
        try:
            with transaction(self.summaries.db):
                summary = self.summaries.get_summary(summary_id)
                sources: dict[str, FactVersionRef] = {}
                values: dict[str, str | None] = {}
                for slot, fact_id in self.summaries.get_slots(summary_id).items():
                    history = self.ledger.get_history(fact_id)
                    current = self.ledger.get_current_record(history[0].entity, history[0].attribute, at_time)
                    if current is None:
                        raise ValueError(f"no event yet effective for {fact_id}")
                    sources[slot] = current.ref
                    values[slot] = current.value
                text = self.builder.build(summary.text_template, values)
                self.summaries.replace_summary_contents_and_sources(summary_id, text, sources, at_time)
                if self.semantic_index_update:
                    self.semantic_index_update(summary_id, text)
            return RepairResult(summary_id, text, sources, self.repair_cost_units)
        except BaseException:
            if self.semantic_index_update and self.semantic_index_rebuild:
                self.semantic_index_rebuild()
            raise
