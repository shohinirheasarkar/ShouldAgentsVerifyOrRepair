"""Answer from stale summaries between scheduled refreshes (no query-time maintenance)."""

from verify_repair.policies.base import MaintenanceAction, MaintenanceCandidate, StaleAccessContext


class PeriodicRefreshStale:
    """Use the stored (possibly stale) summary at query time; repair all stale summaries at ticks.

    This is the "batch rebuild later, serve whatever you have now" baseline. It
    never verifies, so every query to a stale summary between ticks is a stale
    evidence use and is charged the configured stale-use penalty λ by the
    scorer. Tick ordering matches ``PeriodicRefresh`` (by summary ID).
    """

    name = "periodic-refresh-stale"
    uses_stale_evidence = True

    def __init__(self, refresh_interval_events: int = 100):
        if refresh_interval_events <= 0:
            raise ValueError("refresh interval must be positive")
        self.refresh_interval_events = refresh_interval_events

    def on_correction(self, affected_summary_ids: list[str]) -> list[tuple[str, MaintenanceAction]]:
        return []

    def on_stale_access(
        self, summary_id: str, query: str, *, context: StaleAccessContext | None = None,
    ) -> MaintenanceAction:
        return MaintenanceAction.NO_OP

    def on_maintenance_tick(
        self, candidates: list[MaintenanceCandidate],
    ) -> list[tuple[str, MaintenanceAction]]:
        return [(candidate.summary_id, MaintenanceAction.REPAIR)
                for candidate in sorted(candidates, key=lambda item: item.summary_id)]
