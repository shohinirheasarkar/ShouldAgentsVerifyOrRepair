"""Budget-aware hindsight oracle for synthetic-v2 traces (Step 4).

The oracle knows every future query and may repair a stale summary at any
instant, but it spends the same shared budget as the policies: at most ``B``
repair units in each reset window (no carry-over), ``c_r`` per repair.

Reduction (exact for the Step 3 budget rules):

* Episodes are independent apart from the budget: a repair covers the queries
  of one summary until its next source change (Step 2).
* Inside a window, repairing as early as possible is never worse: the
  candidate repair instant in window ``w`` is ``t = max(episode start, window
  start)``, covering every later query in the episode. Queries before ``t`` are
  verified (or answered stale, when allowed). The engine can realize this
  instant: deferred repairs drain at the first event of each window.
* Choosing at most one repair window per episode, subject to at most
  ``floor(B / c_r)`` repairs per window, is a bipartite b-matching. It is
  solved exactly as a 0/1 integer program with HiGHS (its constraint matrix is
  totally unimodular, so the LP relaxation is already integral); the solver
  status and MIP gap are reported. A greedy assignment is available for very
  large runs and is labeled a heuristic: it is feasible, so its cost is an
  upper bound on the optimum, not a lower bound.

Stale-use semantics match ``score_evolving_run``: per-query cost
``min(c_v, λ)``, and a relaxed bound of 0 for expiry episodes.

Measured-cost replay (``cost_profile``): per-query and per-episode costs are
the same keyed draws the engine charges, and each window's constraint becomes
a knapsack (total repair cost at most ``B``) instead of a repair count. It is
still solved exactly as a 0/1 integer program, but the matrix is no longer
totally unimodular, so the reported MIP gap matters.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import floor, inf

from verify_repair.models import stamp
from verify_repair.simulator.events import QueryEvent
from verify_repair.simulator.evolving import EXPIRY, LINK_PREFIX, EvolvingWorkload, horizon_end

EXACT = "exact-milp"
GREEDY = "greedy-heuristic"
EPSILON = 1e-9


@dataclass(frozen=True)
class RepairOption:
    episode_id: str
    window: int
    savings: float


@dataclass
class BudgetOracle:
    total_cost: float
    per_episode: dict[str, float]
    chosen: dict[str, int]          # episode -> window of its repair
    method: str
    status: str
    mip_gap: float | None
    options: int
    windows_at_capacity: int
    repairs_per_window: dict[int, int] = field(default_factory=dict)


def episode_query_times(workload: EvolvingWorkload) -> dict[str, list[datetime]]:
    """Query instants in each episode, in order (fresh queries are excluded)."""
    return {episode_id: [when for when, _ in queries]
            for episode_id, queries in episode_queries(workload).items()}


def episode_queries(workload: EvolvingWorkload) -> dict[str, list[tuple[datetime, str]]]:
    """``(instant, query event ID)`` for each episode's queries, in order."""
    events = {event.event_id: event for event in workload.events}
    owner = {fact_id: spec["summary_id"] for spec in workload.summaries
             for slot, fact_id in spec["slots"].items() if not slot.startswith(LINK_PREFIX)}
    order = defaultdict(list)
    for episode in workload.episodes:
        source = episode.episode_id.split(":", 1)[1]
        if source.endswith(":expiry"):
            key = (stamp(events[source[:-len(":expiry")]].valid_to), -1)
        else:
            key = (stamp(events[source].timestamp), events[source].sequence_number)
        order[episode.summary_id].append((key, episode.episode_id))
    for rows in order.values():
        rows.sort()
    times = {episode.episode_id: [] for episode in workload.episodes}
    for event in workload.events:
        if not isinstance(event, QueryEvent):
            continue
        rows = order.get(owner[event.expected_fact_ids[0]], [])
        position = bisect_right([key for key, _ in rows], (stamp(event.timestamp), event.sequence_number)) - 1
        if position >= 0:
            times[rows[position][1]].append((event.timestamp, event.event_id))
    return times


