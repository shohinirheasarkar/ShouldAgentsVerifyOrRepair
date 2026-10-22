"""Maintenance actions are decisions; the engine owns execution and storage."""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from verify_repair.demand import DemandPrediction


@dataclass(frozen=True)
class MaintenanceCandidate:
    """Stored stale age supplied to storage-free maintenance policies."""

    summary_id: str
    stale_since: datetime


@dataclass(frozen=True)
class StaleAccessContext:
    """Successful VERIFY count before this access and fixed nominal run costs.

    The engine reads the count from SQLite's queries_this_episode. P2 may also
    receive an immutable forecast computed from completed accesses. Policies
    receive no storage handle, evaluator labels, or actual future demand.
    """

    verifications_this_episode: int
    verify_cost_units: float
    repair_cost_units: float
    demand_prediction: DemandPrediction | None = None


class MaintenanceAction(StrEnum):
    NO_OP = "NO_OP"
    VERIFY = "VERIFY"
    REPAIR = "REPAIR"


class MaintenancePolicy(Protocol):
    """Policies may opt into access tracking with immutable prediction_config."""

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
        *,
        context: StaleAccessContext | None = None,
    ) -> MaintenanceAction: ...

    def on_maintenance_tick(
        self,
        candidates: list[MaintenanceCandidate],
    ) -> list[tuple[str, MaintenanceAction]]: ...
