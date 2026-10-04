"""Regression checks for provenance integrity, retrieval ties, and failure paths."""
from datetime import datetime, timedelta, timezone
import sqlite3
import pytest

from verify_repair.db import connect
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.summaries import SummaryStore
from verify_repair.memory.staleness import mark_stale_from_fact_change
from verify_repair.maintenance.operations import MaintenanceService
from verify_repair.models import FactVersionRef, InvalidProvenanceError
from verify_repair.retrieval.keyword import KeywordRetriever
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.events import CorrectionEvent
from verify_repair.models import ChangeType
from verify_repair.policies.verify_only import VerifyOnly

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_multiple_exact_sources_and_invalid_ref_rollback():
    db = connect()
    ledger, store = FactLedger(db), SummaryStore(db)
    for fact_id, value in (("name", "Alice"), ("place", "Maryland")):
        ledger.append_initial_fact(fact_id, "Alice", fact_id, value, T, fact_id)
    store.create_summary("profile", "Alice in Maryland", "{name} in {place}",
                         {"name": FactVersionRef("name", 1), "place": FactVersionRef("place", 1)}, T)
    assert store.get_source_facts("profile") == {
        "name": FactVersionRef("name", 1), "place": FactVersionRef("place", 1)}
    with pytest.raises(sqlite3.IntegrityError):
        store.create_summary("broken", "bad", "{place}",
                             {"place": FactVersionRef("missing", 1)}, T)
    assert db.execute("SELECT count(*) FROM summaries").fetchone()[0] == 1
    with pytest.raises(InvalidProvenanceError):
        store.replace_summary_contents_and_sources("profile", "bad",
                                                   {"place": FactVersionRef("place", 1)}, T)


def test_diamond_staleness_once_and_parent_repair_does_not_clear_child():
    db = connect()
    ledger, store = FactLedger(db), SummaryStore(db)
    ledger.append_initial_fact("place", "Alice", "place", "Maryland", T, "initial")
    for summary_id in "abcd":
        store.create_summary(summary_id, "Maryland", "{place}",
                             {"place": FactVersionRef("place", 1)}, T)
    for parent, child in (("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")):
        store.add_summary_dependency(parent, child)
    ledger.append_correction("place", "Boston", T + timedelta(days=1), "correct")
    assert mark_stale_from_fact_change(store, "place", "correct", T + timedelta(days=1)) == list("abcd")
    assert db.execute("SELECT count(*) FROM stale_events WHERE summary_id='d'").fetchone()[0] == 1
    MaintenanceService(ledger, store).repair("a", T + timedelta(days=1))
    assert store.get_summary("d").is_stale


def test_keyword_ties_and_safe_query_syntax():
    db = connect()
    ledger, store = FactLedger(db), SummaryStore(db)
    ledger.append_initial_fact("x", "E", "x", "same", T, "initial")
    for summary_id in ("z", "a"):
        store.create_summary(summary_id, "same", "{x}", {"x": FactVersionRef("x", 1)}, T)
    retrieval = KeywordRetriever(db)
    assert [hit.summary_id for hit in retrieval.search("same", 2)] == ["a", "z"]
    assert retrieval.search('" OR *', 1) == []


def test_bad_correction_leaves_ledger_and_staleness_unchanged():
    db = connect()
    ledger, store = FactLedger(db), SummaryStore(db)
    ledger.append_initial_fact("x", "E", "x", "old", T, "initial")
    store.create_summary("s", "old", "{x}", {"x": FactVersionRef("x", 1)}, T)
    bad = CorrectionEvent("bad", T - timedelta(days=1), 1, "x", ChangeType.CORRECTION, "new")
    with pytest.raises(ValueError):
        Simulator(ledger, store, KeywordRetriever(db), VerifyOnly()).run([bad])
    assert len(ledger.get_history("x")) == 1
    assert not store.get_summary("s").is_stale
