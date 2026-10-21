"""P1: choose VERIFY/REPAIR from episode counts and fixed nominal costs.

Use with Simulator or `python -m verify_repair.cli run-toy --policy p1`.
The policy is stateless; successful operations and resets belong to storage.
"""

from fractions import Fraction
from math import ceil, isfinite

from verify_repair.policies.base import (
    MaintenanceAction,
    MaintenanceCandidate,
    StaleAccessContext,
)


def verification_threshold(verify_cost_units: float, repair_cost_units: float) -> int | None:
    """Return the first stale access to repair, or None if VERIFY stays free.

    Decimal strings preserve the configured nominal values without division or
    accumulation rounding moving an exact boundary to the following access.
    """
    for cost in (verify_cost_units, repair_cost_units):
        if not isfinite(cost) or cost < 0:
            raise ValueError("nominal costs must be finite and nonnegative")
    if repair_cost_units == 0:
        return 1
    if verify_cost_units == 0:
        return None
    return ceil(Fraction(str(repair_cost_units)) / Fraction(str(verify_cost_units)))


class AccumulatedThreshold:
    """Repair before the next verification would meet or exceed one repair."""

    name = "p1"
    refresh_interval_events = None

    def on_correction(self, affected_summary_ids: list[str]) -> list[tuple[str, MaintenanceAction]]:
        return []

    def on_stale_access(
        self,
        summary_id: str,
        query: str,
        *,
        context: StaleAccessContext | None = None,
    ) -> MaintenanceAction:
        if context is None:
            raise ValueError("P1 requires a stale-access context")
        count = context.verifications_this_episode
        if type(count) is not int or count < 0:
            raise ValueError("episode verification count must be a nonnegative integer")
        threshold = verification_threshold(context.verify_cost_units, context.repair_cost_units)
        if threshold is not None and count + 1 >= threshold:
            return MaintenanceAction.REPAIR
        return MaintenanceAction.VERIFY

    def on_maintenance_tick(
        self, candidates: list[MaintenanceCandidate],
    ) -> list[tuple[str, MaintenanceAction]]:
        return []
