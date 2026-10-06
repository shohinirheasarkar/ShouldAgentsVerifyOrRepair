"""Wait until a stale summary is accessed, then repair it once."""
from verify_repair.policies.base import MaintenanceAction


class RepairOnFirstAccess:
    """Repair stale memory on its first query instead of at correction time."""

    name = "repair-on-first-access"

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
        return MaintenanceAction.REPAIR