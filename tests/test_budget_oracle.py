"""Budget-aware hindsight oracle (Step 4).

Run with ``python -m pytest -q tests/test_budget_oracle.py``; no models or network.
The brute force below is independent of the oracle's reduction: it tries every
instant at which a repair could happen (episode start, every query, every
window start), checks the per-window budget directly, and simulates the cost.
"""
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import product
from math import floor
import random

import pytest

from verify_repair.budget_oracle import EXACT, GREEDY, budget_oracle, episode_query_times
from verify_repair.evaluation import score_evolving_run
from verify_repair.experiments import POLICY_NAMES, STALE_USE_POLICIES, make_policy, run_experiment
from verify_repair.models import ChangeType as CT
from verify_repair.simulator.budget import RepairBudget
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.evolving import (
    EXPIRY, VALUES, EvolvingConfig, evolving_run_config, generate_evolving, horizon_end, load_evolving,
)
from verify_repair.simulator.handbuilt import workload_from_spec
from verify_repair.simulator.synthetic import START


def brute_force(workload, cv, cr, budget, window, stale_penalty=float("inf"), allow_stale=False):
    times = episode_query_times(workload)
    end = horizon_end(workload.config)
    choices, per_query = [], {}
    for episode in workload.episodes:
        queries = times[episode.episode_id]
        if allow_stale and episode.cause == EXPIRY:
            choices.append([(episode.episode_id, None, 0.0)])
            continue
        q = min(cv, stale_penalty) if allow_stale else cv
        per_query[episode.episode_id] = q
        start = datetime.fromisoformat(episode.start_time)
        finish = datetime.fromisoformat(episode.end_time) if episode.end_time else end
        instants = {start, *queries}
        w = 0
        while START + timedelta(seconds=w * window) < finish:
            boundary = START + timedelta(seconds=w * window)
            if start < boundary:
                instants.add(boundary)
            w += 1
        options = [(episode.episode_id, None, q * len(queries))]
        for instant in sorted(instants):
            before = sum(t < instant for t in queries)
            window_index = floor((instant - START).total_seconds() / window)
            options.append((episode.episode_id, window_index, q * before + cr))
        choices.append(options)
    best = float("inf")
    for combo in product(*choices):
        used = {}
        for _, window_index, _ in combo:
            if window_index is not None:
                used[window_index] = used.get(window_index, 0) + cr
        if budget is not None and any(spent > budget + 1e-9 for spent in used.values()):
            continue
        best = min(best, sum(cost for _, _, cost in combo))
    return best


def random_trace(seed, memories=2, hours=3, max_changes=3, max_queries=8, temporary=False):
    rng = random.Random(seed)
    minutes = rng.sample(range(1, hours * 60), max_changes + max_queries)
    change_minutes = sorted(minutes[:rng.randint(1, max_changes)])
    query_minutes = minutes[max_changes:max_changes + rng.randint(0, max_queries)]
    current = {m: "amber" for m in range(1, memories + 1)}
    changes = []
    for minute in change_minutes:
        m = rng.randint(1, memories)
        value = rng.choice([v for v in VALUES if v != current[m]])
        if temporary and rng.random() < 0.4:
            changes.append((minute, m, 1, CT.TEMPORARY_EXCEPTION, value, 7.5))  # expires off-minute
        else:
            current[m] = value
            changes.append((minute, m, 1, CT.CORRECTION, value, None))
    queries = [(minute, rng.randint(1, memories), 1) for minute in query_minutes]
    return workload_from_spec(changes, queries, facts=1, memories=memories, hours=hours)


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("budget", [0, 3, 6, None])
def test_oracle_matches_exhaustive_search(seed, budget):
    workload = random_trace(seed)
    oracle = budget_oracle(workload, 1.0, 3.0, budget, 3600)
    assert oracle.total_cost == pytest.approx(brute_force(workload, 1.0, 3.0, budget, 3600))


@pytest.mark.parametrize("seed", range(20))
def test_oracle_matches_exhaustive_search_with_short_windows_and_stale_use(seed):
    workload = random_trace(seed, hours=2, temporary=True)
    for allow in (False, True):
        oracle = budget_oracle(workload, 1.0, 2.0, 2.0, 1200, stale_penalty=0.5, allow_stale_use=allow)
        assert oracle.total_cost == pytest.approx(
            brute_force(workload, 1.0, 2.0, 2.0, 1200, stale_penalty=0.5, allow_stale=allow))


def test_zero_demand_costs_nothing():
    workload = workload_from_spec([(10, 1, 1, CT.CORRECTION, "birch", None)], [], facts=1)
    assert budget_oracle(workload, 1.0, 3.0, 0, 3600).total_cost == 0.0


