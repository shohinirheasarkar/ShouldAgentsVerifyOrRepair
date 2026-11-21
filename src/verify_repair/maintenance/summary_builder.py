"""Summary builders: deterministic template builder and contextual repair protocol.

Inputs: template/slot facts (deterministic) or ``RepairBuildRequest`` with old
summary text, old/current ``FactVersion`` records, and changed slots (LLM).
Outputs: rebuilt summary text (and validated claims/metadata for contextual
builders). Missing/retracted deterministic slots render as ``[retracted]``.
"""
from __future__ import annotations

from dataclasses import dataclass
from string import Formatter
from typing import Any, Protocol

from verify_repair.models import FactVersion, RepairBuildError


class SummaryBuilder(Protocol):
    def build(self, template: str, current_facts: dict[str, str | None]) -> str: ...


@dataclass(frozen=True)
class RepairClaim:
    """One validated summary claim linked to an exact current fact version."""

    slot: str
    statement: str
    source_ref: str


@dataclass(frozen=True)
class RepairBuildRequest:
    """Authoritative inputs for contextual (e.g., LLM) summary repair."""

    summary_id: str
    old_text: str
    text_template: str
    old_facts: dict[str, FactVersion]
    current_facts: dict[str, FactVersion]
    changed_slots: tuple[str, ...]


@dataclass(frozen=True)
class RepairBuildOutput:
    """Validated rebuilt summary text, source-linked claims, and telemetry."""

    text: str
    claims: tuple[RepairClaim, ...]
    fidelity: dict[str, float]
    llm_metadata: dict[str, Any] | None = None


class ContextualSummaryBuilder(Protocol):
    def build_repair(self, request: RepairBuildRequest) -> RepairBuildOutput: ...


class DeterministicTemplateSummaryBuilder:
    def build(self, template: str, current_facts: dict[str, str | None]) -> str:
        fields = {name for _, name, _, _ in Formatter().parse(template) if name is not None}
        if fields != set(current_facts):
            raise RepairBuildError(f"template fields {fields} differ from source slots {set(current_facts)}")
        try:
            return template.format_map({key: value if value is not None else "[retracted]"
                                        for key, value in current_facts.items()})
        except (ValueError, KeyError, AttributeError) as exc:
            raise RepairBuildError(str(exc)) from exc
