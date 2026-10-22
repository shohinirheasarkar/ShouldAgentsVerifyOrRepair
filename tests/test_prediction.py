"""P2's causal moving average, threshold fallback, persistence and integration."""
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import datetime, timedelta, timezone
from math import log

import pytest

from verify_repair.db import connect, initialize, transaction
from verify_repair.demand import AccessHistory, DemandPrediction, PredictionConfig, predict_accesses
from verify_repair.memory.access import AccessHistoryStore
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.summaries import SummaryStore
from verify_repair.models import ChangeType, FactVersionRef, RepairBuildError, SummaryNotFound
from verify_repair.policies.accumulated_threshold import AccumulatedThreshold
from verify_repair.policies.base import MaintenanceAction, MaintenanceCandidate, StaleAccessContext
from verify_repair.policies.predicted_threshold import PredictedThreshold
from verify_repair.retrieval.keyword import KeywordRetriever
from verify_repair.simulator.engine import RunConfig, Simulator
from verify_repair.simulator.events import CorrectionEvent, QueryEvent
from verify_repair.simulator.synthetic import generate_synthetic, load_synthetic, read_synthetic, synthetic_run_config

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_memory(path=":memory:"):
    db = connect(str(path))
    ledger, store = FactLedger(db), SummaryStore(db)
    for sid in ("Alice", "Bob", "Carol"):
        ledger.append_initial_fact(sid, sid, "location", "Maryland", T, f"initial-{sid}")
        store.create_summary(sid, f"{sid} lives in Maryland", f"{sid} lives in {{location}}",
                             {"location": FactVersionRef(sid, 1)}, T)
    return db, ledger, store, KeywordRetriever(db)


@pytest.fixture
def memory():
    state = make_memory()
    try:
        yield state
    finally:
        state[0].close()


def query(index, sid="Alice", seconds=None):
    return QueryEvent(f"q{index}", T + timedelta(seconds=index if seconds is None else seconds),
                      index, f"q{index}", sid, 1, (sid,))


def correction(index, sid="Alice", seconds=None, **kwargs):
    return CorrectionEvent(f"c{index}", T + timedelta(seconds=index if seconds is None else seconds),
                           index, sid, kwargs.pop("change_type", ChangeType.CORRECTION),
                           kwargs.pop("value", "Boston"), **kwargs)


def simulator(memory, policy=None, config=RunConfig()):
    _, ledger, store, retriever = memory
    return Simulator(ledger, store, retriever, policy or PredictedThreshold(), config)


def actions(result, sid="Alice"):
    return [row["action"] for row in result.logs if row["event_type"] == "query" and row["summary_id"] == sid]


def test_exponential_decay_rate_recency_and_confidence():
    config = PredictionConfig(half_life_seconds=60, horizon_seconds=120)
    history = AccessHistory(3, T, 3)
    fresh = predict_accesses(history, T, config)
    half = predict_accesses(history, T + timedelta(seconds=60), config)
    old = predict_accesses(history, T + timedelta(seconds=120), config)
    assert fresh.expected_accesses == pytest.approx(6 * log(2))
    assert half.expected_accesses == pytest.approx(fresh.expected_accesses / 2)
    assert old.expected_accesses == pytest.approx(fresh.expected_accesses / 4)
    assert fresh.confident and half.confident and not old.confident
    assert not predict_accesses(history, T + timedelta(seconds=60, microseconds=1), config).confident
    assert predict_accesses(history, T + timedelta(days=1000), config).expected_accesses == 0
    assert not predict_accesses(AccessHistory(2, T, 2), T, config).confident
    # Lifetime popularity alone cannot make an isolated recent access confident.
    assert not predict_accesses(AccessHistory(100, T, 1), T, config).confident
    assert predict_accesses(AccessHistory(), T, config) == DemandPrediction(0, False)
    assert history == AccessHistory(3, T, 3)
    with pytest.raises(FrozenInstanceError):
        fresh.confident = False


