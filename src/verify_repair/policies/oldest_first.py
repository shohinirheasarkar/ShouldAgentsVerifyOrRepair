"""Rank supplied stale candidates by age and return bounded scheduled repairs."""

from verify_repair.policies.base import MaintenanceAction, MaintenanceCandidate, StaleAccessContext


class OldestFirst:
    """Verify query accesses; spend limited repair slots on the oldest memories."""

    name = "oldest-first"

    def __init__(self, refresh_interval_events: int = 100, repair_slots_per_tick: int = 1):
        if refresh_interval_events <= 0:
            raise ValueError("refresh interval must be positive")
        if repair_slots_per_tick <= 0:
            raise ValueError("repair slots per tick must be positive")
        self.refresh_interval_events = refresh_interval_events
        self.repair_slots_per_tick = repair_slots_per_tick

    def on_correction(self, affected_summary_ids: list[str]) -> list[tuple[str, MaintenanceAction]]:
        return []

    def on_stale_access(
        self, summary_id: str, query: str, *, context: StaleAccessContext | None = None,
    ) -> MaintenanceAction:
        return MaintenanceAction.VERIFY

    def on_maintenance_tick(
        self, candidates: list[MaintenanceCandidate],
    ) -> list[tuple[str, MaintenanceAction]]:
        ordered = sorted(candidates, key=lambda item: (item.stale_since, item.summary_id))
        return [
            (candidate.summary_id, MaintenanceAction.REPAIR)
            for candidate in ordered[:self.repair_slots_per_tick]
        ]
