"""Replay saved synthetic workloads under many policies and cost ratios.

``run_experiment`` replays every (workload, cost pair, policy) combination
on the same saved inputs and returns one CSV row per demand group.
``summarize_rows`` averages those rows across workloads (seeds) with 95%
t-confidence intervals. CLI: ``run-experiment`` and ``summarize-experiment``
(see README). Costs are nominal units; latency is wall-clock milliseconds of
the deterministic substrate, not of an LLM.
"""
import csv
import hashlib
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import asdict, replace
from math import sqrt
from pathlib import Path
from statistics import mean, stdev

from verify_repair.demand import PredictionConfig
from verify_repair.evaluation import (
    ORACLE_STALE_ALLOWED, ORACLE_VERIFY_REPAIR, score_evolving_run, score_synthetic_run,
)
from verify_repair.policies.accumulated_threshold import AccumulatedThreshold
from verify_repair.policies.eager_refresh import EagerRefresh
from verify_repair.policies.oldest_first import OldestFirst
from verify_repair.policies.periodic_refresh import PeriodicRefresh
from verify_repair.policies.periodic_refresh_stale import PeriodicRefreshStale
from verify_repair.policies.predicted_threshold import PredictedThreshold
from verify_repair.policies.repair_on_first_access import RepairOnFirstAccess
from verify_repair.policies.verify_only import VerifyOnly
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.evolving import (
    FORMAT_VERSION as EVOLVING_FORMAT, EvolvingWorkload, evolving_run_config, load_evolving, read_workload,
)
from verify_repair.simulator.synthetic import (
    FORMAT_VERSION as SYNTHETIC_FORMAT, load_synthetic, synthetic_run_config,
)

POLICY_NAMES = (
    "verify-only",
    "eager-refresh",
    "repair-on-first-access",
    "periodic-refresh",
    "oldest-first",
    "p1",
    "p2",
    "periodic-refresh-stale",
)
# Policies that may answer from a stale summary; scored with λ and the
# stale-allowed oracle (synthetic-v2 only).
STALE_USE_POLICIES = frozenset({"periodic-refresh-stale"})
DEFAULT_STALE_PENALTY = 10.0

# Settings and identifiers that define one replay; summaries group on these.
RUN_KEYS = (
    "workload_format", "memories", "policy", "verify_cost", "repair_cost", "cost_ratio",
    "stale_penalty", "oracle", "refresh_interval_events", "repair_slots_per_tick", "group",
)
# Columns added after the midterm; older CSVs without them still summarize.
LEGACY_DEFAULTS = {"workload_format": SYNTHETIC_FORMAT, "stale_penalty": "0.0",
                   "oracle": ORACLE_VERIFY_REPAIR}
METRICS = (
    "queries", "verifications", "repairs", "verify_cost_total", "repair_cost_total",
    "stale_source_uses", "total_cost", "oracle_cost", "regret", "cost_vs_oracle",
    "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
    "maintenance_latency_p95_ms", "no_maintenance_latency_p95_ms",
    "episodes", "stale_cost", "relaxed_oracle_episodes", "stale_exposure_hours",
    "repaired_episodes", "mean_time_to_repair_s", "p95_time_to_repair_s",
    "peak_backlog", "mean_backlog",
)
RESULT_FIELDS = (
    "dataset_id", "seed", "workload_sha256", "run_id", "config_hash", "trace_hash",
    *RUN_KEYS, "group_memories", *METRICS,
)

# Two-sided 95% Student t quantiles for 1-30 degrees of freedom.
T_95 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)


def make_policy(
    name: str,
    refresh_interval_events: int = 100,
    repair_slots_per_tick: int = 1,
    prediction_config: PredictionConfig | None = None,
):
    """Construct a policy by its CLI name; schedule options apply where used."""
    if name == "oldest-first":
        return OldestFirst(refresh_interval_events, repair_slots_per_tick)
    if name == "periodic-refresh":
        return PeriodicRefresh(refresh_interval_events)
    if name == "periodic-refresh-stale":
        return PeriodicRefreshStale(refresh_interval_events)
    if name == "p2":
        return PredictedThreshold(prediction_config or PredictionConfig())
    simple = {
        "verify-only": VerifyOnly,
        "eager-refresh": EagerRefresh,
        "repair-on-first-access": RepairOnFirstAccess,
        "p1": AccumulatedThreshold,
    }
    if name not in simple:
        raise ValueError(f"unknown policy {name!r}")
    return simple[name]()


