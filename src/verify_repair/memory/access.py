"""Persist P2's per-summary access history separately from stale episode state.

Call observe inside the query's transaction so failed maintenance does not add
an observation. Only the simulator records successful retrieved-summary uses.
"""
import sqlite3
from datetime import datetime
from math import isfinite

from verify_repair.demand import AccessHistory, decayed_count
from verify_repair.models import SummaryNotFound, parse, stamp


class AccessHistoryStore:
    def __init__(self, db: sqlite3.Connection, half_life_seconds: float):
        if isinstance(half_life_seconds, bool) or not isfinite(half_life_seconds) or half_life_seconds <= 0:
            raise ValueError("half_life_seconds must be finite and positive")
        self.db = db
        self.half_life_seconds = half_life_seconds
        if db.execute("SELECT 1 FROM summary_access_history WHERE half_life_seconds != ? LIMIT 1",
                      (half_life_seconds,)).fetchone():
            raise ValueError("stored access history uses a different half-life")

    def get(self, summary_id: str) -> AccessHistory:
        if self.db.execute("SELECT 1 FROM summaries WHERE summary_id=?", (summary_id,)).fetchone() is None:
            raise SummaryNotFound(summary_id)
        row = self.db.execute("SELECT * FROM summary_access_history WHERE summary_id=?", (summary_id,)).fetchone()
        if row is None:
            return AccessHistory()
        if row["half_life_seconds"] != self.half_life_seconds:
            raise ValueError("stored access history uses a different half-life")
        return AccessHistory(row["total_accesses"], parse(row["last_access_at"]), row["decayed_accesses"])

    def observe(self, summary_id: str, at_time: datetime) -> None:
        history = self.get(summary_id)
        count = decayed_count(history, at_time, self.half_life_seconds) + 1
        self.db.execute("""
            INSERT INTO summary_access_history VALUES (?,?,?,?,?)
            ON CONFLICT(summary_id) DO UPDATE SET
              total_accesses=excluded.total_accesses, last_access_at=excluded.last_access_at,
              decayed_accesses=excluded.decayed_accesses
            """, (summary_id, history.total_accesses + 1, stamp(at_time), count, self.half_life_seconds))
