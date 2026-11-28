"""Measured-cost profiles, keyed replay draws, and measured-cost oracles (Step 6.4).

Run with ``python -m pytest -q tests/test_costs.py``; no models or network.
"""
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import product
from math import floor
from pathlib import Path

import pytest

from verify_repair.budget_oracle import EXACT, GREEDY, budget_oracle, episode_queries
from verify_repair.costs import (
    CostProfile, build_profile, read_cost_profile, repair_key, verify_key,
)
from verify_repair.evaluation import measured_episode_oracle, score_evolving_run
from verify_repair.experiments import POLICY_NAMES, STALE_USE_POLICIES, make_policy, run_experiment
from verify_repair.simulator.budget import RepairBudget
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.evolving import EXPIRY, evolving_run_config, horizon_end, load_evolving, read_evolving
from verify_repair.simulator.synthetic import START

from test_budget_oracle import random_trace

STABLE = Path(__file__).resolve().parents[1] / "data/synthetic_v2/stable-100-seed1.json"


def profile(profile_id="test-profile"):
    observations = (
        [{"operation": "verify", "fact_count": n % 3 + 1, "value": 0.5 + 0.25 * n} for n in range(12)]
        + [{"operation": "repair", "fact_count": n % 3 + 1, "value": 2.0 + n} for n in range(12)]
    )
    return build_profile(profile_id, "test_units", observations, {"note": "synthetic test samples"})


def replay(workload, name, cost_profile=None, budget=None):
    db, ledger, summaries, retriever, events = load_evolving(workload)
    config = evolving_run_config(workload)
    if cost_profile is not None:
        c_v, c_r = cost_profile.estimates()
        config = replace(config, verify_cost_units=c_v, repair_cost_units=c_r)
    try:
        return Simulator(ledger, summaries, retriever, make_policy(name, 10, 1), config,
                         cost_profile=cost_profile, budget=budget).run(events)
    finally:
        db.close()


def test_profile_round_trip_estimates_and_strata(tmp_path):
    original = profile()
    path = tmp_path / "profile.json"
    digest = original.write(path)
    loaded = read_cost_profile(path)
    assert loaded.sha256 == digest and loaded.samples == original.samples
    assert loaded.estimates() == pytest.approx((0.5 + 0.25 * 5.5, 2.0 + 5.5))
    assert len(loaded.values_for("verify", 2)) == 12  # 4-sample stratum < 5 -> pooled
    with pytest.raises(FileExistsError):
        original.write(path)


def test_profile_validation():
    with pytest.raises(ValueError, match="pooled samples"):
        CostProfile("p", "u", {"verify": {"all": (1.0,)}, "repair": {"all": ()}})
    with pytest.raises(ValueError, match="finite"):
        CostProfile("p", "u", {"verify": {"all": (-1.0,)}, "repair": {"all": (1.0,)}})
    with pytest.raises(ValueError, match="sorted"):
        CostProfile("p", "u", {"verify": {"all": (2.0, 1.0)}, "repair": {"all": (1.0,)}})


def test_strata_with_enough_samples_are_used():
    observations = ([{"operation": "verify", "fact_count": 2, "value": 9.0}] * 5
                    + [{"operation": "verify", "fact_count": 1, "value": 1.0}] * 5
                    + [{"operation": "repair", "fact_count": 2, "value": 4.0}])
    p = build_profile("p", "u", observations, {})
    assert {p.draw("verify", f"k{i}", 1, 2) for i in range(20)} == {9.0}
    assert {p.draw("verify", f"k{i}", 1, 1) for i in range(20)} == {1.0}
    assert {p.draw("verify", f"k{i}", 1, 4) for i in range(40)} == {1.0, 9.0}  # no stratum -> pooled