@pytest.mark.parametrize("field", ["half_life_seconds", "horizon_seconds"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), -float("inf"), True])
def test_invalid_prediction_durations(field, value):
    with pytest.raises(ValueError, match="finite and positive"):
        PredictionConfig(**{field: value})


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_invalid_minimum_history(value):
    with pytest.raises(ValueError, match="positive integer"):
        PredictionConfig(min_accesses=value)


def test_invalid_overflow_and_prediction_records():
    with pytest.raises(ValueError, match="must be finite"):
        PredictionConfig(half_life_seconds=1e-300, horizon_seconds=1e300)
    for value in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            DemandPrediction(value, True)
    with pytest.raises(ValueError):
        DemandPrediction(1, 1)
    for args in [(-1, None, 0), (1, None, 1), (0, T, 0), (1, T, 2), (1, T, float("nan"))]:
        with pytest.raises(ValueError):
            AccessHistory(*args)


def test_access_store_utc_same_time_decay_and_read_only_prediction(memory):
    db, _, summaries, _ = memory
    tracker = AccessHistoryStore(db, 60)
    before = summaries.get_summary("Alice")
    tracker.observe("Alice", T)
    tracker.observe("Alice", T.astimezone(timezone(timedelta(hours=-5))))
    assert tracker.get("Alice") == AccessHistory(2, T, 2)
    predict_accesses(tracker.get("Alice"), T + timedelta(seconds=60), PredictionConfig(60))
    assert tracker.get("Alice").decayed_accesses == 2
    tracker.observe("Alice", T + timedelta(seconds=60))
    assert tracker.get("Alice") == AccessHistory(3, T + timedelta(seconds=60), 2)
    assert tracker.get("Bob") == AccessHistory()
    assert summaries.get_summary("Alice") == before
    assert summaries.get_source_facts("Alice") == {"location": FactVersionRef("Alice", 1)}
    for bad_time, message in [(T, "backwards"), (T.replace(tzinfo=None), "timezone-aware")]:
        with pytest.raises(ValueError, match=message):
            tracker.observe("Alice", bad_time)
    with pytest.raises(SummaryNotFound):
        tracker.observe("missing", T)
    with pytest.raises(ValueError, match="different half-life"):
        AccessHistoryStore(db, 120)
    with pytest.raises(RuntimeError):
        with transaction(db):
            tracker.observe("Alice", T + timedelta(seconds=61))
            raise RuntimeError("rollback")
    assert tracker.get("Alice").total_accesses == 3
    initialize(db)
    assert tracker.get("Alice").total_accesses == 3


@pytest.mark.parametrize("expected,confident,action", [
    (0, True, "VERIFY"), (3.999999999, True, "VERIFY"), (4, True, "REPAIR"),
    (1000, True, "REPAIR"), (1000, False, "VERIFY"),
])
def test_prediction_cost_boundary_and_uncertainty(expected, confident, action):
    policy = PredictedThreshold()
    context = StaleAccessContext(0, 1, 5, DemandPrediction(expected, confident))
    assert policy.on_stale_access("Alice", "where", context=context) == action
    assert policy.on_correction(["Alice"]) == []
    assert policy.on_maintenance_tick([MaintenanceCandidate("Alice", T)]) == []
    assert policy.refresh_interval_events is None
    assert asdict(policy) == {"prediction_config": asdict(PredictionConfig())}
    with pytest.raises(FrozenInstanceError):
        policy.prediction_config = PredictionConfig()


@pytest.mark.parametrize("verify,repair", [(1, 5), (.1, .3), (.1, .30000000000000004),
                                          (0, 5), (0, 0), (1, 0), (1e-300, 1e300), (1e300, 1e-300)])
def test_missing_uncertain_or_low_prediction_is_exactly_p1(verify, repair):
    for prediction in (None, DemandPrediction(1e300, False), DemandPrediction(0, True)):
        for count in range(12):
            context = StaleAccessContext(count, verify, repair, prediction)
            assert PredictedThreshold().on_stale_access("x", "q", context=context) == (
                AccumulatedThreshold().on_stale_access("x", "q", context=context))


