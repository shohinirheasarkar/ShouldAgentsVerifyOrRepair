"""Oldest-First ranking, stale ages, and scheduled accounting regressions."""
from datetime import datetime, timedelta, timezone

import pytest

from verify_repair.db import connect
from verify_repair.maintenance.operations import MaintenanceService
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.staleness import mark_stale_from_fact_change
from verify_repair.memory.summaries import SummaryStore
from verify_repair.models import ChangeType, FactVersionRef
from verify_repair.policies.base import MaintenanceAction, MaintenanceCandidate
from verify_repair.policies.oldest_first import OldestFirst
from verify_repair.retrieval.keyword import KeywordRetriever
from verify_repair.simulator.engine import RunConfig, Simulator
from verify_repair.simulator.events import CorrectionEvent, QueryEvent

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def memory():
    db = connect()
    ledger, store = FactLedger(db), SummaryStore(db)
    for sid in ("z_old", "m_middle", "a_new"):
        ledger.append_initial_fact(sid, sid, "location", "Maryland", T, f"initial-{sid}")
        store.create_summary(sid, f"{sid} Maryland", f"{sid} {{location}}",
                             {"location": FactVersionRef(sid, 1)}, T)
    return db, ledger, store


def test_oldest_first_correction_and_access():
    policy = OldestFirst()
    assert policy.refresh_interval_events == 100
    assert policy.repair_slots_per_tick == 1
    assert policy.on_correction(["z_old"]) == []
    assert policy.on_stale_access("z_old", "where") == MaintenanceAction.VERIFY
    assert policy.on_maintenance_tick([]) == []


def test_oldest_first_ranking_budget_ties_and_input_preservation():
    candidates = [MaintenanceCandidate("a_new", T + timedelta(hours=1)),
                  MaintenanceCandidate("z_old", T), MaintenanceCandidate("m_middle", T)]
    original = list(candidates)
    assert OldestFirst().on_maintenance_tick(candidates) == [("m_middle", MaintenanceAction.REPAIR)]
    assert OldestFirst(repair_slots_per_tick=2).on_maintenance_tick(candidates) == [
        ("m_middle", MaintenanceAction.REPAIR), ("z_old", MaintenanceAction.REPAIR)]
    assert len(OldestFirst(repair_slots_per_tick=10).on_maintenance_tick(candidates)) == 3
    assert candidates == original


@pytest.mark.parametrize("value", [0, -1])
@pytest.mark.parametrize("argument, message", [("refresh_interval_events", "refresh interval"),
                                                ("repair_slots_per_tick", "repair slots")])
def test_oldest_first_rejects_nonpositive_values(argument, message, value):
    with pytest.raises(ValueError, match=message):
        OldestFirst(**{argument: value})


def test_stale_period_survives_corrections_and_restarts_after_repair():
    db, ledger, store = memory()
    try:
        for version in (1, 2):
            timestamp = T + timedelta(hours=version)
            ledger.append_correction("z_old", f"Boston{version}", timestamp, f"c{version}")
            mark_stale_from_fact_change(store, "z_old", f"c{version}", timestamp)
            assert store.get_summary("z_old").stale_since == T + timedelta(hours=1)
            assert store.get_summary("z_old").stale_episode_id == f"z_old:c{version}"
        assert store.list_stale_maintenance_candidates() == [("z_old", T + timedelta(hours=1))]
        MaintenanceService(ledger, store).repair("z_old", T + timedelta(hours=2))
        assert store.get_summary("z_old").stale_since is None
        assert store.list_stale_maintenance_candidates() == []
        timestamp = T + timedelta(hours=3)
        ledger.append_correction("z_old", "Virginia", timestamp, "c3")
        mark_stale_from_fact_change(store, "z_old", "c3", timestamp)
        assert store.get_summary("z_old").stale_since == timestamp
    finally:
        db.close()


def test_stale_candidate_store_rejects_missing_timestamp():
    db, _, store = memory()
    try:
        db.execute("UPDATE summaries SET is_stale=1 WHERE summary_id='z_old'")
        with pytest.raises(ValueError, match="z_old.*stale_since"):
            store.list_stale_maintenance_candidates()
    finally:
        db.close()


def scenario(policy):
    db, ledger, store = memory()
    events = [CorrectionEvent(f"c{i}", T + timedelta(minutes=i), i, sid,
                              ChangeType.CORRECTION, "Boston")
              for i, sid in enumerate(("z_old", "m_middle", "a_new"), 1)]
    events.append(QueryEvent("q", T + timedelta(minutes=4), 4, "q", "a_new", 1, ("a_new",)))
    result = Simulator(ledger, store, KeywordRetriever(db), policy,
                       RunConfig(repair_cost_units=7)).run(events)
    return db, store, result


def test_oldest_first_scheduled_repairs_and_fresh_verification():
    db, store, result = scenario(OldestFirst(4, 2))
    try:
        rows = [row for row in result.logs if row["maintenance_trigger"] == "scheduled_tick"]
        assert [row["summary_id"] for row in rows] == ["z_old", "m_middle"]
        assert all(row["maintenance_tick"] == 4 and row["action"] == "REPAIR" for row in rows)
        assert result.repairs == 2
        assert result.cumulative_repair_cost == 14
        assert [row["cumulative_repair_cost"] for row in rows] == [7, 14]
        assert [row["repair_cost_units"] for row in rows] == [7, 7]
        assert store.list_stale_summary_ids() == ["a_new"]
        assert "Maryland" in store.get_summary("a_new").text
        assert all("Boston" in store.get_summary(sid).text for sid in ("z_old", "m_middle"))
        query = next(row for row in result.logs if row["event_id"] == "q" and row["maintenance_trigger"] is None)
        assert query["action"] == "VERIFY"
        assert query["current_evidence"] == {"location": "Boston"}
        assert query["required_fact_current"] is True
        assert result.verifications == 1 and result.stale_source_uses == 0
        other_db, _, replay = scenario(OldestFirst(4, 2))
        try:
            assert result.logs == replay.logs
        finally:
            other_db.close()
    finally:
        db.close()


@pytest.mark.parametrize("actions, message", [
    ([("z_old", MaintenanceAction.REPAIR)] * 2, "twice"),
    ([("missing", MaintenanceAction.REPAIR)], "fresh summary"),
    ([("z_old", MaintenanceAction.REPAIR), ("m_middle", MaintenanceAction.VERIFY)], "only schedule REPAIR"),
])
def test_invalid_scheduled_actions_fail_before_any_repair(actions, message):
    class InvalidPolicy(OldestFirst):
        def on_maintenance_tick(self, candidates):
            return actions

    db, ledger, store = memory()
    try:
        events = [CorrectionEvent("c", T + timedelta(minutes=1), 1, "z_old", ChangeType.CORRECTION, "Boston"),
                  CorrectionEvent("d", T + timedelta(minutes=2), 2, "m_middle", ChangeType.CORRECTION, "Boston")]
        with pytest.raises(ValueError, match=message):
            Simulator(ledger, store, KeywordRetriever(db), InvalidPolicy(2)).run(events)
        assert store.get_summary("z_old").is_stale
        assert store.get_summary("z_old").last_repair_at is None
    finally:
        db.close()
