"""Template-only summary builder; missing/retracted slots render as [retracted]."""
from __future__ import annotations

from string import Formatter
from typing import Protocol

from verify_repair.models import RepairBuildError


class SummaryBuilder(Protocol):
    def build(self, template: str, current_facts: dict[str, str | None]) -> str: ...


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
