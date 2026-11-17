"""Shared repair budget B per reset window and the deferred-repair queue (Step 3).

Run with ``python -m pytest -q tests/test_budget.py``; no models or network.
Tiny traces use one-hour windows from the trace start, c_v = 1 and c_r = 3.
"""
from collections import defaultdict
from dataclasses import replace

import pytest

from verify_repair.evaluation import score_evolving_run
from verify_repair.experiments import POLICY_NAMES, STALE_USE_POLICIES, make_policy, run_experiment
from verify_repair.maintenance.operations import MaintenanceService
from verify_repair.models import ChangeType as CT
from verify_repair.policies.base import MaintenanceAction
from verify_repair.simulator.budget import BudgetState, RepairBudget
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.evolving import EvolvingConfig, evolving_run_config, generate_evolving, load_evolving
from verify_repair.simulator.handbuilt import workload_from_spec
from verify_repair.simulator.synthetic import START


def run(workload, policy, units=None, window=3600.0, order="predicted-savings", verify=1.0, repair=3.0):
    db, ledger, summaries, retriever, events = load_evolving(workload)
    config = replace(evolving_run_config(workload), verify_cost_units=verify, repair_cost_units=repair)
    budget = None if units is None else RepairBudget(units, window, order, origin=START)
    if isinstance(policy, str):
        policy = make_policy(policy, 4, 1)
    try:
        return Simulator(ledger, summaries, retriever, policy, config, budget=budget).run(events)
    finally:
        db.close()


def two_memories_compete(hours=3.0):
    """Both memories change in hour 0 and are queried in hours 0 and 1."""
    return workload_from_spec(
        changes=[(10, 1, 1, CT.CORRECTION, "birch", None), (20, 2, 1, CT.CORRECTION, "cedar", None)],
        queries=[(30, 1, 1), (40, 2, 1), (70, 1, 1), (80, 2, 1), (130, 1, 1)],
        memories=2, hours=hours)


def repairs_by_window(result):
    spent = defaultdict(float)
    for row in result.logs:
        if row["action"] == "REPAIR":
            spent[row["budget_window"]] += row["repair_cost_units"]
    return dict(spent)


def test_zero_budget_never_repairs_and_verifies_instead():
    result = run(two_memories_compete(), "repair-on-first-access", units=0)
    assert result.repairs == 0 and result.verifications == 5
    assert result.budget["deferrals"] == 5 and result.budget["still_queued"] == 2
    assert all(row["budget_outcome"] == "deferred" for row in result.logs if row["action"] == "VERIFY")


def test_tight_budget_defers_the_second_repair_to_the_next_window():
    # B = c_r: eager refresh repairs memory 1 at minute 10; memory 2 (minute 20) must wait.
    result = run(two_memories_compete(), "eager-refresh", units=3)
    assert repairs_by_window(result) == {0: 3.0, 1: 3.0}
    queued = [row for row in result.logs if row["maintenance_trigger"] == "deferred_queue"]
    assert [(row["summary_id"], row["event_timestamp"][11:16]) for row in queued] == [("memory_000002", "01:10")]
    assert result.budget["mean_wait_s"] == 50 * 60  # queued at minute 20, drained at minute 70
    assert result.verifications == 1  # memory 2's minute-40 query is verified while it waits


def test_exact_fit_budget_spends_everything_without_deferral():
    result = run(two_memories_compete(), "eager-refresh", units=6)
    assert result.repairs == 2 and result.budget["deferrals"] == 0
    assert result.logs[-1]["budget_remaining"] == 6  # a later window starts fresh
    assert repairs_by_window(result) == {0: 6.0}


@pytest.mark.parametrize("name", POLICY_NAMES)
def test_ample_budget_matches_unbudgeted_runs(name):
    workload = generate_evolving(EvolvingConfig(num_memories=40, changes_per_memory=3, facts_per_summary=(1, 4)))
    unlimited, ample = run(workload, name), run(workload, name, units=1e9)
    assert (ample.verifications, ample.repairs, ample.stale_source_uses) == (
        unlimited.verifications, unlimited.repairs, unlimited.stale_source_uses)
    assert ample.budget["deferrals"] == 0
    strip = {"budget_window", "budget_remaining", "budget_outcome", "config_snapshot", "config_hash", "run_id"}
    assert [{k: v for k, v in row.items() if k not in strip} for row in ample.logs] == [
        {k: v for k, v in row.items() if k not in strip} for row in unlimited.logs]


def test_unbudgeted_logs_have_no_budget_fields():
    result = run(two_memories_compete(), "p1")
    assert result.budget is None and all("budget_outcome" not in row for row in result.logs)
    assert "budget" not in result.logs[0]["config_snapshot"]


