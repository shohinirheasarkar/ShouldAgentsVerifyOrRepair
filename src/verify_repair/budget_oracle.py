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
            times[rows[position][1]].append(event.timestamp)
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
) -> BudgetOracle:
    """Minimum total cost achievable in hindsight under the shared repair budget."""
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


def _solve_exact(options: list[RepairOption], capacity: int):
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix

    episodes = {episode_id: row for row, episode_id in enumerate(sorted({o.episode_id for o in options}))}
    windows = {window: len(episodes) + row for row, window in enumerate(sorted({o.window for o in options}))}
    rows, cols = [], []
    for column, option in enumerate(options):
        rows += [episodes[option.episode_id], windows[option.window]]
        cols += [column, column]
    matrix = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(len(episodes) + len(windows), len(options)))
    upper = np.concatenate([np.ones(len(episodes)), np.full(len(windows), float(capacity))])
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
