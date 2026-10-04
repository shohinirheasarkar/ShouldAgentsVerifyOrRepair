"""Exact provenance, DAG checks, and stale-episode regressions."""
from datetime import datetime, timedelta, timezone
import sqlite3
import pytest

from verify_repair.db import connect
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.summaries import SummaryStore
from verify_repair.memory.staleness import mark_stale_from_fact_change
from verify_repair.models import DependencyCycleError, FactVersionRef

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def setup():
    db = connect()
    ledger = FactLedger(db)
    store = SummaryStore(db)
    for key, value in (("location", "Maryland"), ("job", "researcher")):
        ledger.append_initial_fact(key, "Alice", key, value, T, "initial")
    store.create_summary("a", "Alice in Maryland", "Alice in {location}",
                         {"location": FactVersionRef("location", 1)}, T)
    store.create_summary("b", "Alice researcher", "Alice {job}",
                         {"job": FactVersionRef("job", 1)}, T)
    store.create_summary("c", "Alice both", "Alice {job}",
                         {"job": FactVersionRef("job", 1)}, T)
    return db, ledger, store


def test_provenance_reverse_and_replacement():
    db, ledger, store = setup()
    assert store.get_source_facts("a") == {"location": FactVersionRef("location", 1)}
    assert store.get_direct_summaries_for_fact("location") == ["a"]
    ledger.append_correction("location", "Boston", T + timedelta(days=1), "correct")
    store.replace_summary_contents_and_sources("a", "Alice in Boston",
                                               {"location": FactVersionRef("location", 2)}, T + timedelta(days=1))
    assert store.get_source_facts("a")["location"].version == 2
    assert store.get_direct_summaries_for_fact("location") == ["a"]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM fact_versions WHERE fact_id='location'")


def test_dag_and_stale_episodes():
    _, ledger, store = setup()
    store.add_summary_dependency("a", "c")
    store.add_summary_dependency("b", "c")
    assert store.descendants("a") == ["c"]
    with pytest.raises(DependencyCycleError):
        store.add_summary_dependency("c", "a")
    ledger.append_correction("location", "Boston", T + timedelta(days=1), "event1")
    assert mark_stale_from_fact_change(store, "location", "event1", T + timedelta(days=1)) == ["a", "c"]
    assert not store.get_summary("b").is_stale
    store.db.execute("UPDATE summaries SET queries_this_episode=3 WHERE summary_id='c'")
    mark_stale_from_fact_change(store, "location", "event1", T + timedelta(days=1))
    assert store.get_summary("c").queries_this_episode == 3
    ledger.append_correction("location", "Virginia", T + timedelta(days=2), "event2")
    mark_stale_from_fact_change(store, "location", "event2", T + timedelta(days=2))
    assert store.get_summary("c").queries_this_episode == 0
    assert store.get_summary("c").stale_episode_id == "c:event2"
