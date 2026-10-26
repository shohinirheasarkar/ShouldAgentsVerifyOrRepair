"""Hindsight oracle, regret scoring, latency instrumentation, and experiment runs.

Run with ``python -m pytest -q tests/test_evaluation.py``; no models or network.
"""
import csv
from itertools import product

import pytest

from verify_repair.cli import main
from verify_repair.evaluation import oracle_episode_cost, percentile, score_synthetic_run
from verify_repair.experiments import (
    RESULT_FIELDS, confidence_interval, make_policy, run_experiment, summarize_rows,
)
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.synthetic import (
    SyntheticConfig, generate_synthetic, load_synthetic, synthetic_run_config,
)


def exact_workload(cold=0, warm=3, hot=11, seed=7):
    return generate_synthetic(SyntheticConfig(
        num_memories=3, seed=seed, group_counts=(1, 1, 1),
        cold_queries=(cold, cold), warm_queries=(warm, warm), hot_queries=(hot, hot),
    ))


def replay(workload, name, measure_latency=False, verify_cost=1.0, repair_cost=5.0):
    from dataclasses import replace
    db, ledger, summaries, retriever, events = load_synthetic(workload)
    config = replace(synthetic_run_config(workload),
                     verify_cost_units=verify_cost, repair_cost_units=repair_cost)
    try:
        return Simulator(ledger, summaries, retriever, make_policy(name, 2, 1), config,
                         measure_latency=measure_latency).run(events)
    finally:
        db.close()


def brute_force_oracle(k, verify_cost, repair_cost):
    """Try every action sequence: verify j queries then repair, or verify all."""
    options = [j * verify_cost + repair_cost for j in range(k)]
    options.append(k * verify_cost)
    return min(options) if k else 0.0


@pytest.mark.parametrize("k,costs", product(range(8), [(1, 5), (2, 5), (1, 1), (0, 3), (3, 0), (0.1, 0.3)]))
def test_oracle_matches_exhaustive_search(k, costs):
    assert oracle_episode_cost(k, *costs) == pytest.approx(brute_force_oracle(k, *costs))


def test_oracle_rejects_invalid_counts():
    for bad in (-1, 1.5, True):
        with pytest.raises(ValueError):
            oracle_episode_cost(bad, 1, 5)


def test_nearest_rank_percentile():
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 100) == 100
    assert percentile([3.0], 95) == 3.0
    assert percentile([], 95) is None
    with pytest.raises(ValueError):
        percentile(values, 0)


# Exact cold/warm/hot demand of 0/3/11 queries with c_v = 1, c_r = 5.
# Oracle: 0 + 3 + 5. P1 repairs on query 5: 0 + 3 + (4 + 5).
@pytest.mark.parametrize("name,expected", [
    ("verify-only", {"all": 14, "cold": 0, "warm": 3, "hot": 11}),
    ("eager-refresh", {"all": 15, "cold": 5, "warm": 5, "hot": 5}),
    ("repair-on-first-access", {"all": 10, "cold": 0, "warm": 5, "hot": 5}),
    ("p1", {"all": 12, "cold": 0, "warm": 3, "hot": 9}),
])
def test_scores_match_hand_computed_costs(name, expected):
    workload = exact_workload()
    scores = {s.group: s for s in score_synthetic_run(workload, replay(workload, name), 1, 5)}
    oracle = {"all": 8, "cold": 0, "warm": 3, "hot": 5}
    for group, cost in expected.items():
        assert scores[group].total_cost == cost
        assert scores[group].oracle_cost == oracle[group]
        assert scores[group].regret == cost - oracle[group]
    assert scores["cold"].cost_vs_oracle is None
    assert scores["all"].cost_vs_oracle == pytest.approx(expected["all"] / 8)
    assert scores["all"].queries == 14 and scores["all"].memories == 3


@pytest.mark.parametrize("name", ["verify-only", "eager-refresh", "repair-on-first-access",
                                  "periodic-refresh", "oldest-first", "p1", "p2"])
def test_oracle_is_never_beaten_and_p1_stays_within_twice_oracle(name):
    workload = generate_synthetic(SyntheticConfig(num_memories=60, seed=3))
    for repair_cost in (2.0, 5.0, 10.0):
        scores = score_synthetic_run(workload, replay(workload, name, repair_cost=repair_cost), 1, repair_cost)
        for score in scores:
            assert score.regret >= -1e-9
            if name == "p1":
                assert score.total_cost <= 2 * score.oracle_cost + 1e-9


def test_scheduled_repairs_on_query_ticks_are_not_counted_as_accesses():
    workload = exact_workload()
    result = replay(workload, "oldest-first")  # ticks every 2 events, mostly on queries
    ticks = [row for row in result.logs if row["maintenance_trigger"] == "scheduled_tick"]
    assert any(row["event_type"] == "query" for row in ticks)
    scores = {s.group: s for s in score_synthetic_run(workload, result, 1, 5)}
    assert scores["all"].queries == 14
    assert scores["all"].repairs == len(ticks) == result.repairs


