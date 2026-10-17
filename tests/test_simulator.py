"""Toy trace correctness, replay, expiry, and retrieval regressions."""
from datetime import datetime, timezone
from pathlib import Path

from verify_repair.models import FactVersionRef
from verify_repair.policies.eager_refresh import EagerRefresh
from verify_repair.policies.verify_only import VerifyOnly
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.toy import load_toy
from verify_repair.policies.eager_refresh import EagerRefresh
from verify_repair.policies.verify_only import VerifyOnly
from verify_repair.policies.repair_on_first_access import RepairOnFirstAccess

DATA = Path(__file__).resolve().parents[1] / "data/toy"


def run(policy):
    db, ledger, store, retriever, events = load_toy(DATA)
    result = Simulator(ledger, store, retriever, policy).run(events)
    return result, db, ledger, store


def test_canonical_verify_only_three_queries():
    result, db, ledger, store = run(VerifyOnly())
    first_three = [row for row in result.logs if row["query_id"] in ("q1", "q2", "q3")]
    assert len(first_three) == 3
    assert [row["action"] for row in first_three] == ["VERIFY"] * 3
    assert all(row["summary_id"] == "alice_profile" and not row["used_stale_source"] for row in first_three)
    assert result.repairs == 0
    assert store.get_summary("alice_profile").is_stale
    assert "Maryland" in store.get_summary("alice_profile").text
    assert ledger.get_current_value("Alice", "location", datetime(2026, 1, 2, tzinfo=timezone.utc)) == "Boston"
    assert store.get_source_facts("alice_profile")["location"] == FactVersionRef("alice_location", 1)
    db.close()


def test_canonical_eager_refresh_and_expiry():
    result, db, _, store = run(EagerRefresh())
    first_three = [row for row in result.logs if row["query_id"] in ("q1", "q2", "q3")]
    assert all(row["action"] == "NO_OP" for row in first_three)
    assert result.verifications == 0
    assert "Boston" in store.get_summary("alice_profile").text
    assert store.get_source_facts("alice_profile")["location"] == FactVersionRef("alice_location", 2)
    assert not store.get_summary("alice_profile").is_stale
    assert "Room A" in store.get_summary("alice_office").text
    assert store.get_source_facts("alice_office")["office"].version == 1
    assert any(row["event_type"] == "expiry" for row in result.logs)
    db.close()


def test_deterministic_replay_and_policy_swap():
    a, db1, _, _ = run(VerifyOnly())
    b, db2, _, _ = run(VerifyOnly())
    eager, db3, _, _ = run(EagerRefresh())
    assert a.logs == b.logs
    assert a.verifications == b.verifications
    assert a.verifications > eager.verifications
    assert [row["event_id"] for row in a.logs if row["summary_id"] is None] == [
        row["event_id"] for row in eager.logs if row["summary_id"] is None]
    assert all(row["config_snapshot"]["seed"] == 12345 and len(row["trace_hash"]) == 64
               and row["latency_ms_instrumentation_only"] is None for row in a.logs)
    for db in (db1, db2, db3):
        db.close()


def test_keyword_retrieval_and_repaired_index():
    db, ledger, store, retriever, events = load_toy(DATA)
    assert retriever.search("Maryland researcher", 1)[0].summary_id == "alice_profile"
    Simulator(ledger, store, retriever, EagerRefresh()).run(events[:1])
    assert retriever.search("Maryland", 10) == []
    assert retriever.search("Boston researcher", 1)[0].summary_id == "alice_profile"
    assert len(retriever.search("Alice", 2)) == 2
    assert retriever.search("unfindableword", 5) == []
    db.close()


def test_required_three_query_canonical_slice_under_both_policies():
    for policy, expected_repairs, expected_verifications in ((VerifyOnly(), 0, 3),
                                                              (EagerRefresh(), 2, 0)):
        db, ledger, store, retriever, events = load_toy(DATA)
        result = Simulator(ledger, store, retriever, policy).run(events[:4])
        assert result.repairs == expected_repairs
        assert result.verifications == expected_verifications
        assert result.stale_source_uses == 0
        profile = store.get_summary("alice_profile")
        assert profile.is_stale is isinstance(policy, VerifyOnly)
        assert ("Maryland" if isinstance(policy, VerifyOnly) else "Boston") in profile.text
        assert store.get_source_facts("alice_profile")["location"].version == (
            1 if isinstance(policy, VerifyOnly) else 2)
        assert [row["action"] for row in result.logs if row["query_id"] in ("q1", "q2", "q3")] == (
            ["VERIFY"] * 3 if isinstance(policy, VerifyOnly) else ["NO_OP"] * 3)
        if isinstance(policy, VerifyOnly):
            assert all(row["current_evidence"]["location"] == "Boston" for row in result.logs
                       if row["query_id"] in ("q1", "q2", "q3"))
        db.close()


def test_repair_on_first_access_repairs_once():
    result, db, _, store = run(RepairOnFirstAccess())

    alice_queries = [
        row
        for row in result.logs
        if row["query_id"] in ("q1", "q2", "q3")
    ]

    assert [row["action"] for row in alice_queries] == [
        "REPAIR",
        "NO_OP",
        "NO_OP",
    ]
    assert result.verifications == 0

    profile = store.get_summary("alice_profile")
    assert not profile.is_stale
    assert "Boston" in profile.text
    assert store.get_source_facts("alice_profile")["location"].version == 2

    db.close()