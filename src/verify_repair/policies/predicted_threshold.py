"""P2: advance P1's repair when a past-only moving average predicts high reuse.

Use `--policy p2` with either replay CLI. Policy instances contain only immutable
settings; the simulator owns observations, prediction, and maintenance execution.
"""
from dataclasses import dataclass
from fractions import Fraction

from verify_repair.demand import PredictionConfig
from verify_repair.policies.accumulated_threshold import AccumulatedThreshold
from verify_repair.policies.base import MaintenanceAction, StaleAccessContext


@dataclass(frozen=True)
class PredictedThreshold(AccumulatedThreshold):
    prediction_config: PredictionConfig = PredictionConfig()
    name = "p2"

    def __post_init__(self) -> None:
        if not isinstance(self.prediction_config, PredictionConfig):
            raise ValueError("P2 requires a PredictionConfig")

    def on_stale_access(
        self, summary_id: str, query: str, *, context: StaleAccessContext | None = None,
    ) -> MaintenanceAction:
        if context is None:
            raise ValueError("P2 requires a stale-access context")
        fallback = super().on_stale_access(summary_id, query, context=context)
        if fallback == MaintenanceAction.REPAIR or context.verify_cost_units == 0:
            return fallback
        prediction = context.demand_prediction
        if prediction is not None and prediction.confident:
            # Preserve decimal cost boundaries without overflow or float division.
            future_cost = (1 + Fraction(str(prediction.expected_accesses))) * Fraction(str(context.verify_cost_units))
            if future_cost >= Fraction(str(context.repair_cost_units)):
                return MaintenanceAction.REPAIR
        return fallback
