"""Verify every stale access and leave the stored summary stale."""
from verify_repair.policies.base import MaintenanceAction


class VerifyOnly:
    name = "verify-only"

    def on_correction(self, affected_summary_ids: list[str]) -> list[tuple[str, MaintenanceAction]]:
        return []

    def on_stale_access(self, summary_id: str, query: str) -> MaintenanceAction:
        return MaintenanceAction.VERIFY
