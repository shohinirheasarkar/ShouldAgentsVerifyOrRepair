"""Verify every stale access and leave the stored summary stale."""
from verify_repair.policies.base import MaintenanceAction, MaintenanceCandidate, StaleAccessContext


class VerifyOnly:
    name = "verify-only"
    refresh_interval_events = None

    def on_correction(self, affected_summary_ids: list[str]) -> list[tuple[str, MaintenanceAction]]:
        return []

    def on_stale_access(
        self, summary_id: str, query: str, *, context: StaleAccessContext | None = None,
    ) -> MaintenanceAction:
        return MaintenanceAction.VERIFY

    def on_maintenance_tick(
    self,
    candidates: list[MaintenanceCandidate],
    ) -> list[tuple[str, MaintenanceAction]]:
        return []