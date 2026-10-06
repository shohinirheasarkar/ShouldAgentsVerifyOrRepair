"""VERIFY/REPAIR atomicity and storage-free baseline decisions."""
from datetime import datetime, timedelta, timezone
import pytest

from verify_repair.db import connect
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.summaries import SummaryStore
from verify_repair.memory.staleness import mark_stale_from_fact_change
from verify_repair.maintenance.operations import MaintenanceService
from verify_repair.models import FactVersionRef, RepairBuildError
from verify_repair.policies.base import MaintenanceAction
from verify_repair.policies.verify_only import VerifyOnly
from verify_repair.policies.eager_refresh import EagerRefresh
from verify_repair.policies.repair_on_first_access import RepairOnFirstAccess

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def setup():
    db = connect()
    ledger, store = FactLedger(db), SummaryStore(db)
    ledger.append_initial_fact("location", "Alice", "location", "Maryland", T, "initial")
    store.create_summary("alice", "Alice lives in Maryland", "Alice lives in {location}",
                         {"location": FactVersionRef("location", 1)}, T)
    ledger.append_correction("location", "Boston", T + timedelta(days=1), "correct")
    mark_stale_from_fact_change(store, "location", "correct", T + timedelta(days=1))
    return db, ledger, store


def test_verify_and_repair_canonical():
    db, ledger, store = setup()
    service = MaintenanceService(ledger, store)
    result = service.verify("alice", T + timedelta(days=1))
    assert result.current_evidence == {"location": "Boston"}
    assert result.changed_sources[0].old_ref.version == 1
    assert store.get_summary("alice").text == "Alice lives in Maryland"
    assert store.get_summary("alice").is_stale
    assert store.get_source_facts("alice")["location"].version == 1
    assert store.get_summary("alice").queries_this_episode == 1
    repaired = service.repair("alice", T + timedelta(days=1))
    assert repaired.text == "Alice lives in Boston"
    assert store.get_source_facts("alice")["location"].version == 2
    assert not store.get_summary("alice").is_stale
    assert db.execute("SELECT text FROM summary_fts WHERE summary_id='alice'").fetchone()[0] == "Alice lives in Boston"


def test_repair_rollback_on_build_failure():
    db, ledger, store = setup()
    class BrokenBuilder:
        def build(self, template, current_facts):
            raise RepairBuildError("injected")
    with pytest.raises(RepairBuildError):
        MaintenanceService(ledger, store, BrokenBuilder()).repair("alice", T + timedelta(days=1))
    assert store.get_summary("alice").is_stale
    assert store.get_source_facts("alice")["location"].version == 1
    assert db.execute("SELECT text FROM summary_fts WHERE summary_id='alice'").fetchone()[0] == "Alice lives in Maryland"


def test_repair_rollback_after_index_update_failure():
    db, ledger, store = setup()
    semantic = {"alice": "Alice lives in Maryland"}
    def broken_update(summary_id, text):
        semantic[summary_id] = text
        raise RuntimeError("injected index failure")
    def rebuild():
        semantic["alice"] = store.get_summary("alice").text
    with pytest.raises(RuntimeError, match="index failure"):
        MaintenanceService(ledger, store, semantic_index_update=broken_update,
                           semantic_index_rebuild=rebuild).repair("alice", T + timedelta(days=1))
    assert store.get_summary("alice").text == semantic["alice"] == "Alice lives in Maryland"
    assert store.get_source_facts("alice")["location"].version == 1
    assert store.get_summary("alice").is_stale


def test_policies_only_return_actions():
    verify, eager = VerifyOnly(), EagerRefresh()
    assert verify.on_correction(["alice"]) == []
    assert verify.on_stale_access("alice", "where") == MaintenanceAction.VERIFY
    assert eager.on_correction(["alice"]) == [
        ("alice", MaintenanceAction.REPAIR)
    ]

    first_access = RepairOnFirstAccess()
    assert first_access.on_correction(["alice"]) == []
    assert (
        first_access.on_stale_access("alice", "where")
        == MaintenanceAction.REPAIR
    )