def test_new_correction_retags_a_queued_request_to_the_new_episode():
    workload = workload_from_spec(
        changes=[(10, 1, 1, CT.CORRECTION, "birch", None), (20, 2, 1, CT.CORRECTION, "cedar", None),
                 (30, 2, 2, CT.UPDATE, "delta", None)],
        queries=[(70, 2, 1)], memories=2, hours=2.0)
    result = run(workload, "eager-refresh", units=3)
    assert result.budget["requeued"] == 1
    drained = [row for row in result.logs if row["maintenance_trigger"] == "deferred_queue"]
    assert [row["stale_episode_id"] for row in drained] == ["memory_000002:change_000000003"]
    score_evolving_run(workload, result, 1.0, 3.0, 10.0)  # engine and trace episodes still agree


def test_predicted_savings_order_repairs_the_busier_memory_first():
    # B = c_r. Memory 3's change at minute 1 uses the hour-0 budget, so memories 1 (minute 5)
    # and 2 (minute 10) are both deferred. Memory 1 is older but asked once; memory 2 is asked
    # every few minutes. At the hour-1 reset there is room for exactly one repair.
    workload = workload_from_spec(
        changes=[(1, 3, 1, CT.CORRECTION, "ember", None), (5, 1, 1, CT.CORRECTION, "birch", None),
                 (10, 2, 1, CT.CORRECTION, "cedar", None)],
        queries=[(15, 1, 1)] + [(m, 2, 1) for m in (12, 20, 25, 30, 35, 40, 45, 50, 55)] + [(65, 2, 1)],
        memories=3, hours=2.0)
    first = {}
    for order in ("predicted-savings", "oldest"):
        result = run(workload, "eager-refresh", units=3, order=order)
        drained = [row["summary_id"] for row in result.logs if row["maintenance_trigger"] == "deferred_queue"]
        assert len(drained) == 1 and result.budget["still_queued"] == 1
        first[order] = drained[0]
    assert first == {"predicted-savings": "memory_000002", "oldest": "memory_000001"}


def test_invalid_batch_is_rejected_before_any_repair_runs():
    class BadBatch:
        name, refresh_interval_events = "bad", None

        def on_correction(self, affected):
            return [(affected[0], MaintenanceAction.REPAIR), (affected[0], MaintenanceAction.VERIFY)]

        def on_stale_access(self, *args, **kwargs):
            return MaintenanceAction.VERIFY

    workload = two_memories_compete()
    db, ledger, summaries, retriever, events = load_evolving(workload)
    try:
        with pytest.raises(ValueError, match="only schedule REPAIR"):
            Simulator(ledger, summaries, retriever, BadBatch(), evolving_run_config(workload),
                      budget=RepairBudget(100, origin=START)).run(events)
        assert summaries.get_summary("memory_000001").is_stale  # nothing executed
    finally:
        db.close()


def test_failed_repair_is_refunded_and_propagates(monkeypatch):
    state = BudgetState(RepairBudget(6), 3.0, START)
    state.roll(START)
    state.reserve()
    state.refund()
    assert (state.remaining, state.spent) == (6.0, 0.0)

    def explode(self, summary_id, at_time):
        raise RuntimeError("repair failed")

    monkeypatch.setattr(MaintenanceService, "repair", explode)
    with pytest.raises(RuntimeError, match="repair failed"):
        run(two_memories_compete(), "eager-refresh", units=3)


@pytest.mark.parametrize("name", POLICY_NAMES)
@pytest.mark.parametrize("units", [0, 5, 20])
def test_no_window_overspends_and_scores_reconcile(name, units):
    workload = generate_evolving(EvolvingConfig(num_memories=60, demand_mode="bursty", changes_per_memory=4,
                                                facts_per_summary=(1, 4), fan_out=3, shared_fact_rate=0.4,
                                                horizon_hours=6))
    result = run(workload, name, units=units, window=1800, repair=5.0)
    assert all(spent <= units + 1e-9 for spent in repairs_by_window(result).values())
    assert all(row["budget_remaining"] >= -1e-9 for row in result.logs)
    groups, _ = score_evolving_run(workload, result, 1.0, 5.0, 10.0, allow_stale_use=name in STALE_USE_POLICIES)
    assert groups[0].repairs == result.repairs
    again = run(workload, name, units=units, window=1800, repair=5.0)
    assert again.logs == result.logs  # deterministic replay


def test_budget_validation():
    for bad in ({"units": -1}, {"units": float("inf")}, {"units": 1, "window_seconds": 0},
                {"units": 1, "queue_order": "random"}):
        with pytest.raises(ValueError):
            RepairBudget(**bad)


def test_experiment_runner_reports_budget_columns(tmp_path):
    two_memories_compete().write(tmp_path / "w.json")
    rows = [row for row in run_experiment([tmp_path / "w.json"], ["eager-refresh"], [(1.0, 3.0)], budget_units=3)
            if row["group"] == "all"]
    # Memory 2's correction-time repair is deferred, then refused again at its minute-40 query.
    assert rows[0]["budget_units"] == 3 and rows[0]["deferrals"] == 2 and rows[0]["executed_from_queue"] == 1