def test_high_prediction_preserves_free_cost_rules_and_decimal_equality():
    for verify, repair, expected in [(0, 5, "VERIFY"), (0, 0, "REPAIR"), (1, 0, "REPAIR"),
                                      (.1, .3, "REPAIR"), (.1, .30000000000000004, "VERIFY")]:
        context = StaleAccessContext(0, verify, repair, DemandPrediction(2, True))
        assert PredictedThreshold().on_stale_access("x", "q", context=context) == expected
    # Large finite predictions/costs must not overflow decision arithmetic.
    context = StaleAccessContext(0, 1e-300, 1e300, DemandPrediction(1e300, True))
    assert PredictedThreshold().on_stale_access("x", "q", context=context) == "VERIFY"
    with pytest.raises(ValueError, match="P2 requires"):
        PredictedThreshold().on_stale_access("x", "q")
    with pytest.raises(ValueError, match="nonnegative integer"):
        PredictedThreshold().on_stale_access("x", "q", context=StaleAccessContext(-1, 1, 5))
    with pytest.raises(ValueError, match="finite and nonnegative"):
        PredictedThreshold().on_stale_access("x", "q", context=StaleAccessContext(0, float("nan"), 5))


def test_hot_repairs_earlier_than_cold_and_p1_using_only_fresh_history(memory):
    db, _, store, retriever = memory
    sim = simulator(memory)
    initial_rows = list(map(tuple, db.execute("SELECT * FROM fact_versions ORDER BY fact_id")))
    # Both receive three fresh accesses; only Alice's are recent at correction.
    warmup = [query(i, "Bob") for i in (1, 2, 3)] + [query(i, seconds=200000 + i) for i in (4, 5, 6)]
    result = sim.run(warmup + [correction(7, seconds=200007), correction(8, "Bob", seconds=200008),
                              correction(9, "Carol", seconds=200009)] +
                     [query(i, sid, seconds=200000 + i * (7200 if sid == "Bob" else 1)) for i, sid in
                      [(10, "Alice"), (11, "Bob"), (12, "Alice"), (13, "Bob"), (14, "Bob"),
                       (15, "Bob"), (16, "Bob")]])
    assert actions(result, "Alice") == ["NO_OP"] * 3 + ["REPAIR", "NO_OP"]
    assert actions(result, "Bob") == ["NO_OP"] * 3 + ["VERIFY"] * 4 + ["REPAIR"]
    assert store.get_summary("Alice").text == "Alice lives in Boston"
    assert store.get_source_facts("Alice")["location"].version == 2
    assert store.get_summary("Carol").is_stale
    assert sim.access_history.get("Carol") == AccessHistory()
    assert sim.access_history.get("Alice").total_accesses == 5
    assert sim.access_history.get("Bob").total_accesses == 8
    assert {hit.summary_id for hit in retriever.search("Boston", 3)} == {"Alice", "Bob"}
    assert result.stale_source_uses == 0
    assert all(row["required_fact_current"] for row in result.logs if row["event_type"] == "query")
    assert initial_rows == list(map(tuple, db.execute("SELECT * FROM fact_versions WHERE version=1 ORDER BY fact_id")))


def test_cold_start_then_hot_repairs_fourth_access_with_default_costs(memory):
    sim = simulator(memory)
    result = sim.run([correction(0, seconds=0.5), *[query(i) for i in range(1, 7)]])
    assert actions(result) == ["VERIFY"] * 3 + ["REPAIR", "NO_OP", "NO_OP"]
    assert result.verifications == 3 and result.repairs == 1
    assert result.cumulative_verify_cost == 3 and result.cumulative_repair_cost == 5
    assert sim.access_history.get("Alice").total_accesses == 6
    assert sim.summaries.get_summary("Alice").queries_this_episode == 0