def test_scoring_rejects_logs_from_a_different_workload():
    result = replay(exact_workload(hot=11), "p1")
    with pytest.raises(ValueError, match="future query counts"):
        score_synthetic_run(exact_workload(hot=12), result, 1, 5)


def test_latency_is_opt_in_and_changes_no_other_log_field():
    workload = exact_workload()
    plain = replay(workload, "p1")
    timed = replay(workload, "p1", measure_latency=True)
    assert plain.query_latency_ms == []
    assert all(row["latency_ms_instrumentation_only"] is None for row in plain.logs)
    assert len(timed.query_latency_ms) == timed.queries == 14
    assert all(value >= 0 for value in timed.query_latency_ms)
    scores = {s.group: s for s in score_synthetic_run(workload, timed, 1, 5)}
    for a, b in zip(plain.logs, timed.logs, strict=True):
        if a["event_type"] == "query":
            assert b["latency_ms_instrumentation_only"] >= 0
        else:
            assert b["latency_ms_instrumentation_only"] is None
        assert ({k: v for k, v in a.items() if k != "latency_ms_instrumentation_only"}
                == {k: v for k, v in b.items() if k != "latency_ms_instrumentation_only"})
    assert scores["all"].latency_p95_ms is not None
    assert scores["cold"].latency_p95_ms is None  # no cold queries
    assert scores["hot"].maintenance_latency_p95_ms is not None
    assert scores["hot"].no_maintenance_latency_p95_ms is not None


def test_confidence_interval_uses_student_t():
    mean, half_width = confidence_interval([1.0, 2.0, 3.0])
    assert mean == 2.0
    assert half_width == pytest.approx(4.303 / 3 ** 0.5)
    assert confidence_interval([4.0]) == (4.0, None)


def test_experiment_rows_are_deterministic_and_inputs_are_unchanged(tmp_path):
    paths = []
    for seed in (1, 2):
        path = tmp_path / f"w{seed}.json"
        generate_synthetic(SyntheticConfig(num_memories=9, seed=seed)).write(path)
        paths.append(path)
    before = [path.read_bytes() for path in paths]
    args = (paths, ["verify-only", "p1"], [(1.0, 2.0), (1.0, 5.0)])
    first = list(run_experiment(*args))
    assert len(first) == 2 * 2 * 2 * 4
    assert first == list(run_experiment(*args))
    assert [path.read_bytes() for path in paths] == before
    assert all(set(row) == set(RESULT_FIELDS) for row in first)
    # Every policy at one cost ratio replays the same saved trace.
    assert len({row["trace_hash"] for row in first if row["seed"] == 1}) == 1
    summary = summarize_rows([{k: str(v) if v is not None else "" for k, v in row.items()} for row in first])
    assert len(summary) == 2 * 2 * 4 and all(row["seeds"] == 2 for row in summary)


def test_experiment_cli_writes_and_summarizes_without_overwriting(tmp_path, capsys):
    workload, results, summary = tmp_path / "w.json", tmp_path / "r.csv", tmp_path / "s.csv"
    main(["generate-synthetic", "--memories", "6", "--output", str(workload)])
    main(["run-experiment", "--workload", str(workload), "--policies", "p1", "p2",
          "--repair-costs", "2", "5", "--measure-latency", "--output", str(results)])
    assert "rows=16" in capsys.readouterr().out
    with results.open() as stream:
        rows = list(csv.DictReader(stream))
    assert {row["policy"] for row in rows} == {"p1", "p2"}
    assert all(float(row["latency_p95_ms"]) >= 0 for row in rows if row["group"] == "all")
    main(["summarize-experiment", "--input", str(results), "--output", str(summary)])
    assert "settings=16" in capsys.readouterr().out
    before = results.read_bytes()
    with pytest.raises(SystemExit) as error:
        main(["run-experiment", "--workload", str(workload), "--output", str(results)])
    assert error.value.code == 2 and "already exists" in capsys.readouterr().err
    assert results.read_bytes() == before


@pytest.mark.parametrize("extra,message", [
    (["--repair-costs", "-1"], "finite and nonnegative"),
    (["--refresh-interval-events", "0"], "positive"),
])
def test_experiment_cli_rejects_invalid_settings_before_writing(tmp_path, capsys, extra, message):
    workload, results = tmp_path / "w.json", tmp_path / "r.csv"
    main(["generate-synthetic", "--memories", "3", "--output", str(workload)])
    with pytest.raises(SystemExit) as error:
        main(["run-experiment", "--workload", str(workload), *extra, "--output", str(results)])
    assert error.value.code == 2 and message in capsys.readouterr().err
    assert not results.exists()
