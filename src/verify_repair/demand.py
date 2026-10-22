"""Past-only exponential access-rate estimates for P2; all durations are seconds.

The simulator supplies stored history and event time. No trace or evaluator
metadata is accepted. Defaults are development heuristics, not fitted estimates.
"""
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, log

from verify_repair.models import utc


@dataclass(frozen=True)
class PredictionConfig:
    half_life_seconds: float = 3600.0
    horizon_seconds: float = 86400.0
    min_accesses: int = 3

    def __post_init__(self) -> None:
        for name in ("half_life_seconds", "horizon_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if type(self.min_accesses) is not int or self.min_accesses < 1:
            raise ValueError("min_accesses must be a positive integer")
        # Require a representable multiplier even for extreme API inputs.
        if not isfinite(self.horizon_seconds / self.half_life_seconds):
            raise ValueError("prediction horizon / half-life must be finite")


@dataclass(frozen=True)
class AccessHistory:
    total_accesses: int = 0
    last_access_at: datetime | None = None
    decayed_accesses: float = 0.0

    def __post_init__(self) -> None:
        if type(self.total_accesses) is not int or self.total_accesses < 0:
            raise ValueError("total_accesses must be a nonnegative integer")
        if not isfinite(self.decayed_accesses) or not 0 <= self.decayed_accesses <= self.total_accesses:
            raise ValueError("invalid decayed access count")
        if (self.last_access_at is None) != (self.total_accesses == 0):
            raise ValueError("access count and last-access time disagree")
        if self.last_access_at is not None:
            utc(self.last_access_at)


@dataclass(frozen=True)
class DemandPrediction:
    """Additional accesses over the rolling horizon, excluding this query.

    Confidence is only a minimum-history/recency gate, not a probability.
    """
    expected_accesses: float
    confident: bool

    def __post_init__(self) -> None:
        if not isfinite(self.expected_accesses) or self.expected_accesses < 0:
            raise ValueError("predicted accesses must be finite and nonnegative")
        if type(self.confident) is not bool:
            raise ValueError("prediction confidence must be boolean")


def decayed_count(history: AccessHistory, at_time: datetime, half_life_seconds: float) -> float:
    """Read history at event time without mutating it; same-time accesses are valid."""
    now = utc(at_time)
    if history.last_access_at is None:
        return 0.0
    elapsed = (now - utc(history.last_access_at)).total_seconds()
    if elapsed < 0:
        raise ValueError("access timestamps must not move backwards")
    return history.decayed_accesses * 2.0 ** (-elapsed / half_life_seconds)


def predict_accesses(history: AccessHistory, at_time: datetime, config: PredictionConfig) -> DemandPrediction:
    count = decayed_count(history, at_time, config.half_life_seconds)
    expected = count * log(2) * (config.horizon_seconds / config.half_life_seconds)
    if not isfinite(expected):
        # An unrepresentable estimate is unusable evidence; let P1 decide.
        return DemandPrediction(0.0, False)
    confident = (
        history.total_accesses >= config.min_accesses
        and count >= config.min_accesses / 2
        and history.last_access_at is not None
        and (utc(at_time) - utc(history.last_access_at)).total_seconds() <= config.half_life_seconds
    )
    return DemandPrediction(expected, confident)