def test_low_forecast_waits_for_p1_despite_confident_recent_history(memory):
    sim = simulator(memory, PredictedThreshold(PredictionConfig(horizon_seconds=1)))
    sim.run([query(i) for i in range(1, 6)])
    result = sim.run([correction(6), *[query(i) for i in range(7, 13)]])
    assert actions(result) == ["VERIFY"] * 4 + ["REPAIR", "NO_OP"]


def test_episode_resets_preserve_history_and_parent_repair_leaves_descendant_stale(memory):
    _, _, store, _ = memory
    store.add_summary_dependency("Alice", "Carol")
    sim = simulator(memory)
    sim.run([correction(1), query(2), query(3)])
    stale_since = store.get_summary("Alice").stale_since
    history = sim.access_history.get("Alice")
    sim.run([correction(4, value="Virginia")])
    assert sim.access_history.get("Alice") == history
    assert store.get_summary("Alice").queries_this_episode == 0
    assert store.get_summary("Alice").stale_since == stale_since
    result = sim.run([query(5), query(6), correction(7, value="Oregon"), query(8)])
    assert actions(result) == ["VERIFY", "REPAIR", "REPAIR"]
    assert store.get_summary("Alice").text == "Alice lives in Oregon"
    assert store.get_source_facts("Alice")["location"].version == 4
    assert store.get_summary("Carol").is_stale
    assert sim.access_history.get("Carol") == AccessHistory()


def test_expiry_resets_p1_but_preserves_accesses_and_current_evidence(memory):
    sim = simulator(memory, PredictedThreshold(PredictionConfig(min_accesses=100)))
    result = sim.run([correction(1, change_type=ChangeType.TEMPORARY_EXCEPTION,
                                 valid_to=T + timedelta(seconds=5)),
                      query(2), query(3), query(4), *[query(i) for i in range(6, 12)]])
    assert actions(result) == ["VERIFY"] * 7 + ["REPAIR", "NO_OP"]
    assert sim.access_history.get("Alice").total_accesses == 9
    assert sim.summaries.get_source_facts("Alice")["location"].version == 1
    assert result.stale_source_uses == 0


@pytest.mark.parametrize("operation", ["verify", "repair"])
def test_failed_maintenance_rolls_back_observation_and_episode_state(memory, operation):
    sim = simulator(memory)
    if operation == "repair":
        sim.run([query(i) for i in range(1, 4)])
    sim.run([correction(4)])
    before = sim.summaries.get_summary("Alice")
    history = sim.access_history.get("Alice")
    original = getattr(sim.maintenance, operation)
    semantic = {"Alice": before.text}
    if operation == "repair":
        def broken_update(sid, text):
            semantic[sid] = text
            raise RepairBuildError("injected failure")
        sim.maintenance.semantic_index_update = broken_update
        sim.maintenance.semantic_index_rebuild = lambda: semantic.update(Alice=sim.summaries.get_summary("Alice").text)
    else:
        def broken_verify(*args):
            original(*args)
            raise RepairBuildError("injected failure")
        sim.maintenance.verify = broken_verify
    with pytest.raises(RepairBuildError, match="injected"):
        sim.run([query(5)])
    assert sim.summaries.get_summary("Alice") == before
    assert sim.access_history.get("Alice") == history
    assert sim.summaries.get_source_facts("Alice")["location"].version == 1
    assert sim.retriever.search("Boston", 1) == []
    assert semantic["Alice"] == before.text
    sim.maintenance.semantic_index_update = None
    sim.maintenance.verify = original if operation == "verify" else sim.maintenance.verify
    retry = sim.run([query(5)])
    assert actions(retry) == [operation.upper()]
    assert sim.access_history.get("Alice").total_accesses == history.total_accesses + 1


