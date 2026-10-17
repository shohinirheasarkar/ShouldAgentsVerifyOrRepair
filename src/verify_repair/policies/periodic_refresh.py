"""Verify stale accesses and repair stale summaries on scheduled ticks."""

from verify_repair.policies.base import MaintenanceAction


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
    ) -> MaintenanceAction:
        return MaintenanceAction.VERIFY

    def on_maintenance_tick(
        self,
        stale_summary_ids: list[str],
    ) -> list[tuple[str, MaintenanceAction]]:
        return [
            (summary_id, MaintenanceAction.REPAIR)
            for summary_id in sorted(stale_summary_ids)
        ]