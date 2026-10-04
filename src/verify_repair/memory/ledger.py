"""Append immutable temporal facts and resolve effective values at UTC times."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from verify_repair.db import transaction
from verify_repair.models import (
    ChangeType, FactNotFound, FactVersion, FactVersionNotFound,
    FactVersionRef, InvalidVersionTransition, parse, stamp,
)


def _record(row: sqlite3.Row) -> FactVersion:
    predecessor = (FactVersionRef(row["supersedes_fact_id"], row["supersedes_version"])
                   if row["supersedes_fact_id"] is not None else None)
    return FactVersion(row["fact_id"], row["version"], row["entity"], row["attribute"],
                       row["value"], ChangeType(row["change_type"]), parse(row["valid_from"]),
                       parse(row["valid_to"]), predecessor, row["event_id"], row["event_sequence"])


class FactLedger:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def _append(self, fact_id: str, entity: str, attribute: str, value: str | None,
                change_type: ChangeType, valid_from: datetime, event_id: str,
                event_sequence: int = 0, valid_to: datetime | None = None) -> FactVersion:
        start = stamp(valid_from)
        end = stamp(valid_to) if valid_to else None
        if end is not None and end <= start:
            raise InvalidVersionTransition("temporary interval must have positive duration")
        if change_type == ChangeType.TEMPORARY_EXCEPTION and end is None:
            raise InvalidVersionTransition("temporary exception needs valid_to")
        if change_type != ChangeType.TEMPORARY_EXCEPTION and end is not None:
            raise InvalidVersionTransition("only temporary exceptions have stored valid_to")
        if (change_type == ChangeType.RETRACTION) != (value is None):
            raise InvalidVersionTransition("only retractions have null values")
        with transaction(self.db):
            prior = self.db.execute("SELECT * FROM fact_versions WHERE fact_id=? ORDER BY version DESC LIMIT 1", (fact_id,)).fetchone()
            if (prior is None) != (change_type == ChangeType.INITIAL):
                raise InvalidVersionTransition("initial requires new fact; changes require existing fact")
            if prior is not None:
                if (entity, attribute) != (prior["entity"], prior["attribute"]):
                    raise InvalidVersionTransition("fact key cannot change")
                if (start, event_sequence) <= (prior["valid_from"], prior["event_sequence"]):
                    raise InvalidVersionTransition("versions must advance in event-time order")
            other = self.db.execute("SELECT DISTINCT fact_id FROM fact_versions WHERE entity=? AND attribute=?", (entity, attribute)).fetchall()
            if other and any(row[0] != fact_id for row in other):
                raise InvalidVersionTransition("entity and attribute already belong to another logical fact")
            version = 1 if prior is None else prior["version"] + 1
            self.db.execute("""INSERT INTO fact_versions
              (fact_id,version,entity,attribute,value,change_type,valid_from,valid_to,
               event_id,event_sequence,supersedes_fact_id,supersedes_version,created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (fact_id, version, entity, attribute, value, change_type.value, start, end,
               event_id, event_sequence, fact_id if prior else None,
               prior["version"] if prior else None, start))
        return self.get_version(FactVersionRef(fact_id, version))

    def append_initial_fact(self, fact_id: str, entity: str, attribute: str, value: str,
                            valid_from: datetime, event_id: str, event_sequence: int = 0) -> FactVersion:
        return self._append(fact_id, entity, attribute, value, ChangeType.INITIAL, valid_from, event_id, event_sequence)

    def _change(self, fact_id: str, value: str | None, kind: ChangeType,
                valid_from: datetime, event_id: str, event_sequence: int = 0,
                valid_to: datetime | None = None) -> FactVersion:
        history = self.get_history(fact_id)
        if not history:
            raise FactNotFound(fact_id)
        first = history[0]
        return self._append(fact_id, first.entity, first.attribute, value, kind,
                            valid_from, event_id, event_sequence, valid_to)

    def append_correction(self, fact_id: str, value: str, valid_from: datetime,
                          event_id: str, event_sequence: int = 0) -> FactVersion:
        return self._change(fact_id, value, ChangeType.CORRECTION, valid_from, event_id, event_sequence)

    def append_update(self, fact_id: str, value: str, valid_from: datetime,
                      event_id: str, event_sequence: int = 0) -> FactVersion:
        return self._change(fact_id, value, ChangeType.UPDATE, valid_from, event_id, event_sequence)

    def append_retraction(self, fact_id: str, valid_from: datetime,
                          event_id: str, event_sequence: int = 0) -> FactVersion:
        return self._change(fact_id, None, ChangeType.RETRACTION, valid_from, event_id, event_sequence)

    def append_temporary_exception(self, fact_id: str, value: str, valid_from: datetime,
                                   valid_to: datetime, event_id: str,
                                   event_sequence: int = 0) -> FactVersion:
        return self._change(fact_id, value, ChangeType.TEMPORARY_EXCEPTION,
                            valid_from, event_id, event_sequence, valid_to)

    def get_version(self, ref: FactVersionRef) -> FactVersion:
        row = self.db.execute("SELECT * FROM fact_versions WHERE fact_id=? AND version=?",
                              (ref.fact_id, ref.version)).fetchone()
        if row is None:
            raise FactVersionNotFound(ref)
        return _record(row)

    def get_history(self, fact_id: str) -> list[FactVersion]:
        return [_record(row) for row in self.db.execute(
            "SELECT * FROM fact_versions WHERE fact_id=? ORDER BY version", (fact_id,))]

    def get_current_record(self, entity: str, attribute: str,
                           at_time: datetime | None = None) -> FactVersion | None:
        now = stamp(at_time or datetime.now(timezone.utc))
        rows = self.db.execute("""SELECT * FROM fact_versions
            WHERE entity=? AND attribute=? AND valid_from<=?
            ORDER BY valid_from DESC,event_sequence DESC,version DESC""",
            (entity, attribute, now)).fetchall()
        # The latest applicable event wins. An expired exception is skipped,
        # exposing the latest permanent base (including a tombstone) below it.
        for row in rows:
            if row["valid_to"] is None or now < row["valid_to"]:
                return _record(row)
        return None

    def get_current_value(self, entity: str, attribute: str,
                          at_time: datetime | None = None) -> str | None:
        record = self.get_current_record(entity, attribute, at_time)
        return record.value if record else None

    def resolve_current_ref(self, old_ref: FactVersionRef,
                            at_time: datetime | None = None) -> FactVersionRef | None:
        old = self.get_version(old_ref)
        current = self.get_current_record(old.entity, old.attribute, at_time)
        return current.ref if current else None
