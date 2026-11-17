"""Shared repair budget B per reset window, plus the deferred-repair queue.

Rules (documented in docs/step3/README.md):

* **Unit.** The budget is in the same nominal units as ``repair_cost_units``;
  every REPAIR, from any trigger (correction hook, query, scheduled tick, or
  the deferred queue), reserves exactly ``c_r`` before it runs.
* **Windows.** Window ``w`` covers ``[origin + w*W, origin + (w+1)*W)``. An
  event belongs to the window containing its timestamp. ``origin`` defaults to
  the first event's timestamp; experiments pass the trace start.
* **Reset and carry-over.** The budget resets to ``B`` when the first event of
  a new window is processed. Unused budget does not carry over.
* **Verification.** VERIFY never draws on ``B`` (it still costs ``c_v`` in the
  scored total). A query whose REPAIR is denied is VERIFIED instead, so the
  answer stays current, and the repair is queued.
* **Failure.** A reservation is refunded if the repair raises; the error then
  propagates as before. Nothing is ever spent beyond ``B`` in a window.
* **Queue.** One entry per stale summary, tagged with its stale episode. A new
  source change re-tags the entry with the new episode; any repair of the
  summary removes it. At each window reset the queue is drained in priority
  order while the budget allows; the rest wait for the next window.
* **Priority.** ``predicted-savings`` (default) ranks entries by
  ``forecast_accesses * c_v - c_r`` from past accesses only (the same
  decayed-count estimator as P2); ``oldest`` ranks by ``stale_since``. Ties
  break by ``stale_since`` and then summary ID.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from math import floor, isfinite

from verify_repair.demand import AccessHistory, PredictionConfig, decayed_count, predict_accesses

QUEUE_ORDERS = ("predicted-savings", "oldest")
EPSILON = 1e-9


@dataclass(frozen=True)
class RepairBudget:
    units: float
    window_seconds: float = 3600.0
    queue_order: str = "predicted-savings"
    origin: datetime | None = None
    forecast: PredictionConfig = PredictionConfig()

    def __post_init__(self) -> None:
        if isinstance(self.units, bool) or not isfinite(self.units) or self.units < 0:
            raise ValueError("budget units must be finite and nonnegative")
        if isinstance(self.window_seconds, bool) or not isfinite(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError("budget window must be finite and positive")
        if self.queue_order not in QUEUE_ORDERS:
            raise ValueError(f"queue_order must be one of {QUEUE_ORDERS}")

    def snapshot(self) -> dict:
        data = asdict(self)
        data["origin"] = self.origin.isoformat() if self.origin else None
        return data


@dataclass
class QueueEntry:
    summary_id: str
    episode_id: str
    stale_since: datetime
    enqueued_at: datetime
    trigger: str


@dataclass
class BudgetState:
    """Mutable per-run budget accounting; owned by the simulator."""

    config: RepairBudget
    repair_cost: float
    origin: datetime
    window: int | None = None
    remaining: float = 0.0
    spent: float = 0.0
    queue: dict[str, QueueEntry] = field(default_factory=dict)
    history: dict[str, AccessHistory] = field(default_factory=dict)
    deferrals: int = 0
    requeued: int = 0
    invalidated: int = 0
    executed_from_queue: int = 0
    peak_queue: int = 0
    waits_seconds: list[float] = field(default_factory=list)
    spent_by_window: dict[int, float] = field(default_factory=dict)

    def window_of(self, at_time: datetime) -> int:
        return floor((at_time - self.origin).total_seconds() / self.config.window_seconds)

    def window_start(self, window: int) -> datetime:
        return self.origin + timedelta(seconds=window * self.config.window_seconds)

    def roll(self, at_time: datetime) -> bool:
        """Advance to the event's window; return True if the budget reset."""
        window = self.window_of(at_time)
        if self.window is not None and window < self.window:
            raise ValueError("events must not move to an earlier budget window")
        if window == self.window:
            return False
        self.window, self.remaining = window, float(self.config.units)
        return True

    def can_afford(self) -> bool:
        return self.remaining + EPSILON >= self.repair_cost

    def reserve(self) -> None:
        if not self.can_afford():
            raise ValueError("repair reservation exceeds the remaining budget")
        self.remaining -= self.repair_cost
        self.spent += self.repair_cost
        self.spent_by_window[self.window] = self.spent_by_window.get(self.window, 0.0) + self.repair_cost

    def refund(self) -> None:
        self.remaining += self.repair_cost
        self.spent -= self.repair_cost
        self.spent_by_window[self.window] -= self.repair_cost

    def enqueue(self, summary_id: str, episode_id: str, stale_since: datetime,
                at_time: datetime, trigger: str) -> None:
        self.deferrals += 1
        if summary_id not in self.queue:
            self.queue[summary_id] = QueueEntry(summary_id, episode_id, stale_since, at_time, trigger)
            self.peak_queue = max(self.peak_queue, len(self.queue))

    def retag(self, summary_id: str, episode_id: str) -> None:
        entry = self.queue.get(summary_id)
        if entry is not None and entry.episode_id != episode_id:
            entry.episode_id = episode_id
            self.requeued += 1

    def observe(self, summary_id: str, at_time: datetime) -> None:
        history = self.history.get(summary_id, AccessHistory())
        count = decayed_count(history, at_time, self.config.forecast.half_life_seconds) + 1
        self.history[summary_id] = AccessHistory(history.total_accesses + 1, at_time, count)

    def priority(self, entry: QueueEntry, at_time: datetime, verify_cost: float):
        tie = (entry.stale_since, entry.summary_id)
        if self.config.queue_order == "oldest":
            return tie
        forecast = predict_accesses(self.history.get(entry.summary_id, AccessHistory()), at_time,
                                    self.config.forecast)
        return (-(forecast.expected_accesses * verify_cost - self.repair_cost), *tie)

    def ordered(self, at_time: datetime, verify_cost: float) -> list[QueueEntry]:
        return sorted(self.queue.values(), key=lambda entry: self.priority(entry, at_time, verify_cost))
