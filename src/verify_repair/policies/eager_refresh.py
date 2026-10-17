"""Repair every affected summary immediately after a fact change."""
from verify_repair.policies.base import MaintenanceAction


class EagerRefresh:
    name = "eager-refresh"
    refresh_interval_events = None

    def on_correction(self, affected_summary_ids: list[str]) -> list[tuple[str, MaintenanceAction]]:
        return [(summary_id, MaintenanceAction.REPAIR) for summary_id in affected_summary_ids]

    def on_stale_access(self, summary_id: str, query: str) -> MaintenanceAction:
        return MaintenanceAction.REPAIR

    def on_maintenance_tick(
        self,
        stale_summary_ids: list[str],
    ) -> list[tuple[str, MaintenanceAction]]:
        return []