def test_access_history_survives_database_reopen_and_policy_recreation(tmp_path):
    path = tmp_path / "persistent.sqlite"
    state = make_memory(path)
    sim = simulator(state)
    sim.run([query(i) for i in range(1, 4)])
    history = sim.access_history.get("Alice")
    state[0].close()
    db = connect(str(path))
    try:
        sim = simulator((db, FactLedger(db), SummaryStore(db), KeywordRetriever(db)))
        assert sim.access_history.get("Alice") == history
        assert actions(sim.run([correction(4), query(5)])) == ["REPAIR"]
    finally:
        db.close()


def test_no_hits_or_corrections_do_not_add_accesses_and_top_k_counts_each_summary(memory):
    sim = simulator(memory)
    result = sim.run([query(1, "nothingmatches"), correction(2),
                      replace(query(3), query_text="Maryland", top_k=3, expected_fact_ids=())])
    assert result.queries == 2
    assert result.logs[0]["summary_id"] is None
    assert [sim.access_history.get(sid).total_accesses for sid in ("Alice", "Bob", "Carol")] == [1, 1, 1]


def test_context_is_immutable_prior_only_and_has_no_future_metadata(memory):
    class Observer(PredictedThreshold):
        def on_stale_access(self, summary_id, query_text, *, context=None):
            assert {field.name for field in fields(context)} == {
                "verifications_this_episode", "verify_cost_units", "repair_cost_units", "demand_prediction"}
            assert {field.name for field in fields(context.demand_prediction)} == {"expected_accesses", "confident"}
            if context.verifications_this_episode == 0:
                assert context.demand_prediction == DemandPrediction(0, False)
            with pytest.raises(FrozenInstanceError):
                context.demand_prediction = None
            return super().on_stale_access(summary_id, query_text, context=context)
    result = simulator(memory, Observer()).run([correction(0, seconds=0.5), *[query(i) for i in range(1, 7)]])
    assert actions(result) == ["VERIFY"] * 3 + ["REPAIR", "NO_OP", "NO_OP"]


def test_future_suffix_cannot_change_prefix_decisions_or_history():
    runs = []
    for suffix in ([], [query(i) for i in range(7, 50)]):
        state = make_memory()
        try:
            sim = simulator(state)
            result = sim.run([correction(0, seconds=0.5), *[query(i) for i in range(1, 7)], *suffix])
            runs.append(actions(result)[:6])
        finally:
            state[0].close()
    assert runs[0] == runs[1]


def test_saved_synthetic_p2_replay_deterministic_inputs_and_ledger(tmp_path):
    path = tmp_path / "inputs.json"
    generate_synthetic().write(path)
    before = path.read_bytes()
    policy = PredictedThreshold()
    results = []
    for _ in range(2):
        workload = read_synthetic(path)
        db, ledger, store, retriever, events = load_synthetic(workload)
        original = list(map(tuple, db.execute("SELECT * FROM fact_versions ORDER BY fact_id")))
        try:
            sim = Simulator(ledger, store, retriever, policy, synthetic_run_config(workload))
            result = sim.run(events)
            results.append(result)
            for demand in workload.demands:
                rows = [r for r in result.logs if r["summary_id"] == demand.summary_id and r["event_type"] == "query"]
                assert len(rows) == demand.future_queries
                assert sim.access_history.get(demand.summary_id).total_accesses == demand.future_queries
                expected = (["VERIFY"] * demand.future_queries if demand.future_queries < 4 else
                            ["VERIFY"] * 3 + ["REPAIR"] + ["NO_OP"] * (demand.future_queries - 4))
                assert [r["action"] for r in rows] == expected
                assert all(r["required_fact_current"] for r in rows)
            assert result.stale_source_uses == 0
            assert original == list(map(tuple, db.execute("SELECT * FROM fact_versions WHERE version=1 ORDER BY fact_id")))
            assert result.cumulative_verify_cost == result.verifications
            assert result.cumulative_repair_cost == 5 * result.repairs
            assert result.logs[0]["config_snapshot"]["prediction"] == asdict(PredictionConfig())
        finally:
            db.close()
    assert results[0] == results[1]
    assert path.read_bytes() == before
    assert asdict(policy) == {"prediction_config": asdict(PredictionConfig())}


