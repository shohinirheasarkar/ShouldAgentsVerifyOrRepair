"""Evaluator-only scoring of synthetic replays against a hindsight oracle.

Inputs: a ``synthetic-v1`` workload (with its evaluator-only ``demands``) and
the ``RunResult`` of replaying it. Output: per-group cost, oracle cost, regret,
and optional latency percentiles. Runs only after replay; nothing here is
passed to a policy. Costs are nominal units, not measurements.

Each synthetic memory has exactly one stale episode (one correction before all
of its queries), so the hindsight-optimal cost for a memory with ``k`` future
queries is ``min(k * c_v, c_r)``: verify every query, or repair on the first.
This formula does not apply to traces with repeated corrections or a budget.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from math import ceil, isclose

from verify_repair.simulator.engine import RunResult
from verify_repair.simulator.synthetic import GROUP_BOUNDS, SyntheticWorkload

GROUPS = ("all", *GROUP_BOUNDS)


def oracle_episode_cost(future_queries: int, verify_cost: float, repair_cost: float) -> float:
    """Hindsight-optimal cost for one stale episode with known future queries."""
    if type(future_queries) is not int or future_queries < 0:
        raise ValueError("future_queries must be a nonnegative integer")
    return min(future_queries * verify_cost, repair_cost)


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in (0, 100]); None for an empty sample."""
    if not 0 < q <= 100:
        raise ValueError("percentile must lie in (0, 100]")
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(ceil(q / 100 * len(ordered)), 1) - 1]


@dataclass(frozen=True)
class GroupScore:
    """Totals for one demand group ("all", "cold", "warm", or "hot")."""

    group: str
    memories: int
    queries: int
    verifications: int
    repairs: int
    verify_cost: float
    repair_cost: float
    stale_source_uses: int
    total_cost: float
    oracle_cost: float
    regret: float
    # C(policy) / C(oracle); None when the oracle spends nothing.
    cost_vs_oracle: float | None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    latency_p99_ms: float | None = None
    maintenance_latency_p95_ms: float | None = None
    no_maintenance_latency_p95_ms: float | None = None


def score_synthetic_run(
    workload: SyntheticWorkload,
    result: RunResult,
    verify_cost: float,
    repair_cost: float,
    stale_penalty: float = 0.0,
) -> list[GroupScore]:
    """Score one replay; total cost is c_v*N_v + c_r*N_r + penalty*N_stale.

    Fails loudly if the logs do not match the workload's exact demands.
    """
    group_of = {demand.summary_id: demand.group for demand in workload.demands}
    queries = Counter()
    verifications = Counter()
    repairs = Counter()
    spent_verify = defaultdict(float)
    spent_repair = defaultdict(float)
    stale = Counter()
    query_rows = defaultdict(list)
    for row in result.logs:
        summary_id = row["summary_id"]
        if summary_id is None:
            if row["event_type"] == "query":
                raise ValueError(f"query {row['query_id']} retrieved no summary")
            continue
        if summary_id not in group_of:
            raise ValueError(f"log row references unknown summary {summary_id}")
        # Scheduled repairs are logged under the event that triggered the
        # tick, which may be a query; they are not query accesses.
        if row["event_type"] == "query" and row["maintenance_trigger"] is None:
            queries[summary_id] += 1
            query_rows[row["query_id"]].append(row)
        verifications[summary_id] += row["action"] == "VERIFY"
        repairs[summary_id] += row["action"] == "REPAIR"
        spent_verify[summary_id] += row["verify_cost_units"]
        spent_repair[summary_id] += row["repair_cost_units"]
        stale[summary_id] += bool(row["used_stale_source"])

    for demand in workload.demands:
        if queries[demand.summary_id] != demand.future_queries:
            raise ValueError("replay logs do not match the workload's future query counts")
    if (sum(verifications.values()) != result.verifications or sum(repairs.values()) != result.repairs
            or not isclose(sum(spent_verify.values()), result.cumulative_verify_cost)
            or not isclose(sum(spent_repair.values()), result.cumulative_repair_cost)):
        raise ValueError("per-summary log totals disagree with the run totals")

    latencies = defaultdict(list)
    for rows in query_rows.values():
        latency = rows[0]["latency_ms_instrumentation_only"]
        if latency is None:
            continue
        maintained = any(row["action"] != "NO_OP" for row in rows)
        for group in ("all", group_of[rows[0]["summary_id"]]):
            latencies[group, None].append(latency)
            latencies[group, maintained].append(latency)

    scores = []
    for group in GROUPS:
        members = [d for d in workload.demands if group in ("all", d.group)]
        ids = [d.summary_id for d in members]
        verify_total = sum(spent_verify[i] for i in ids)
        repair_total = sum(spent_repair[i] for i in ids)
        stale_total = sum(stale[i] for i in ids)
        total = verify_total + repair_total + stale_penalty * stale_total
        oracle = sum(oracle_episode_cost(d.future_queries, verify_cost, repair_cost) for d in members)
        sample = latencies[group, None]
        scores.append(GroupScore(
            group=group,
            memories=len(members),
            queries=sum(queries[i] for i in ids),
            verifications=sum(verifications[i] for i in ids),
            repairs=sum(repairs[i] for i in ids),
            verify_cost=verify_total,
            repair_cost=repair_total,
            stale_source_uses=stale_total,
            total_cost=total,
            oracle_cost=oracle,
            regret=total - oracle,
            cost_vs_oracle=total / oracle if oracle > 0 else None,
            latency_p50_ms=percentile(sample, 50),
            latency_p95_ms=percentile(sample, 95),
            latency_p99_ms=percentile(sample, 99),
            maintenance_latency_p95_ms=percentile(latencies[group, True], 95),
            no_maintenance_latency_p95_ms=percentile(latencies[group, False], 95),
        ))
    return scores
