"""Verify stale accesses and repair stale summaries on scheduled ticks."""

from verify_repair.policies.base import MaintenanceAction, MaintenanceCandidate, StaleAccessContext


class PeriodicRefresh:
    """Verify before a scheduled refresh; repair all stale summaries at the tick."""

    name = "periodic-refresh"

    def __init__(self, refresh_interval_events: int = 100):
        if refresh_interval_events <= 0:
            raise ValueError("refresh interval must be positive")
        self.refresh_interval_events = refresh_interval_events

    def on_correction(
        self,
        affected_summary_ids: list[str],
    ) -> list[tuple[str, MaintenanceAction]]:
        return []

    def on_stale_access(
        self,
        summary_id: str,
        query: str,
        *,
        context: StaleAccessContext | None = None,
    ) -> MaintenanceAction:
        return MaintenanceAction.VERIFY

    def on_maintenance_tick(
        self,
        candidates: list[MaintenanceCandidate],
    ) -> list[tuple[str, MaintenanceAction]]:
        return [
            (candidate.summary_id, MaintenanceAction.REPAIR)
            for candidate in sorted(candidates, key=lambda item: item.summary_id)
        ]