def test_draws_are_keyed_deterministic_and_spread():
    p = profile()
    assert p.draw("verify", "a", 7, 2) == p.draw("verify", "a", 7, 2)
    draws = {p.draw("verify", f"event{i}", 7) for i in range(200)}
    assert len(draws) > 6  # different keys sample different values
    # The seed enters the key: another seed reorders which event gets which sample.
    assert [p.draw("verify", f"event{i}", 7) for i in range(50)] != [
        p.draw("verify", f"event{i}", 8) for i in range(50)]


def test_replay_charges_shared_keyed_draws_and_reconciles():
    workload = read_evolving(STABLE)
    p = profile()
    seed = evolving_run_config(workload).seed
    facts = {spec["summary_id"]: len(spec["slots"]) for spec in workload.summaries}
    verify_charges, repair_charges = {}, {}
    for name in ("verify-only", "eager-refresh", "repair-on-first-access", "p1"):
        result = replay(workload, name, p)
        assert sum(r["verify_cost_units"] for r in result.logs) == pytest.approx(result.cumulative_verify_cost)
        assert sum(r["repair_cost_units"] for r in result.logs) == pytest.approx(result.cumulative_repair_cost)
        assert result.logs[0]["config_snapshot"]["cost_profile"]["profile_id"] == "test-profile"
        for row in result.logs:
            if row["action"] == "VERIFY":
                expected = p.draw("verify", verify_key(row["event_id"], row["summary_id"]), seed,
                                  facts[row["summary_id"]])
                assert row["verify_cost_units"] == expected
                verify_charges.setdefault((row["event_id"], row["summary_id"]), set()).add(expected)
            if row["action"] == "REPAIR":
                expected = p.draw("repair", repair_key(row["summary_id"], row["stale_episode_id"]), seed,
                                  facts[row["summary_id"]])
                assert row["repair_cost_units"] == expected
                repair_charges.setdefault((row["summary_id"], row["stale_episode_id"]), set()).add(expected)
    assert all(len(values) == 1 for values in [*verify_charges.values(), *repair_charges.values()])
    # Eager repairs at correction time and first-access repairs at a query share episode draws.
    assert any(len(v) == 1 for v in repair_charges.values())


def test_policies_decide_from_estimates_only():
    """Replay changes what is charged, not what P1 decides: actions match a fixed-cost run."""
    workload = read_evolving(STABLE)
    p = profile()
    c_v, c_r = p.estimates()
    measured = replay(workload, "p1", p)
    db, ledger, summaries, retriever, events = load_evolving(workload)
    config = replace(evolving_run_config(workload), verify_cost_units=c_v, repair_cost_units=c_r)
    try:
        fixed = Simulator(ledger, summaries, retriever, make_policy("p1"), config).run(events)
    finally:
        db.close()
    assert [r["action"] for r in measured.logs] == [r["action"] for r in fixed.logs]
    assert measured.cumulative_verify_cost != pytest.approx(fixed.cumulative_verify_cost)


@pytest.mark.parametrize("name", POLICY_NAMES)
def test_no_policy_beats_the_measured_oracle(name):
    workload = read_evolving(STABLE)
    p = profile()
    c_v, c_r = p.estimates()
    result = replay(workload, name, p)
    groups, episodes = score_evolving_run(
        workload, result, c_v, c_r, 10.0, allow_stale_use=name in STALE_USE_POLICIES,
        cost_profile=p, seed=evolving_run_config(workload).seed)
    assert all(e.total_cost >= e.oracle_cost - 1e-9 for e in episodes)
    assert all(g.regret >= -1e-9 for g in groups)


def test_measured_episode_oracle_matches_exhaustive_choice():
    for verify_costs in ([], [1.0], [0.5, 2.0, 0.25], [3.0, 3.0, 3.0, 3.0]):
        for repair in (0.0, 1.0, 4.0, 20.0):
            # Repair at query j (verifying the first j) or never.
            choices = [sum(verify_costs[:j]) + repair for j in range(len(verify_costs))]
            choices.append(sum(verify_costs))
            assert measured_episode_oracle(verify_costs, repair) == pytest.approx(min(choices))
    assert measured_episode_oracle([5.0, 5.0], 8.0, allow_stale_use=True, stale_penalty=1.0) == 2.0
    assert measured_episode_oracle([5.0], 8.0, allow_stale_use=True, stale_penalty=1.0, cause=EXPIRY) == 0.0


