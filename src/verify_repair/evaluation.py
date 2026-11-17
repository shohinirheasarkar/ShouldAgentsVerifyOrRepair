"""Evaluator-only scoring of synthetic replays against a hindsight oracle.

Inputs: a ``synthetic-v1`` workload (with its evaluator-only ``demands``) and
the ``RunResult`` of replaying it. Output: per-group cost, oracle cost, regret,
and optional latency percentiles. Runs only after replay; nothing here is
passed to a policy. Costs are nominal units, not measurements.

Each synthetic memory has exactly one stale episode (one correction before all
of its queries), so the hindsight-optimal cost for a memory with ``k`` future
queries is ``min(k * c_v, c_r)``: verify every query, or repair on the first.
This formula does not apply to traces with repeated corrections or a budget;
``score_evolving_run`` below scores ``synthetic-v2`` traces episode by episode.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from math import ceil, isclose

from verify_repair.models import stamp
from verify_repair.simulator.engine import RunResult
from verify_repair.simulator.evolving import EXPIRY, EvolvingWorkload, horizon_end
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
    # synthetic-v2 only (None for v1): see score_evolving_run.
    episodes: int | None = None
    stale_cost: float | None = None
    relaxed_oracle_episodes: int | None = None
    stale_exposure_hours: float | None = None
    repaired_episodes: int | None = None
    mean_time_to_repair_s: float | None = None
    p95_time_to_repair_s: float | None = None
    peak_backlog: int | None = None
    mean_backlog: float | None = None
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


# ---------------------------------------------------------------------------
# synthetic-v2: repeated source-change episodes
# ---------------------------------------------------------------------------

ORACLE_VERIFY_REPAIR = "verify-repair"
ORACLE_STALE_ALLOWED = "verify-repair-stale"


def stale_allowed_episode_cost(future_queries: int, verify_cost: float, repair_cost: float,
                               stale_penalty: float, cause: str) -> float:
    """Hindsight lower bound for one episode when answering stale is also allowed.

    Each query is either verified (c_v) or answered stale (λ), so the cheaper
    of the two is paid per query unless one repair (c_r) covers them all. An
    expiry episode can be free: a summary last built before a temporary
    exception becomes correct again when the exception expires, so its stale
    answers are not stale uses. The bound for expiry episodes is therefore 0
    (a relaxed but valid lower bound); scores report how many were relaxed.
    """
    if cause == EXPIRY:
        return 0.0
    return min(future_queries * min(verify_cost, stale_penalty), repair_cost)


@dataclass(frozen=True)
class EpisodeScore:
    """Per-episode accounting for one replay (evaluator-only)."""

    summary_id: str
    episode_id: str
    group: str
    cause: str
    accesses: int
    verifications: int
    repairs: int
    stale_uses: int
    verify_cost: float
    repair_cost: float
    stale_cost: float
    total_cost: float
    oracle_cost: float
    stale_seconds: float
    time_to_repair_s: float | None


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def score_evolving_run(
    workload: EvolvingWorkload,
    result: RunResult,
    verify_cost: float,
    repair_cost: float,
    stale_penalty: float,
    *,
    allow_stale_use: bool = False,
) -> tuple[list[GroupScore], list[EpisodeScore]]:
    """Score one replay of a synthetic-v2 trace, episode by episode.

    Episodes run from a source change (or effective expiry) to the next one,
    so ``k`` counts every query in the episode, including queries after a
    repair. Costs on VERIFY/REPAIR rows are attributed through the engine's
    ``stale_episode_id``; query accesses are attributed by event order, and the
    two must agree. Total cost is c_v*N_v + c_r*N_r + λ*N_stale.

    The oracle is ``min(k*c_v, c_r)`` per episode for policies that always
    VERIFY or REPAIR a stale summary, and ``stale_allowed_episode_cost`` for
    policies that may answer from a stale summary (``allow_stale_use``). Both
    hold here because synthetic-v2 summaries are independent (no summary-to-
    summary dependencies) and each query retrieves exactly one summary.
    Every count is reconciled against the run totals; mismatches raise.
    """
    if stale_penalty < 0:
        raise ValueError("stale penalty must be nonnegative")
    events = {event.event_id: event for event in workload.events}

    def key_of(event_id: str) -> tuple[str, int]:
        base = event_id[:-len(":expiry")] if event_id.endswith(":expiry") else event_id
        if base not in events:
            raise ValueError(f"log row references event {event_id} that is not in this workload")
        event = events[base]
        if base != event_id:
            return stamp(event.valid_to), -1
        return stamp(event.timestamp), event.sequence_number

    profiles = {profile.summary_id: profile for profile in workload.memories}
    episodes = {episode.episode_id: episode for episode in workload.episodes}
    order = defaultdict(list)  # summary -> [(start key, episode_id)]
    for episode in workload.episodes:
        order[episode.summary_id].append((key_of(episode.episode_id.split(":", 1)[1]), episode.episode_id))
    for rows in order.values():
        rows.sort()
    starts = {summary_id: [key for key, _ in rows] for summary_id, rows in order.items()}

    def locate(summary_id: str, key) -> str | None:
        position = bisect_right(starts.get(summary_id, []), key) - 1
        return order[summary_id][position][1] if position >= 0 else None

    def group_at(summary_id: str, when: str) -> str:
        phase = bisect_right(workload.phase_starts, when) - 1
        return profiles[summary_id].groups[phase]

    queries = Counter()
    fresh = Counter()
    verifications = Counter()
    repairs = Counter()
    spent_verify = defaultdict(float)
    spent_repair = defaultdict(float)
    stale = Counter()
    first_repair = {}
    query_rows = defaultdict(list)
    for row in result.logs:
        summary_id = row["summary_id"]
        if summary_id is None:
            if row["event_type"] == "query":
                raise ValueError(f"query {row['query_id']} retrieved no summary")
            continue
        if summary_id not in profiles:
            raise ValueError(f"log row references unknown summary {summary_id}")
        key = key_of(row["event_id"])
        current = locate(summary_id, key)
        if row["event_type"] == "query" and row["maintenance_trigger"] is None:
            query_rows[row["query_id"]].append(row)
            if current is None:
                fresh[summary_id] += 1
            else:
                queries[current] += 1
        if row["action"] != "NO_OP" or row["used_stale_source"]:
            if row["stale_episode_id"] != current or current is None:
                raise ValueError(f"engine episode {row['stale_episode_id']} disagrees with trace episode {current}")
        verifications[current] += row["action"] == "VERIFY"
        repairs[current] += row["action"] == "REPAIR"
        spent_verify[current] += row["verify_cost_units"]
        spent_repair[current] += row["repair_cost_units"]
        stale[current] += bool(row["used_stale_source"])
        if row["action"] == "REPAIR" and current not in first_repair:
            first_repair[current] = _parse(row["event_timestamp"])

    for episode in workload.episodes:
        if queries[episode.episode_id] != episode.accesses:
            raise ValueError(f"replay logs do not match accesses for {episode.episode_id}")
    for profile in workload.memories:
        if fresh[profile.summary_id] != profile.fresh_queries:
            raise ValueError(f"replay logs do not match fresh queries for {profile.summary_id}")
    if (sum(verifications.values()) != result.verifications or sum(repairs.values()) != result.repairs
            or sum(stale.values()) != result.stale_source_uses
            or not isclose(sum(spent_verify.values()), result.cumulative_verify_cost)
            or not isclose(sum(spent_repair.values()), result.cumulative_repair_cost)):
        raise ValueError("per-episode log totals disagree with the run totals")
    if None in verifications and (verifications[None] or repairs[None] or stale[None]):
        raise ValueError("maintenance was logged outside any episode")

    # Stale intervals: a summary turns stale at an episode start and fresh at
    # its first repair; unrepaired staleness carries into the next episode.
    end_of_trace = horizon_end(workload.config)
    scores, intervals, waits = [], [], []
    for summary_id, rows in order.items():
        stale_since = None
        for position, (_, episode_id) in enumerate(rows):
            episode = episodes[episode_id]
            begin = _parse(episode.start_time)
            finish = _parse(episode.end_time) if episode.end_time else end_of_trace
            if stale_since is None:
                stale_since = begin
            repaired = first_repair.get(episode_id)
            stale_until = repaired or finish
            wait = (repaired - stale_since).total_seconds() if repaired else None
            if repaired:
                intervals.append((stale_since, repaired))
                waits.append(wait)
                stale_since = None
            elif episode.end_time is None:
                intervals.append((stale_since, end_of_trace))
            k = episode.accesses
            if allow_stale_use:
                oracle = stale_allowed_episode_cost(k, verify_cost, repair_cost, stale_penalty, episode.cause)
            else:
                oracle = oracle_episode_cost(k, verify_cost, repair_cost)
            stale_cost = stale_penalty * stale[episode_id]
            scores.append(EpisodeScore(
                summary_id=summary_id, episode_id=episode_id,
                group=group_at(summary_id, episode.start_time), cause=episode.cause, accesses=k,
                verifications=verifications[episode_id], repairs=repairs[episode_id],
                stale_uses=stale[episode_id], verify_cost=spent_verify[episode_id],
                repair_cost=spent_repair[episode_id], stale_cost=stale_cost,
                total_cost=spent_verify[episode_id] + spent_repair[episode_id] + stale_cost,
                oracle_cost=oracle, stale_seconds=(stale_until - begin).total_seconds(),
                time_to_repair_s=wait,
            ))

    # Backlog: number of stale summaries over time (time-weighted mean, peak).
    sweep = sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals],
                   key=lambda item: (item[0], item[1]))
    level = peak = 0
    area = 0.0
    previous = workload_start = _parse(workload.phase_starts[0])
    for when, delta in sweep:
        area += level * (when - previous).total_seconds()
        level += delta
        peak = max(peak, level)
        previous = when
    area += level * (end_of_trace - previous).total_seconds()
    mean_backlog = area / (end_of_trace - workload_start).total_seconds()

    latencies = defaultdict(list)
    for rows in query_rows.values():
        latency = rows[0]["latency_ms_instrumentation_only"]
        if latency is None:
            continue
        maintained = any(row["action"] != "NO_OP" for row in rows)
        group = group_at(rows[0]["summary_id"], rows[0]["event_timestamp"])
        for name in ("all", group):
            latencies[name, None].append(latency)
            latencies[name, maintained].append(latency)

    groups = []
    for group in GROUPS:
        members = [score for score in scores if group in ("all", score.group)]
        repaired = [score.time_to_repair_s for score in members if score.time_to_repair_s is not None]
        verify_total = sum(score.verify_cost for score in members)
        repair_total = sum(score.repair_cost for score in members)
        stale_total = sum(score.stale_cost for score in members)
        total = verify_total + repair_total + stale_total
        oracle = sum(score.oracle_cost for score in members)
        sample = latencies[group, None]
        groups.append(GroupScore(
            group=group,
            memories=len({score.summary_id for score in members}),
            queries=sum(score.accesses for score in members),
            verifications=sum(score.verifications for score in members),
            repairs=sum(score.repairs for score in members),
            verify_cost=verify_total,
            repair_cost=repair_total,
            stale_source_uses=sum(score.stale_uses for score in members),
            total_cost=total,
            oracle_cost=oracle,
            regret=total - oracle,
            cost_vs_oracle=total / oracle if oracle > 0 else None,
            episodes=len(members),
            stale_cost=stale_total,
            relaxed_oracle_episodes=(sum(score.cause == EXPIRY for score in members)
                                     if allow_stale_use else 0),
            stale_exposure_hours=sum(score.stale_seconds for score in members) / 3600,
            repaired_episodes=len(repaired),
            mean_time_to_repair_s=sum(repaired) / len(repaired) if repaired else None,
            p95_time_to_repair_s=percentile(repaired, 95),
            peak_backlog=peak if group == "all" else None,
            mean_backlog=mean_backlog if group == "all" else None,
            latency_p50_ms=percentile(sample, 50),
            latency_p95_ms=percentile(sample, 95),
            latency_p99_ms=percentile(sample, 99),
            maintenance_latency_p95_ms=percentile(latencies[group, True], 95),
            no_maintenance_latency_p95_ms=percentile(latencies[group, False], 95),
        ))
    return groups, scores
