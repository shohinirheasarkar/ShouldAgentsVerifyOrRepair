"""Immutable ledger and temporal resolution regressions."""
from datetime import datetime, timedelta, timezone
import sqlite3
import pytest

from verify_repair.db import connect
from verify_repair.memory.ledger import FactLedger
from verify_repair.models import FactVersionRef, InvalidVersionTransition

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def setup():
    db = connect()
    ledger = FactLedger(db)
    ledger.append_initial_fact("location", "Alice", "location", "Maryland", T, "initial")
    return db, ledger


def test_correction_history_and_sql_immutability():
    db, ledger = setup()
    before = tuple(db.execute("SELECT * FROM fact_versions WHERE version=1").fetchone())
    ledger.append_correction("location", "Boston", T + timedelta(days=5), "correct")
    assert tuple(db.execute("SELECT * FROM fact_versions WHERE version=1").fetchone()) == before
    assert ledger.get_current_value("Alice", "location", T) == "Maryland"
    assert ledger.get_current_value("Alice", "location", T + timedelta(days=6)) == "Boston"
    assert ledger.resolve_current_ref(FactVersionRef("location", 1), T + timedelta(days=6)) == FactVersionRef("location", 2)
    for statement in ("UPDATE fact_versions SET value='x'", "DELETE FROM fact_versions"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(statement)


def test_update_retraction_and_independent_fact():
    _, ledger = setup()
    ledger.append_initial_fact("job", "Alice", "job", "researcher", T, "initial")
    ledger.append_update("location", "Virginia", T + timedelta(days=2), "update")
    ledger.append_retraction("location", T + timedelta(days=3), "retract")
    assert ledger.get_current_value("Alice", "location", T + timedelta(days=2)) == "Virginia"
    assert ledger.get_current_value("Alice", "location", T + timedelta(days=3)) is None
    assert ledger.get_current_record("Alice", "location", T + timedelta(days=3)).version == 3
    assert ledger.get_current_value("Alice", "job", T + timedelta(days=4)) == "researcher"
    assert len(ledger.get_history("location")) == 3


def test_temporary_exception_expiry_and_permanent_update_precedence():
    _, ledger = setup()
    ledger.append_temporary_exception("location", "Boston", T + timedelta(days=2),
                                      T + timedelta(days=5), "temporary")
    assert ledger.get_current_value("Alice", "location", T + timedelta(days=3)) == "Boston"
    assert ledger.get_current_value("Alice", "location", T + timedelta(days=5)) == "Maryland"
    ledger.append_update("location", "Virginia", T + timedelta(days=4), "update")
    assert ledger.get_current_value("Alice", "location", T + timedelta(days=4)) == "Virginia"
    assert ledger.get_current_value("Alice", "location", T + timedelta(days=6)) == "Virginia"


def test_ordering_and_invalid_transitions():
    db, ledger = setup()
    ledger.append_correction("location", "Boston", T, "second", 1)
    assert ledger.get_current_value("Alice", "location", T) == "Boston"
    with pytest.raises(InvalidVersionTransition):
        ledger.append_update("location", "X", T, "duplicate-order", 1)
    with pytest.raises(InvalidVersionTransition):
        ledger.append_initial_fact("location", "Alice", "location", "X", T, "duplicate")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO fact_versions SELECT * FROM fact_versions WHERE version=1 LIMIT 1")