def measured_brute_force(workload, p, seed, budget, window):
    """Every repair instant per episode with keyed draws; knapsack checked per window."""
    queries = episode_queries(workload)
    facts = {spec["summary_id"]: len(spec["slots"]) for spec in workload.summaries}
    end = horizon_end(workload.config)
    choices = []
    for episode in workload.episodes:
        items = queries[episode.episode_id]
        q = [p.draw("verify", verify_key(e, episode.summary_id), seed, facts[episode.summary_id]) for _, e in items]
        r = p.draw("repair", repair_key(episode.summary_id, episode.episode_id), seed, facts[episode.summary_id])
        start = datetime.fromisoformat(episode.start_time)
        finish = datetime.fromisoformat(episode.end_time) if episode.end_time else end
        instants = {start, *(t for t, _ in items)}
        w = 0
        while START + timedelta(seconds=w * window) < finish:
            boundary = START + timedelta(seconds=w * window)
            if start < boundary:
                instants.add(boundary)
            w += 1
        options = [(None, 0.0, sum(q))]
        for instant in sorted(instants):
            before = sum(cost for (t, _), cost in zip(items, q) if t < instant)
            options.append((floor((instant - START).total_seconds() / window), r, before + r))
        choices.append(options)
    best = float("inf")
    for combo in product(*choices):
        used = {}
        for window_index, weight, _ in combo:
            if window_index is not None:
                used[window_index] = used.get(window_index, 0.0) + weight
        if budget is not None and any(spent > budget + 1e-9 for spent in used.values()):
            continue
        best = min(best, sum(cost for _, _, cost in combo))
    return best


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("budget", [0.0, 4.0, 9.0, None])
def test_measured_budget_oracle_matches_exhaustive_search(seed, budget):
    workload = random_trace(seed)
    p = profile()
    oracle = budget_oracle(workload, 0, 0, budget, 3600, cost_profile=p, seed=seed)
    assert oracle.total_cost == pytest.approx(measured_brute_force(workload, p, seed, budget, 3600))
    greedy = budget_oracle(workload, 0, 0, budget, 3600, cost_profile=p, seed=seed, method=GREEDY)
    assert greedy.total_cost >= oracle.total_cost - 1e-9


def test_no_budgeted_policy_beats_the_measured_budget_oracle():
    workload = read_evolving(STABLE)
    p = profile()
    seed = evolving_run_config(workload).seed
    oracle = budget_oracle(workload, 0, 0, 12.0, 3600, cost_profile=p, seed=seed, method=EXACT)
    for name in ("eager-refresh", "repair-on-first-access", "p1", "p2"):
        result = replay(workload, name, p, budget=RepairBudget(12.0, 3600, "predicted-savings", origin=START))
        groups, _ = score_evolving_run(workload, result, *p.estimates(), 10.0, cost_profile=p, seed=seed)
        total = next(g for g in groups if g.group == "all").total_cost
        assert total >= oracle.total_cost - 1e-9
        assert result.budget["max_window_spend"] <= 12.0 + 1e-9


def test_experiment_rows_carry_the_profile_and_are_reproducible():
    p = profile()
    rows = list(run_experiment([STABLE], ["verify-only", "p1"], [(1.0, 5.0)], cost_profile=p))
    again = list(run_experiment([STABLE], ["verify-only", "p1"], [(1.0, 5.0)], cost_profile=p))
    assert rows == again
    assert {row["cost_profile_id"] for row in rows} == {"test-profile"}
    assert {row["verify_cost"] for row in rows} == {p.estimates()[0]}
    assert all(row["regret"] >= -1e-9 for row in rows)