def test_tight_budget_repairs_the_episode_that_saves_most():
    # Two episodes start in hour 0; B = c_r allows one repair there. Memory 2 is asked 6 times,
    # memory 1 four times: repair memory 2 now (saves 3), and memory 1 at the hour-1 reset,
    # which still covers its two later queries (saves 2 - 3 < 0, so verify them instead).
    workload = workload_from_spec(
        changes=[(5, 1, 1, CT.CORRECTION, "birch", None), (6, 2, 1, CT.CORRECTION, "cedar", None)],
        queries=[(m, 1, 1) for m in (10, 20, 70, 80)] + [(m, 2, 1) for m in (11, 21, 31, 41, 51, 55)],
        facts=1, memories=2, hours=2)
    oracle = budget_oracle(workload, 1.0, 3.0, 3.0, 3600)
    assert oracle.chosen == {"memory_000002:change_000000002": 0}
    assert oracle.total_cost == 4 + 3 == brute_force(workload, 1.0, 3.0, 3.0, 3600)
    unlimited = budget_oracle(workload, 1.0, 3.0, None, 3600)
    assert unlimited.total_cost == 3 + 3


def test_repair_loses_value_before_the_next_reset():
    # The episode ends (new correction) before the window resets, so a deferred repair is worthless.
    workload = workload_from_spec(
        changes=[(1, 2, 1, CT.CORRECTION, "ember", None), (5, 1, 1, CT.CORRECTION, "birch", None),
                 (50, 1, 1, CT.CORRECTION, "cedar", None)],
        queries=[(m, 1, 1) for m in (10, 15, 20, 25, 30)] + [(m, 2, 1) for m in (2, 3, 4, 6, 7, 8, 9)],
        facts=1, memories=2, hours=2)
    oracle = budget_oracle(workload, 1.0, 3.0, 3.0, 3600)
    assert oracle.total_cost == brute_force(workload, 1.0, 3.0, 3.0, 3600)
    assert len(oracle.chosen) == 1  # only one of the two hour-0 episodes can be repaired


def test_unlimited_budget_equals_the_step2_oracle_and_zero_equals_verify_everything():
    workload = generate_evolving(EvolvingConfig(num_memories=200, changes_per_memory=4, fan_out=3,
                                                shared_fact_rate=0.4, demand_mode="shifting"))
    k = {e.episode_id: e.accesses for e in workload.episodes}
    unlimited = budget_oracle(workload, 1.0, 5.0, None)
    assert unlimited.per_episode == {e: min(n * 1.0, 5.0) for e, n in k.items()}
    huge = budget_oracle(workload, 1.0, 5.0, 1e9)
    assert huge.total_cost == unlimited.total_cost and huge.status == "budget-not-binding"
    assert budget_oracle(workload, 1.0, 5.0, 0).total_cost == sum(k.values())


def test_greedy_is_a_feasible_upper_bound_and_is_labeled_a_heuristic():
    workload = generate_evolving(EvolvingConfig(num_memories=300, changes_per_memory=4, demand_mode="bursty"))
    exact = budget_oracle(workload, 1.0, 5.0, 10.0)
    greedy = budget_oracle(workload, 1.0, 5.0, 10.0, method=GREEDY)
    assert exact.method == EXACT and exact.status == "optimal" and exact.mip_gap == pytest.approx(0, abs=1e-6)
    assert greedy.method == GREEDY and greedy.total_cost >= exact.total_cost - 1e-9
    assert all(count <= 2 for count in exact.repairs_per_window.values())  # floor(10 / 5)
    with pytest.raises(ValueError):
        budget_oracle(workload, 1.0, 5.0, 10.0, method="lp")


@pytest.mark.parametrize("name", POLICY_NAMES)
@pytest.mark.parametrize("units", [0, 5, 15])
def test_no_budgeted_policy_beats_the_budget_oracle(name, units):
    workload = generate_evolving(EvolvingConfig(num_memories=50, demand_mode="bursty", changes_per_memory=4,
                                                facts_per_summary=(1, 4), fan_out=3, shared_fact_rate=0.4,
                                                horizon_hours=6))
    db, ledger, summaries, retriever, events = load_evolving(workload)
    config = replace(evolving_run_config(workload), verify_cost_units=1.0, repair_cost_units=5.0)
    try:
        result = Simulator(ledger, summaries, retriever, make_policy(name, 50, 1), config,
                           budget=RepairBudget(units, 1800, origin=START)).run(events)
    finally:
        db.close()
    stale = name in STALE_USE_POLICIES
    groups, _ = score_evolving_run(workload, result, 1.0, 5.0, 10.0, allow_stale_use=stale)
    oracle = budget_oracle(workload, 1.0, 5.0, units, 1800, stale_penalty=10.0, allow_stale_use=stale)
    assert groups[0].total_cost >= oracle.total_cost - 1e-9
    assert oracle.total_cost >= groups[0].oracle_cost - 1e-9  # a budget can only raise the bound


def test_experiment_rows_carry_budget_matched_regret(tmp_path):
    random_trace(3).write(tmp_path / "w.json")
    rows = list(run_experiment([tmp_path / "w.json"], ["p1", "periodic-refresh-stale"], [(1.0, 3.0)],
                               budget_units=3, refresh_interval_events=4))
    for row in rows:
        assert row["budget_oracle_method"] == EXACT
        assert row["budget_regret"] == pytest.approx(row["total_cost"] - row["budget_oracle_cost"])
        assert row["budget_regret"] >= -1e-9
    plain = list(run_experiment([tmp_path / "w.json"], ["p1"], [(1.0, 3.0)]))
    assert all(row["budget_oracle_cost"] is None for row in plain)
