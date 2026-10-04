"""Maintenance actions are decisions; the engine owns execution and storage."""
from enum import StrEnum
from typing import Protocol


class MaintenanceAction(StrEnum):
    NO_OP = "NO_OP"
    VERIFY = "VERIFY"
    REPAIR = "REPAIR"


class MaintenancePolicy(Protocol):
    name: str

    def on_correction(self, affected_summary_ids: list[str]) -> list[tuple[str, MaintenanceAction]]: ...
    def on_stale_access(self, summary_id: str, query: str) -> MaintenanceAction: ...
