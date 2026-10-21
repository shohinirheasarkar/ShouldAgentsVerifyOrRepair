"""P1 switch boundaries, stored episode state, and real simulator transitions."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from verify_repair.db import connect
from verify_repair.maintenance.operations import MaintenanceService
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.staleness import mark_stale_from_fact_change
from verify_repair.memory.summaries import SummaryStore
from verify_repair.models import ChangeType, FactVersionRef, RepairBuildError
from verify_repair.policies.accumulated_threshold import AccumulatedThreshold, verification_threshold
from verify_repair.policies.base import MaintenanceAction, MaintenanceCandidate, StaleAccessContext
from verify_repair.retrieval.keyword import KeywordRetriever
from verify_repair.simulator.engine import RunConfig, Simulator
from verify_repair.simulator.events import CorrectionEvent, QueryEvent

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
COST_CASES = [
    (1, 5, 5), (2, 5, 3), (2, 6, 3), (5, 1, 1), (2, 2, 1),
    (0.1, 1, 10), (0.1, 0.3, 3), (0.07, 0.49, 7), (0.3, 0.9, 3),
    (0.1, 0.30000000000000004, 4), (0.1, 0.29999999999999993, 3),
    (0, 5, None), (1, 0, 1), (0, 0, 1), (1e-300, 5e-300, 5),
    (1e300, 5e300, 5),
]


def correction(index, sid="Alice", value="Boston", **kwargs):
    return CorrectionEvent(f"c{index}", T + timedelta(minutes=index), index, sid,
                           kwargs.pop("change_type", ChangeType.CORRECTION), value, **kwargs)


def query(index, sid="Alice"):
    return QueryEvent(f"q{index}", T + timedelta(minutes=index), index, f"q{index}", sid, 1, (sid,))


@pytest.fixture
def memory():
    db = connect()
    ledger, store = FactLedger(db), SummaryStore(db)
    for sid in ("Alice", "Bob", "Carol"):
        ledger.append_initial_fact(sid, sid, "location", "Maryland", T, f"initial-{sid}")
        store.create_summary(sid, f"{sid} lives in Maryland", f"{sid} lives in {{location}}",
                             {"location": FactVersionRef(sid, 1)}, T)
    try:
        yield db, ledger, store, KeywordRetriever(db)
    finally:
        db.close()


@pytest.mark.parametrize("verify,repair,threshold", COST_CASES)
def test_threshold_and_pure_decisions_at_boundaries(verify, repair, threshold):
    policy = AccumulatedThreshold()
    assert verification_threshold(verify, repair) == threshold
    assert policy.on_correction(["Alice"]) == []
    assert policy.on_maintenance_tick([MaintenanceCandidate("Alice", T)]) == []
    assert policy.refresh_interval_events is None
    for count in range(12):
        context = StaleAccessContext(count, verify, repair)
        expected = (MaintenanceAction.REPAIR if threshold is not None and count + 1 >= threshold
                    else MaintenanceAction.VERIFY)
        assert policy.on_stale_access("Alice", "where", context=context) == expected
        assert context == StaleAccessContext(count, verify, repair)
        with pytest.raises(FrozenInstanceError):
            context.verifications_this_episode = 100
    assert vars(policy) == {}


def test_threshold_does_not_overflow_for_extreme_cost_ratio():
    assert verification_threshold(1e-300, 1e300) == 10 ** 600


@pytest.mark.parametrize("invalid", [-1, float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["verify_cost_units", "repair_cost_units"])
def test_reject_invalid_costs(invalid, field):
    costs = {"verify_cost_units": 1, "repair_cost_units": 5, field: invalid}
    with pytest.raises(ValueError, match="finite and nonnegative"):
        verification_threshold(**costs)
    with pytest.raises(ValueError, match="finite and nonnegative"):
        RunConfig(**costs)


@pytest.mark.parametrize("count", [-1, 0.5, True])
def test_reject_invalid_episode_counts(count):
    with pytest.raises(ValueError, match="nonnegative integer"):
        AccumulatedThreshold().on_stale_access("Alice", "where", context=StaleAccessContext(count, 1, 5))


def test_p1_requires_context():
    with pytest.raises(ValueError, match="requires a stale-access context"):
        AccumulatedThreshold().on_stale_access("Alice", "where")


@pytest.mark.parametrize("verify,repair,threshold", COST_CASES)
def test_simulator_repairs_on_exact_question_and_only_charges_one_action(memory, verify, repair, threshold):
    db, ledger, store, retriever = memory
    original = list(map(tuple, db.execute("SELECT * FROM fact_versions ORDER BY fact_id")))
    events = [query(0), correction(1), *[query(i) for i in range(2, 14)]]
    result = Simulator(ledger, store, retriever, AccumulatedThreshold(),
                       RunConfig(verify_cost_units=verify, repair_cost_units=repair)).run(events)
    rows = [row for row in result.logs if row["event_type"] == "query"]
    expected = (["VERIFY"] * 12 if threshold is None else
                ["VERIFY"] * (threshold - 1) + ["REPAIR"] + ["NO_OP"] * (12 - threshold))
    assert [row["action"] for row in rows] == ["NO_OP", *expected]
    assert result.verifications == expected.count("VERIFY")
    assert result.repairs == expected.count("REPAIR")
    assert result.cumulative_verify_cost == pytest.approx(result.verifications * verify)
    assert result.cumulative_repair_cost == result.repairs * repair
    assert result.stale_source_uses == 0
    assert all(row["required_fact_current"] for row in rows)
    assert all(row["verify_cost_units"] == (verify if row["action"] == "VERIFY" else 0)
               and row["repair_cost_units"] == (repair if row["action"] == "REPAIR" else 0) for row in rows)
    assert all(row["current_evidence"] == {"location": "Boston"}
               for row in rows if row["action"] == "VERIFY")
    final = store.get_summary("Alice")
    assert final.is_stale is (threshold is None)
    assert final.queries_this_episode == (12 if threshold is None else 0)
    assert final.verification_cost_this_episode == 0
    assert store.get_source_facts("Alice")["location"].version == (1 if threshold is None else 2)
    assert final.text == ("Alice lives in Maryland" if threshold is None else "Alice lives in Boston")
    assert bool(retriever.search("Boston", 1)) is (threshold is not None)
    assert original == list(map(tuple, db.execute("SELECT * FROM fact_versions WHERE version=1 ORDER BY fact_id")))
    assert store.get_summary("Bob").queries_this_episode == 0


def test_correction_resets_count_and_spend_but_preserves_uninterrupted_stale_age(memory):
    _, ledger, store, retriever = memory
    simulator = Simulator(ledger, store, retriever, AccumulatedThreshold())
    simulator.run([correction(1), *[query(i) for i in range(2, 6)]])
    before = store.get_summary("Alice")
    assert before.queries_this_episode == before.verification_cost_this_episode == 4
    assert before.text == "Alice lives in Maryland"
    assert store.get_source_facts("Alice")["location"].version == 1
    simulator.run([correction(6, value="Virginia")])
    reset = store.get_summary("Alice")
    assert reset.queries_this_episode == reset.verification_cost_this_episode == 0
    assert reset.stale_since == before.stale_since
    assert reset.stale_episode_id == "Alice:c6"
    result = simulator.run([query(i) for i in range(7, 13)])
    assert [row["action"] for row in result.logs] == ["VERIFY"] * 4 + ["REPAIR", "NO_OP"]
    repaired = store.get_summary("Alice")
    assert not repaired.is_stale and repaired.stale_since is None and repaired.stale_episode_id is None
    assert repaired.queries_this_episode == repaired.verification_cost_this_episode == 0
    assert repaired.text == "Alice lives in Virginia"
    assert store.get_source_facts("Alice")["location"].version == 3
    simulator.run([correction(13, value="Oregon")])
    assert store.get_summary("Alice").stale_since == T + timedelta(minutes=13)
    again = simulator.run([query(i) for i in range(14, 19)])
    assert [row["action"] for row in again.logs] == ["VERIFY"] * 4 + ["REPAIR"]


def test_independent_summary_counts_unqueried_memory_and_parent_repair(memory):
    _, ledger, store, retriever = memory
    store.add_summary_dependency("Alice", "Carol")
    simulator = Simulator(ledger, store, retriever, AccumulatedThreshold())
    events = [correction(1), correction(2, "Bob"), query(3), query(4, "Bob"),
              *[query(i) for i in range(5, 10)], query(10, "Bob")]
    result = simulator.run(events)
    rows = [row for row in result.logs if row["event_type"] == "query"]
    assert [r["action"] for r in rows if r["summary_id"] == "Alice"] == ["VERIFY"] * 4 + ["REPAIR", "NO_OP"]
    assert [r["action"] for r in rows if r["summary_id"] == "Bob"] == ["VERIFY", "VERIFY"]
    assert store.get_summary("Bob").queries_this_episode == 2
    assert store.get_summary("Bob").verification_cost_this_episode == 2
    assert store.get_summary("Carol").is_stale
    assert store.get_summary("Carol").queries_this_episode == 0
    assert store.get_summary("Carol").last_repair_at is None
    assert result.repairs == 1


def test_effective_expiry_starts_new_threshold_episode(memory):
    _, ledger, store, retriever = memory
    events = [correction(1, change_type=ChangeType.TEMPORARY_EXCEPTION,
                         valid_to=T + timedelta(minutes=7)),
              *[query(i) for i in range(2, 6)], *[query(i) for i in range(8, 14)]]
    result = Simulator(ledger, store, retriever, AccumulatedThreshold()).run(events)
    rows = [row for row in result.logs if row["event_type"] == "query"]
    assert [row["action"] for row in rows] == ["VERIFY"] * 8 + ["REPAIR", "NO_OP"]
    assert rows[3]["stale_episode_id"] == "Alice:c1"
    assert rows[4]["stale_episode_id"] == "Alice:c1:expiry"
    assert rows[3]["current_evidence"] == {"location": "Boston"}
    assert rows[4]["current_evidence"] == {"location": "Maryland"}
    assert store.get_source_facts("Alice")["location"].version == 1
    assert not store.get_summary("Alice").is_stale
    assert result.stale_source_uses == 0


def test_failed_threshold_repair_preserves_counter_and_retry_repairs(memory):
    db, ledger, store, retriever = memory
    simulator = Simulator(ledger, store, retriever, AccumulatedThreshold())
    simulator.run([correction(1), *[query(i) for i in range(2, 6)]])
    before = store.get_summary("Alice")
    semantic = {"Alice": before.text}

    def broken_update(sid, text):
        semantic[sid] = text
        raise RepairBuildError("injected index failure")

    simulator.maintenance.semantic_index_update = broken_update
    simulator.maintenance.semantic_index_rebuild = lambda: semantic.update(Alice=store.get_summary("Alice").text)
    with pytest.raises(RepairBuildError, match="injected"):
        simulator.run([query(6)])
    assert store.get_summary("Alice") == before
    assert semantic["Alice"] == before.text
    assert store.get_source_facts("Alice")["location"].version == 1
    assert retriever.search("Boston", 1) == []
    simulator.maintenance.semantic_index_update = None
    assert simulator.run([query(7)]).repairs == 1
    assert store.get_summary("Alice").queries_this_episode == 0
    assert db.execute("SELECT count(*) FROM fact_versions WHERE fact_id='Alice'").fetchone()[0] == 2


def test_preexisting_persisted_verifications_drive_new_policy_instance(memory):
    _, ledger, store, retriever = memory
    ledger.append_correction("Alice", "Boston", T + timedelta(minutes=1), "manual")
    mark_stale_from_fact_change(store, "Alice", "manual", T + timedelta(minutes=1))
    service = MaintenanceService(ledger, store)
    for i in range(4):
        service.verify("Alice", T + timedelta(minutes=2 + i))
    assert Simulator(ledger, store, retriever, AccumulatedThreshold()).run([query(6)]).repairs == 1
