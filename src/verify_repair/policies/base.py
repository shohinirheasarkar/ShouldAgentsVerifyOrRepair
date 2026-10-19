"""Maintenance actions are decisions; the engine owns execution and storage."""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True)
class MaintenanceCandidate:
    """Stored stale age supplied to storage-free maintenance policies."""

    summary_id: str
    stale_since: datetime


class MaintenanceAction(StrEnum):
    NO_OP = "NO_OP"
    VERIFY = "VERIFY"
    REPAIR = "REPAIR"


class MaintenancePolicy(Protocol):
    name: str
    refresh_interval_events: int | None

    def on_correction(
        self,
        affected_summary_ids: list[str],
    ) -> list[tuple[str, MaintenanceAction]]: ...

    def on_stale_access(
        self,
        summary_id: str,
        query: str,
    ) -> MaintenanceAction: ...

    def on_maintenance_tick(
        self,
        candidates: list[MaintenanceCandidate],
    ) -> list[tuple[str, MaintenanceAction]]: ...