def test_outer_query_failure_after_repair_restores_semantic_index(memory, monkeypatch):
    from contextlib import contextmanager

    sim = simulator(memory)
    sim.run([query(i) for i in range(1, 4)] + [correction(4)])
    before, history = sim.summaries.get_summary("Alice"), sim.access_history.get("Alice")
    semantic = {"Alice": before.text}
    sim.maintenance.semantic_index_update = lambda sid, text: semantic.update({sid: text})
    sim._rebuild_semantic = lambda: semantic.update(Alice=sim.summaries.get_summary("Alice").text)

    @contextmanager
    def fail_after_success(db):
        with transaction(db):
            yield
            raise RuntimeError("outer transaction failure")

    monkeypatch.setattr("verify_repair.simulator.engine.transaction", fail_after_success)
    with pytest.raises(RuntimeError, match="outer transaction failure"):
        sim.run([query(5)])
    assert sim.summaries.get_summary("Alice") == before
    assert sim.access_history.get("Alice") == history
    assert sim.summaries.get_source_facts("Alice")["location"].version == 1
    assert sim.retriever.search("Boston", 1) == []
    assert semantic["Alice"] == before.text


def test_initialize_existing_database_adds_history_without_altering_source_or_summaries(memory):
    db, _, _, _ = memory
    original = {table: list(map(tuple, db.execute(f"SELECT * FROM {table}")))
                for table in ("fact_versions", "summaries", "summary_fact_sources")}
    db.execute("DROP TABLE summary_access_history")
    initialize(db)
    assert AccessHistoryStore(db, 3600).get("Alice") == AccessHistory()
    for table, rows in original.items():
        assert list(map(tuple, db.execute(f"SELECT * FROM {table}"))) == rows


def test_non_prediction_policy_preserves_config_hash_and_does_not_track(memory):
    sim = simulator(memory, AccumulatedThreshold())
    result = sim.run([correction(1), query(2)])
    assert sim.access_history is None
    assert result.logs[0]["config_hash"] == sim.config.digest()
    assert result.logs[0]["config_snapshot"] == asdict(sim.config)
    assert memory[0].execute("SELECT count(*) FROM summary_access_history").fetchone()[0] == 0


def test_reject_missing_prediction_configuration():
    with pytest.raises(ValueError, match="requires a PredictionConfig"):
        PredictedThreshold(None)


def test_unrepresentable_forecast_falls_back_instead_of_overflowing():
    prediction = predict_accesses(AccessHistory(10, T, 10), T,
                                  PredictionConfig(half_life_seconds=1, horizon_seconds=1e308))
    assert prediction == DemandPrediction(0, False)
    for count, expected in [(0, "VERIFY"), (4, "REPAIR")]:
        assert PredictedThreshold().on_stale_access(
            "x", "q", context=StaleAccessContext(count, 1, 5, prediction)) == expected


def test_store_validates_half_life_independently_of_forecast_horizon(memory):
    tracker = AccessHistoryStore(memory[0], 1e-305)
    tracker.observe("Alice", T)
    tracker.observe("Alice", T + timedelta(seconds=1))
    assert tracker.get("Alice").decayed_accesses == 1
    for bad in (0, -1, True, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite and positive"):
            AccessHistoryStore(memory[0], bad)


@pytest.mark.parametrize("policy", [AccumulatedThreshold(), PredictedThreshold()])
def test_log_config_snapshots_remain_independent_records(memory, policy):
    sim = simulator(memory, policy)
    result = sim.run([correction(1), query(2)])
    result.logs[0]["config_snapshot"]["verify_cost_units"] = 99
    assert result.logs[1]["config_snapshot"]["verify_cost_units"] == 1
    if isinstance(policy, PredictedThreshold):
        result.logs[0]["config_snapshot"]["prediction"]["min_accesses"] = 99
        assert result.logs[1]["config_snapshot"]["prediction"]["min_accesses"] == 3
    assert sim.config.verify_cost_units == 1