def run_experiment(
    workload_paths: list[Path],
    policies: list[str] | None,
    cost_pairs: list[tuple[float, float]],
    *,
    refresh_interval_events: int = 100,
    repair_slots_per_tick: int = 1,
    measure_latency: bool = False,
    stale_penalty: float = DEFAULT_STALE_PENALTY,
) -> Iterator[dict]:
    """Yield result rows; every policy replays identical saved inputs.

    Accepts synthetic-v1 and synthetic-v2 workloads. ``policies=None`` runs
    every policy that applies to each workload's format (stale-use policies
    need v2 scoring). Demand labels and episode metadata are read only by the
    scorer after each replay finishes.
    """
    for name in policies or POLICY_NAMES:
        make_policy(name, refresh_interval_events, repair_slots_per_tick)  # fail before replay
    for path in workload_paths:
        raw = Path(path).read_bytes()
        workload = read_workload(path)
        evolving = isinstance(workload, EvolvingWorkload)
        names = list(policies) if policies is not None else [
            name for name in POLICY_NAMES if evolving or name not in STALE_USE_POLICIES]
        if not evolving and STALE_USE_POLICIES & set(names):
            raise ValueError("stale-use policies need synthetic-v2 workloads (multi-episode scoring)")
        base = evolving_run_config(workload) if evolving else synthetic_run_config(workload)
        for verify_cost, repair_cost in cost_pairs:
            config = replace(base, verify_cost_units=verify_cost, repair_cost_units=repair_cost)
            for name in names:
                policy = make_policy(name, refresh_interval_events, repair_slots_per_tick)
                db, ledger, summaries, retriever, events = (
                    load_evolving(workload) if evolving else load_synthetic(workload))
                try:
                    result = Simulator(ledger, summaries, retriever, policy, config,
                                       measure_latency=measure_latency).run(events)
                finally:
                    db.close()
                stale_use = name in STALE_USE_POLICIES
                if evolving:
                    scores, _ = score_evolving_run(workload, result, verify_cost, repair_cost, stale_penalty,
                                                   allow_stale_use=stale_use)
                else:
                    scores = score_synthetic_run(workload, result, verify_cost, repair_cost, stale_penalty)
                first = result.logs[0]
                for score in scores:
                    values = asdict(score)
                    yield {
                        "dataset_id": workload.config.dataset_id(),
                        "seed": workload.config.seed,
                        "workload_sha256": hashlib.sha256(raw).hexdigest(),
                        "run_id": first["run_id"],
                        "config_hash": first["config_hash"],
                        "trace_hash": first["trace_hash"],
                        "workload_format": EVOLVING_FORMAT if evolving else SYNTHETIC_FORMAT,
                        "memories": workload.config.num_memories,
                        "policy": policy.name,
                        "verify_cost": verify_cost,
                        "repair_cost": repair_cost,
                        "cost_ratio": repair_cost / verify_cost if verify_cost else None,
                        "stale_penalty": stale_penalty,
                        "oracle": ORACLE_STALE_ALLOWED if stale_use else ORACLE_VERIFY_REPAIR,
                        "refresh_interval_events": refresh_interval_events,
                        "repair_slots_per_tick": repair_slots_per_tick,
                        "group": score.group,
                        "group_memories": score.memories,
                        "verify_cost_total": values.pop("verify_cost"),
                        "repair_cost_total": values.pop("repair_cost"),
                        **{key: values[key] for key in METRICS if key in values},
                    }


def write_rows(path: Path, rows, fields) -> int:
    """Write a new CSV (never overwrite an earlier result); return row count."""
    count = 0
    with Path(path).open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row[key] is None else row[key] for key in fields})
            stream.flush()
            count += 1
    return count


def read_rows(paths: list[Path]) -> list[dict]:
    rows = []
    for path in paths:
        with Path(path).open(newline="", encoding="utf-8") as stream:
            rows.extend(csv.DictReader(stream))
    return rows


def confidence_interval(values: list[float]) -> tuple[float, float | None]:
    """Mean and 95% t half-width; the half-width is None for one sample."""
    if not values:
        raise ValueError("cannot summarize an empty sample")
    if len(values) == 1:
        return values[0], None
    t = T_95[len(values) - 2] if len(values) - 1 <= len(T_95) else 1.96
    return mean(values), t * stdev(values) / sqrt(len(values))


def summary_fields() -> tuple[str, ...]:
    stats = [f"{metric}_{part}" for metric in METRICS for part in ("mean", "ci95")]
    return (*RUN_KEYS, "seeds", *stats)


def summarize_rows(rows: list[dict]) -> list[dict]:
    """Average result rows across seeds for each setting, policy, and group."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[tuple(row.get(key, LEGACY_DEFAULTS.get(key)) for key in RUN_KEYS)].append(row)
    summary = []
    for key, members in grouped.items():
        seeds = [row["seed"] for row in members]
        if len(set(seeds)) != len(seeds):
            raise ValueError(f"duplicate seed for setting {key}")
        out = dict(zip(RUN_KEYS, key), seeds=len(members))
        for metric in METRICS:
            values = [float(row[metric]) for row in members if row.get(metric) not in ("", None)]
            if len(values) != len(members):
                out[f"{metric}_mean"] = out[f"{metric}_ci95"] = None
                continue
            out[f"{metric}_mean"], out[f"{metric}_ci95"] = confidence_interval(values)
        summary.append(out)
    return summary
