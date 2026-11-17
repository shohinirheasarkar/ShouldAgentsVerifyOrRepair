"""Multi-episode scoring for synthetic-v2 traces and the stale-use baseline.

Run with ``python -m pytest -q tests/test_scoring_v2.py``; no models or network.

The hand-check trace (``handcheck``) is small enough to score on paper. With
c_v = 1, c_r = 3, λ = 10 and a maintenance tick every 4 processed events:

    minute  event                         episode
    1       Q memory 1                    fresh
    2       CORRECTION memory 1 attr01    A1 starts   (k = 4: minutes 3, 4, 5, 6)
    3,4,5,6 Q memory 1                    A1
    7       CORRECTION memory 2 attr01    B1 starts   (k = 1: minute 8)
    8       Q memory 2                    B1
    9       UPDATE memory 1 attr02        A2 starts   (k = 1: minute 10)
    10      Q memory 1                    A2

Oracle = min(4,3) + min(1,3) + min(1,3) = 3 + 1 + 1 = 5 for every policy here.
Ticks run after processed events 4 (minute 4) and 8 (minute 8).
"""
from dataclasses import replace

import pytest

from verify_repair.evaluation import (
    ORACLE_STALE_ALLOWED, oracle_episode_cost, score_evolving_run, stale_allowed_episode_cost,
)
from verify_repair.experiments import (
    POLICY_NAMES, RESULT_FIELDS, STALE_USE_POLICIES, make_policy, read_rows, run_experiment, summarize_rows,
)
from verify_repair.models import ChangeType as CT
from verify_repair.policies.periodic_refresh import PeriodicRefresh
from verify_repair.policies.periodic_refresh_stale import PeriodicRefreshStale
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.evolving import (
    DEMAND_MODES, EXPIRY, EvolvingConfig, evolving_run_config, generate_evolving, load_evolving,
)
from verify_repair.simulator.handbuilt import workload_from_spec
from verify_repair.simulator.synthetic import SyntheticConfig, generate_synthetic


def handcheck():
    return workload_from_spec(
        changes=[(2, 1, 1, CT.CORRECTION, "birch", None), (7, 2, 1, CT.CORRECTION, "cedar", None),
                 (9, 1, 2, CT.UPDATE, "delta", None)],
        queries=[(1, 1, 1), (3, 1, 1), (4, 1, 1), (5, 1, 2), (6, 1, 1), (8, 2, 1), (10, 1, 2)],
        memories=2)


def score(workload, name, verify=1.0, repair=3.0, penalty=10.0, interval=4):
    db, ledger, summaries, retriever, events = load_evolving(workload)
    config = replace(evolving_run_config(workload), verify_cost_units=verify, repair_cost_units=repair)
    try:
        result = Simulator(ledger, summaries, retriever, make_policy(name, interval, 1), config).run(events)
    finally:
        db.close()
    return score_evolving_run(workload, result, verify, repair, penalty, allow_stale_use=name in STALE_USE_POLICIES)


# (verifications, repairs, stale uses, total cost, stale-exposure minutes, mean time-to-repair s, peak backlog)
HAND_CHECKED = {
    # Never repairs: 4 + 1 + 1 verifications. Memory 1 stale 2→60, memory 2 stale 7→60.
    "verify-only": (6, 0, 0, 6.0, 58 + 53, None, 2),
    # Repairs at each of the 3 changes, before any query sees staleness.
    "eager-refresh": (0, 3, 0, 9.0, 0, 0.0, 0),
    # Repairs on the first stale query of each episode, one minute after each change.
    "repair-on-first-access": (0, 3, 0, 9.0, 1 + 1 + 1, 60.0, 1),
    # Verifies minutes 3, 4 (tick repairs memory 1 after minute 4), 8 (tick repairs memory 2), 10.
    "periodic-refresh": (4, 2, 0, 10.0, 2 + 1 + 51, 90.0, 1),
    # One repair slot per tick; only one summary is stale at each tick, so same as periodic.
    "oldest-first": (4, 2, 0, 10.0, 2 + 1 + 51, 90.0, 1),
    # m = ceil(3/1) = 3: A1 verifies twice then repairs (minute 5); B1 and A2 verify once.
    "p1": (4, 1, 0, 7.0, 3 + 51 + 53, 180.0, 2),
    # Like P1 in A1 (forecast not confident until 3 past accesses); confident in A2, so it
    # repairs at minute 10 instead of verifying.
    "p2": (3, 2, 0, 9.0, 3 + 53 + 1, 120.0, 2),
    # Same ticks as periodic, but the 4 stale queries are answered stale: 2*3 + 4*10 = 46.
    "periodic-refresh-stale": (0, 2, 4, 46.0, 2 + 1 + 51, 90.0, 1),
}


@pytest.mark.parametrize("name", POLICY_NAMES)
def test_handcheck_trace_matches_paper_calculation(name):
    groups, episodes = score(handcheck(), name)
    total = groups[0]
    verify, repair, stale, cost, exposure_minutes, wait, peak = HAND_CHECKED[name]
    assert (total.verifications, total.repairs, total.stale_source_uses) == (verify, repair, stale)
    assert total.total_cost == cost and total.oracle_cost == 5.0 and total.regret == cost - 5.0
    assert total.episodes == 3 and total.queries == 6  # the fresh query is in no episode
    assert total.stale_exposure_hours * 60 == pytest.approx(exposure_minutes)
    assert total.mean_time_to_repair_s == wait
    assert total.peak_backlog == peak
    assert total.mean_backlog == pytest.approx(exposure_minutes / 60)  # one-hour trace
    assert [e.accesses for e in episodes] == [4, 1, 1]
    assert [e.oracle_cost for e in episodes] == [3.0, 1.0, 1.0]