def budget_oracle(
    workload: EvolvingWorkload,
    verify_cost: float,
    repair_cost: float,
    budget_units: float | None,
    window_seconds: float = 3600.0,
    *,
    stale_penalty: float = inf,
    allow_stale_use: bool = False,
    method: str = EXACT,
    cost_profile=None,
    seed: int | None = None,
) -> BudgetOracle:
    """Minimum total cost achievable in hindsight under the shared repair budget."""
    if cost_profile is not None:
        return _measured_budget_oracle(workload, budget_units, window_seconds, stale_penalty,
                                       allow_stale_use, method, cost_profile, seed)
    if method not in (EXACT, GREEDY):
        raise ValueError(f"method must be {EXACT!r} or {GREEDY!r}")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    origin = datetime.fromisoformat(workload.phase_starts[0])
    end_of_trace = horizon_end(workload.config)

    def window_of(at_time: datetime) -> int:
        return floor((at_time - origin).total_seconds() / window_seconds)

    times = episode_query_times(workload)
    base, options = {}, []
    for episode in workload.episodes:
        queries = times[episode.episode_id]
        if len(queries) != episode.accesses:
            raise ValueError(f"episode metadata disagrees with queries for {episode.episode_id}")
        if allow_stale_use and episode.cause == EXPIRY:
            base[episode.episode_id] = 0.0  # relaxed: may be correct again (see score_evolving_run)
            continue
        per_query = min(verify_cost, stale_penalty) if allow_stale_use else verify_cost
        base[episode.episode_id] = per_query * len(queries)
        start = datetime.fromisoformat(episode.start_time)
        finish = datetime.fromisoformat(episode.end_time) if episode.end_time else end_of_trace
        for window in range(window_of(start), window_of(finish - timedelta(microseconds=1)) + 1):
            instant = max(start, origin + timedelta(seconds=window * window_seconds))
            covered = len(queries) - bisect_left(queries, instant)
            savings = per_query * covered - repair_cost
            if savings > EPSILON:
                options.append(RepairOption(episode.episode_id, window, savings))

    if budget_units is None or repair_cost <= EPSILON:
        capacity = None
    else:
        capacity = floor(budget_units / repair_cost + EPSILON)

    best = {}
    for option in options:  # earliest window has the largest savings for its episode
        if option.episode_id not in best or option.savings > best[option.episode_id].savings + EPSILON:
            best[option.episode_id] = option
    demand = Counter(option.window for option in best.values())
    status, gap = "unconstrained", None
    if capacity is None or all(count <= capacity for count in demand.values()):
        chosen = {episode_id: option.window for episode_id, option in best.items()}
        if capacity is not None:
            status = "budget-not-binding"
    elif method == GREEDY:
        chosen, used = {}, Counter()
        for option in sorted(options, key=lambda o: (-o.savings, o.window, o.episode_id)):
            if option.episode_id not in chosen and used[option.window] < capacity:
                chosen[option.episode_id] = option.window
                used[option.window] += 1
        status = "heuristic"
    else:
        chosen, status, gap = _solve_exact(options, capacity)

    savings = {(o.episode_id, o.window): o.savings for o in options}
    per_episode = {episode_id: cost - savings.get((episode_id, chosen[episode_id]), 0.0)
                   if episode_id in chosen else cost for episode_id, cost in base.items()}
    used = Counter(chosen.values())
    return BudgetOracle(
        total_cost=sum(per_episode.values()), per_episode=per_episode, chosen=chosen,
        method=GREEDY if status == "heuristic" else EXACT, status=status, mip_gap=gap,
        options=len(options),
        windows_at_capacity=sum(capacity is not None and count >= capacity for count in used.values()),
        repairs_per_window=dict(sorted(used.items())),
    )


def _solve_exact(options: list[RepairOption], capacity: int | None, *,
                 weights: dict[str, float] | None = None, budget_units: float | None = None):
    """Exact 0/1 program; window rows count repairs, or sum ``weights`` up to ``budget_units``."""
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix

    episodes = {episode_id: row for row, episode_id in enumerate(sorted({o.episode_id for o in options}))}
    windows = {window: len(episodes) + row for row, window in enumerate(sorted({o.window for o in options}))}
    rows, cols, values = [], [], []
    for column, option in enumerate(options):
        rows += [episodes[option.episode_id], windows[option.window]]
        cols += [column, column]
        values += [1.0, 1.0 if weights is None else weights[option.episode_id]]
    matrix = coo_matrix((np.array(values), (rows, cols)), shape=(len(episodes) + len(windows), len(options)))
    window_limit = float(capacity) if weights is None else float(budget_units) + EPSILON
    upper = np.concatenate([np.ones(len(episodes)), np.full(len(windows), window_limit)])
    result = milp(
        c=-np.array([option.savings for option in options]),
        constraints=LinearConstraint(matrix.tocsr(), -np.inf, upper),
        integrality=np.ones(len(options)),
        bounds=Bounds(0, 1),
    )
    if result.status != 0 or result.x is None:
        raise RuntimeError(f"budget oracle solver failed: {result.message}")
    chosen = {option.episode_id: option.window for option, value in zip(options, result.x) if value > 0.5}
    return chosen, "optimal", float(getattr(result, "mip_gap", 0.0) or 0.0)


