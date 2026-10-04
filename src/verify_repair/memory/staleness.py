"""Propagate a fact change once per event through the summary dependency DAG."""
from __future__ import annotations

from datetime import datetime

from verify_repair.db import transaction
from verify_repair.models import stamp
from verify_repair.memory.summaries import SummaryStore


def mark_stale_from_fact_change(store: SummaryStore, changed_fact_id: str,
                                correction_event_id: str, timestamp: datetime) -> list[str]:
    affected = set(store.get_direct_summaries_for_fact(changed_fact_id))
    for summary_id in list(affected):
        affected.update(store.descendants(summary_id))
    ordered = sorted(affected)
    with transaction(store.db):
        for summary_id in ordered:
            inserted = store.db.execute("INSERT OR IGNORE INTO stale_events VALUES (?,?)",
                                        (summary_id, correction_event_id)).rowcount
            if inserted:
                store.db.execute("""UPDATE summaries SET is_stale=1,stale_since=?,stale_episode_id=?,
                    queries_this_episode=0,verification_cost_this_episode=0 WHERE summary_id=?""",
                    (stamp(timestamp), f"{summary_id}:{correction_event_id}", summary_id))
    return ordered