def test_episode_counts_include_queries_after_a_repair():
    _, episodes = score(handcheck(), "repair-on-first-access")
    first = episodes[0]
    assert (first.accesses, first.repairs, first.verifications) == (4, 1, 0)  # 3 queries after the repair


@pytest.mark.parametrize("mode", DEMAND_MODES)
@pytest.mark.parametrize("name", POLICY_NAMES)
def test_no_policy_beats_the_hindsight_oracle_on_any_episode(mode, name):
    workload = generate_evolving(EvolvingConfig(num_memories=60, demand_mode=mode, changes_per_memory=4,
                                                facts_per_summary=(1, 4), fan_out=3, shared_fact_rate=0.4))
    for verify, repair in ((1.0, 2.0), (1.0, 10.0)):
        groups, episodes = score(workload, name, verify, repair, interval=50)
        assert all(e.total_cost >= e.oracle_cost - 1e-9 for e in episodes)
        assert groups[0].total_cost == pytest.approx(sum(e.total_cost for e in episodes))
        assert sum(g.episodes for g in groups[1:]) == groups[0].episodes  # groups partition episodes


def test_stale_use_oracle_semantics():
    assert stale_allowed_episode_cost(4, 1, 3, 10, "CORRECTION") == oracle_episode_cost(4, 1, 3) == 3
    assert stale_allowed_episode_cost(2, 1, 3, 0.5, "UPDATE") == 1.0  # answering stale is cheaper than verifying
    assert stale_allowed_episode_cost(5, 1, 3, 10, EXPIRY) == 0.0  # relaxed: may be correct again


def test_expiry_can_make_a_stale_summary_correct_again():
    """A summary built before a temporary exception is correct after it expires."""
    workload = workload_from_spec(changes=[(10, 1, 1, CT.TEMPORARY_EXCEPTION, "ember", 5)],
                                  queries=[(20, 1, 1), (30, 1, 1)])
    groups, episodes = score(workload, "periodic-refresh-stale", interval=100)
    assert [e.cause for e in episodes] == ["TEMPORARY_EXCEPTION", EXPIRY]
    assert groups[0].stale_source_uses == 0 and groups[0].total_cost == 0.0  # flagged stale, but evidence is current
    assert groups[0].relaxed_oracle_episodes == 1 and groups[0].oracle_cost == 0.0
    groups, _ = score(workload, "verify-only", interval=100)
    assert groups[0].total_cost == 2.0 == groups[0].oracle_cost  # verify/repair-only policies must still act


def test_periodic_refresh_default_is_unchanged_and_variant_is_separate():
    assert PeriodicRefresh().name == "periodic-refresh" and PeriodicRefreshStale().name == "periodic-refresh-stale"
    assert PeriodicRefresh().on_stale_access("s", "q").value == "VERIFY"
    assert PeriodicRefreshStale().on_stale_access("s", "q").value == "NO_OP"


def test_scorer_rejects_logs_from_a_different_trace():
    workload = handcheck()
    db, ledger, summaries, retriever, events = load_evolving(workload)
    result = Simulator(ledger, summaries, retriever, make_policy("verify-only"), evolving_run_config(workload)).run(events)
    db.close()
    other = workload_from_spec(changes=[(2, 1, 1, CT.CORRECTION, "birch", None)], queries=[(1, 1, 1)], memories=2)
    with pytest.raises(ValueError):
        score_evolving_run(other, result, 1.0, 3.0, 10.0)


def test_experiment_runner_scores_v2_and_keeps_v1(tmp_path):
    handcheck().write(tmp_path / "v2.json")
    generate_synthetic(SyntheticConfig(num_memories=6)).write(tmp_path / "v1.json")
    rows = list(run_experiment([tmp_path / "v2.json"], None, [(1.0, 3.0)], refresh_interval_events=4))
    everything = [row for row in rows if row["group"] == "all"]
    assert {row["policy"] for row in everything} == set(POLICY_NAMES)
    by_policy = {row["policy"]: row for row in everything}
    assert by_policy["periodic-refresh-stale"]["oracle"] == ORACLE_STALE_ALLOWED
    assert by_policy["p1"]["total_cost"] == 7.0 and by_policy["p1"]["workload_format"] == "synthetic-v2"
    assert all(set(row) == set(RESULT_FIELDS) for row in rows)
    v1 = list(run_experiment([tmp_path / "v1.json"], None, [(1.0, 5.0)]))
    assert "periodic-refresh-stale" not in {row["policy"] for row in v1}
    with pytest.raises(ValueError, match="synthetic-v2"):
        list(run_experiment([tmp_path / "v1.json"], ["periodic-refresh-stale"], [(1.0, 5.0)]))


def test_midterm_result_csvs_still_summarize():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "docs/midterm/results/balanced-10k"
    summary = summarize_rows(read_rows(sorted(root.glob("results-*.csv"))))
    assert summary and all(row["workload_format"] == "synthetic-v1" for row in summary)
