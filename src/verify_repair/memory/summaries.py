"""Mutable summary cache, normalized exact provenance, and DAG dependencies."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from verify_repair.db import transaction
from verify_repair.models import (DependencyCycleError, FactVersionRef,
                                  InvalidProvenanceError, SummaryNotFound, parse, stamp)


@dataclass(frozen=True)
class SummaryRecord:
    summary_id: str
    text: str
    text_template: str
    is_stale: bool
    stale_since: datetime | None
    stale_episode_id: str | None
    queries_this_episode: int
    verification_cost_this_episode: float
    last_repair_at: datetime | None
    created_at: datetime


def _summary(row: sqlite3.Row) -> SummaryRecord:
    return SummaryRecord(row["summary_id"], row["text"], row["text_template"],
                         bool(row["is_stale"]), parse(row["stale_since"]),
                         row["stale_episode_id"], row["queries_this_episode"],
                         row["verification_cost_this_episode"], parse(row["last_repair_at"]),
                         parse(row["created_at"]))


class SummaryStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def create_summary(self, summary_id: str, text: str, text_template: str,
                       source_facts: dict[str, FactVersionRef], created_at: datetime) -> SummaryRecord:
        if not source_facts:
            raise InvalidProvenanceError("summary needs source facts")
        with transaction(self.db):
            self.db.execute("INSERT INTO summaries(summary_id,text,text_template,created_at) VALUES (?,?,?,?)",
                            (summary_id, text, text_template, stamp(created_at)))
            for slot, ref in sorted(source_facts.items()):
                self.db.execute("INSERT INTO summary_slots VALUES (?,?,?)", (summary_id, slot, ref.fact_id))
                self.db.execute("INSERT INTO summary_fact_sources VALUES (?,?,?,?)",
                                (summary_id, ref.fact_id, ref.version, slot))
            self.db.execute("INSERT INTO summary_fts(summary_id,text) VALUES (?,?)", (summary_id, text))
        return self.get_summary(summary_id)

    def get_summary(self, summary_id: str) -> SummaryRecord:
        row = self.db.execute("SELECT * FROM summaries WHERE summary_id=?", (summary_id,)).fetchone()
        if row is None:
            raise SummaryNotFound(summary_id)
        return _summary(row)

    def get_source_facts(self, summary_id: str) -> dict[str, FactVersionRef]:
        self.get_summary(summary_id)
        return {row["slot_name"]: FactVersionRef(row["fact_id"], row["fact_version"])
                for row in self.db.execute("SELECT * FROM summary_fact_sources WHERE summary_id=? ORDER BY slot_name", (summary_id,))}

    def get_slots(self, summary_id: str) -> dict[str, str]:
        self.get_summary(summary_id)
        return {row["slot_name"]: row["fact_id"] for row in self.db.execute(
            "SELECT * FROM summary_slots WHERE summary_id=? ORDER BY slot_name", (summary_id,))}

    def get_direct_summaries_for_fact(self, fact_id: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT summary_id FROM summary_fact_sources WHERE fact_id=? ORDER BY summary_id", (fact_id,))]

    def add_summary_dependency(self, parent_summary_id: str, child_summary_id: str) -> None:
        self.get_summary(parent_summary_id)
        self.get_summary(child_summary_id)
        if parent_summary_id == child_summary_id or parent_summary_id in self.descendants(child_summary_id):
            raise DependencyCycleError(f"{parent_summary_id} -> {child_summary_id} creates cycle")
        self.db.execute("INSERT INTO summary_dependencies VALUES (?,?)", (parent_summary_id, child_summary_id))

    def get_summary_children(self, summary_id: str) -> list[str]:
        self.get_summary(summary_id)
        return [r[0] for r in self.db.execute(
            "SELECT child_summary_id FROM summary_dependencies WHERE parent_summary_id=? ORDER BY child_summary_id",
            (summary_id,))]

    def descendants(self, summary_id: str) -> list[str]:
        seen: set[str] = set()
        pending = self.get_summary_children(summary_id)
        while pending:
            child = pending.pop(0)
            if child not in seen:
                seen.add(child)
                pending.extend(self.get_summary_children(child))
        return sorted(seen)

    def replace_summary_contents_and_sources(self, summary_id: str, text: str,
                                             sources: dict[str, FactVersionRef],
                                             repaired_at: datetime) -> SummaryRecord:
        if set(sources) != set(self.get_slots(summary_id)):
            raise InvalidProvenanceError("replacement must cover exactly the stored slots")
        with transaction(self.db):
            self.db.execute("DELETE FROM summary_fact_sources WHERE summary_id=?", (summary_id,))
            for slot, ref in sorted(sources.items()):
                if self.get_slots(summary_id)[slot] != ref.fact_id:
                    raise InvalidProvenanceError("slot fact identity cannot change")
                self.db.execute("INSERT INTO summary_fact_sources VALUES (?,?,?,?)",
                                (summary_id, ref.fact_id, ref.version, slot))
            self.db.execute("""UPDATE summaries SET text=?,is_stale=0,stale_since=NULL,
                stale_episode_id=NULL,queries_this_episode=0,verification_cost_this_episode=0,
                last_repair_at=? WHERE summary_id=?""", (text, stamp(repaired_at), summary_id))
            self.db.execute("DELETE FROM summary_fts WHERE summary_id=?", (summary_id,))
            self.db.execute("INSERT INTO summary_fts(summary_id,text) VALUES (?,?)", (summary_id, text))
        return self.get_summary(summary_id)