def _measured_budget_oracle(workload, budget_units, window_seconds, stale_penalty,
                            allow_stale_use, method, cost_profile, seed) -> BudgetOracle:
    """Budget oracle with per-query and per-episode keyed cost draws (knapsack windows)."""
    from verify_repair.costs import repair_key, verify_key

    if method not in (EXACT, GREEDY):
        raise ValueError(f"method must be {EXACT!r} or {GREEDY!r}")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if seed is None:
        raise ValueError("measured-cost oracle needs the run seed")
    origin = datetime.fromisoformat(workload.phase_starts[0])
    end_of_trace = horizon_end(workload.config)
    fact_counts = {spec["summary_id"]: len(spec["slots"]) for spec in workload.summaries}

    def window_of(at_time: datetime) -> int:
        return floor((at_time - origin).total_seconds() / window_seconds)

    queries_by_episode = episode_queries(workload)
    base, options, weight = {}, [], {}
    for episode in workload.episodes:
        queries = queries_by_episode[episode.episode_id]
        if len(queries) != episode.accesses:
            raise ValueError(f"episode metadata disagrees with queries for {episode.episode_id}")
        if allow_stale_use and episode.cause == EXPIRY:
            base[episode.episode_id] = 0.0
            continue
        facts = fact_counts[episode.summary_id]
        per_query = [cost_profile.draw("verify", verify_key(event_id, episode.summary_id), seed, facts)
                     for _, event_id in queries]
        if allow_stale_use:
            per_query = [min(cost, stale_penalty) for cost in per_query]
        repair = cost_profile.draw("repair", repair_key(episode.summary_id, episode.episode_id), seed, facts)
        weight[episode.episode_id] = repair
        base[episode.episode_id] = sum(per_query)
        instants = [when for when, _ in queries]
        suffix = [0.0] * (len(per_query) + 1)  # suffix[i] = cost of queries i..end
        for i in range(len(per_query) - 1, -1, -1):
            suffix[i] = suffix[i + 1] + per_query[i]
        start = datetime.fromisoformat(episode.start_time)
        finish = datetime.fromisoformat(episode.end_time) if episode.end_time else end_of_trace
        for window in range(window_of(start), window_of(finish - timedelta(microseconds=1)) + 1):
            instant = max(start, origin + timedelta(seconds=window * window_seconds))
            savings = suffix[bisect_left(instants, instant)] - repair
            if savings > EPSILON:
                options.append(RepairOption(episode.episode_id, window, savings))

    best = {}
    for option in options:
        if option.episode_id not in best or option.savings > best[option.episode_id].savings + EPSILON:
            best[option.episode_id] = option
    demand = defaultdict(float)
    for option in best.values():
        demand[option.window] += weight[option.episode_id]
    status, gap = "unconstrained", None
    if budget_units is None or all(total <= budget_units + EPSILON for total in demand.values()):
        chosen = {episode_id: option.window for episode_id, option in best.items()}
        if budget_units is not None:
            status = "budget-not-binding"
    elif method == GREEDY:
        chosen, used = {}, defaultdict(float)
        for option in sorted(options, key=lambda o: (-o.savings, o.window, o.episode_id)):
            cost = weight[option.episode_id]
            if option.episode_id not in chosen and used[option.window] + cost <= budget_units + EPSILON:
                chosen[option.episode_id] = option.window
                used[option.window] += cost
        status = "heuristic"
    else:
        chosen, status, gap = _solve_exact(options, None, weights=weight, budget_units=budget_units)

    savings = {(o.episode_id, o.window): o.savings for o in options}
    per_episode = {episode_id: cost - savings.get((episode_id, chosen[episode_id]), 0.0)
                   if episode_id in chosen else cost for episode_id, cost in base.items()}
    used = Counter(chosen.values())
    spend = defaultdict(float)
    for episode_id, window in chosen.items():
        spend[window] += weight[episode_id]
    cheapest = min(weight.values(), default=0.0)
    return BudgetOracle(
        total_cost=sum(per_episode.values()), per_episode=per_episode, chosen=chosen,
        method=GREEDY if status == "heuristic" else EXACT, status=status, mip_gap=gap,
        options=len(options),
        windows_at_capacity=sum(budget_units is not None and spent + cheapest > budget_units + EPSILON
                                for spent in spend.values()),
        repairs_per_window=dict(sorted(used.items())),
    )
