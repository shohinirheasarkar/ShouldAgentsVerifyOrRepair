"""Evolving synthetic workloads (``synthetic-v2``): repeated changes mixed with queries.

Each memory is one summary over ``d`` independent facts about one entity. Fact
changes arrive as a per-memory Poisson process over the trace horizon; each is a
correction, update, retraction, or temporary exception (which expires and
restores the prior permanent value). Queries follow a stable, bursty, or
shifting cold/warm/hot demand schedule and are interleaved with the changes.

Groups, per-episode access counts, and the shift schedule are evaluator-only
metadata; the simulator and maintenance policies never receive them. The
``synthetic-v1`` format in ``synthetic.py`` is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import random
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from verify_repair.db import connect, transaction
from verify_repair.maintenance.summary_builder import DeterministicTemplateSummaryBuilder
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.summaries import SummaryStore
from verify_repair.models import ChangeType, FactVersionRef, stamp
from verify_repair.retrieval.keyword import KeywordRetriever
from verify_repair.simulator.engine import RunConfig
from verify_repair.simulator.events import CorrectionEvent, QueryEvent
from verify_repair.simulator.synthetic import GROUP_BOUNDS, START

FORMAT_VERSION = "synthetic-v2"
DEMAND_MODES = ("stable", "bursty", "shifting")
CHANGE_KINDS = (ChangeType.CORRECTION, ChangeType.UPDATE,
                ChangeType.RETRACTION, ChangeType.TEMPORARY_EXCEPTION)
EXPIRY = "EXPIRY"
# Attribute values and the query word never collide with entity tokens, so
# keyword retrieval always reaches exactly the queried memory.
VALUES = ("amber", "birch", "cedar", "delta", "ember", "fjord", "garnet", "harbor")
QUERY_WORD = "lookup"
MAX_FACTS = 20
MAX_FAN_OUT = 8
LINK_PREFIX = "linked_"
MICROS_PER_HOUR = 3_600_000_000


@dataclass(frozen=True)
class EvolvingConfig:
    """Settings for one evolving trace; all durations are hours or minutes.

    ``changes_per_memory`` is the Poisson mean number of fact changes per
    memory over the horizon. ``change_mix`` weights correction, update,
    retraction, and temporary exception. Query ranges apply per demand phase.
    ``num_phases`` is used only by ``shifting`` demand. With
    ``shared_fact_rate`` p > 0, each memory after the first also links (with
    probability p) one fact owned by an earlier memory, as a ``linked_<attr>``
    slot; no fact feeds more than ``fan_out`` summaries in total.
    """

    num_memories: int = 100
    seed: int = 12345
    group_counts: tuple[int, int, int] | None = None
    cold_queries: tuple[int, int] = (0, 2)
    warm_queries: tuple[int, int] = (3, 10)
    hot_queries: tuple[int, int] = (11, 50)
    demand_mode: str = "stable"
    num_phases: int = 2
    horizon_hours: float = 24.0
    changes_per_memory: float = 2.0
    change_mix: tuple[float, float, float, float] = (0.6, 0.2, 0.1, 0.1)
    exception_hours: float = 1.0
    facts_per_summary: tuple[int, int] = (3, 3)
    burst_minutes: float = 10.0
    fan_out: int = 1
    shared_fact_rate: float = 0.0

    def __post_init__(self):
        if type(self.num_memories) is not int or self.num_memories <= 0:
            raise ValueError("num_memories must be a positive integer")
        if type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        counts = self.group_counts
        if counts is None:
            size, remainder = divmod(self.num_memories, 3)
            counts = tuple(size + int(i < remainder) for i in range(3))
        counts = tuple(counts)
        if (len(counts) != 3 or any(type(n) is not int or n < 0 for n in counts)
                or sum(counts) != self.num_memories):
            raise ValueError("group_counts must be three nonnegative integers summing to num_memories")
        object.__setattr__(self, "group_counts", counts)
        for group, (lower, upper) in GROUP_BOUNDS.items():
            bounds = tuple(getattr(self, f"{group}_queries"))
            if (len(bounds) != 2 or any(type(n) is not int for n in bounds)
                    or not lower <= bounds[0] <= bounds[1] <= upper):
                raise ValueError(f"{group}_queries must be an inclusive range within {lower}–{upper}")
            object.__setattr__(self, f"{group}_queries", bounds)
        if self.demand_mode not in DEMAND_MODES:
            raise ValueError(f"demand_mode must be one of {DEMAND_MODES}")
        if type(self.num_phases) is not int or self.num_phases < 1:
            raise ValueError("num_phases must be a positive integer")
        for name in ("horizon_hours", "exception_hours", "burst_minutes"):
            if not _positive_number(getattr(self, name)):
                raise ValueError(f"{name} must be a positive number")
        if not _positive_number(self.changes_per_memory, allow_zero=True):
            raise ValueError("changes_per_memory must be a nonnegative number")
        mix = tuple(self.change_mix)
        if (len(mix) != 4 or not all(_positive_number(w, allow_zero=True) for w in mix)
                or sum(mix) <= 0):
            raise ValueError("change_mix must be four nonnegative weights with a positive sum")
        object.__setattr__(self, "change_mix", mix)
        facts = tuple(self.facts_per_summary)
        if (len(facts) != 2 or any(type(n) is not int for n in facts)
                or not 1 <= facts[0] <= facts[1] <= MAX_FACTS):
            raise ValueError(f"facts_per_summary must be an inclusive range within 1–{MAX_FACTS}")
        object.__setattr__(self, "facts_per_summary", facts)
        if type(self.fan_out) is not int or not 1 <= self.fan_out <= MAX_FAN_OUT:
            raise ValueError(f"fan_out must be an integer within 1–{MAX_FAN_OUT}")
        if not _positive_number(self.shared_fact_rate, allow_zero=True) or self.shared_fact_rate > 1:
            raise ValueError("shared_fact_rate must lie within 0–1")
        if self.shared_fact_rate > 0 and self.fan_out == 1:
            raise ValueError("shared facts need fan_out of at least 2")

    @property
    def phases(self) -> int:
        return self.num_phases if self.demand_mode == "shifting" else 1

    def dataset_id(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode("utf-8")
        return f"{FORMAT_VERSION}-{hashlib.sha256(payload).hexdigest()[:16]}"


def _horizon_micros(config: EvolvingConfig) -> int:
    return round(config.horizon_hours * MICROS_PER_HOUR)


def _phase_bounds(config: EvolvingConfig) -> list[tuple[int, int]]:
    horizon, phases = _horizon_micros(config), config.phases
    return [(horizon * p // phases, horizon * (p + 1) // phases) for p in range(phases)]


def phase_starts(config: EvolvingConfig) -> list[str]:
    """Phase start stamps; phases split the horizon evenly at whole microseconds."""
    return [stamp(START + timedelta(microseconds=begin)) for begin, _ in _phase_bounds(config)]


def _positive_number(value, allow_zero: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value >= 0 if allow_zero else value > 0


@dataclass(frozen=True)
class MemoryProfile:
    """Evaluator-only demand labels for one memory."""

    summary_id: str
    entity: str
    facts: int
    groups: tuple[str, ...]          # demand group in each phase
    phase_queries: tuple[int, ...]   # exact query count in each phase
    fresh_queries: int               # queries before the first source change


@dataclass(frozen=True)
class Episode:
    """One source-change episode, from a change (or effective expiry) to the next.

    ``episode_id`` equals the simulator's ``stale_episode_id``. ``accesses``
    counts every query to the memory in the episode, including queries after a
    repair, because repair value lasts until the next source change.
    """

    summary_id: str
    episode_id: str
    cause: str
    start_time: str
    end_time: str | None
    accesses: int


@dataclass
class EvolvingWorkload:
    config: EvolvingConfig
    facts: list[dict]
    summaries: list[dict]
    events: list[CorrectionEvent | QueryEvent]
    phase_starts: list[str]
    memories: list[MemoryProfile]
    episodes: list[Episode]

    def validate(self) -> None:
        """Reject inconsistent inputs before saving or opening a database."""
        config = self.config
        facts = {fact["fact_id"]: fact for fact in self.facts}
        if len(facts) != len(self.facts):
            raise ValueError("fact IDs must be unique")
        if len(self.summaries) != config.num_memories or len(self.memories) != config.num_memories:
            raise ValueError("summaries and memory profiles must match num_memories")
        owner = {}
        users = Counter()
        entities = set()
        for spec, profile in zip(self.summaries, self.memories):
            if spec["summary_id"] != profile.summary_id or spec["entity"] != profile.entity:
                raise ValueError("memory profiles must follow summary order")
            if spec["entity"] in entities:
                raise ValueError("summary entities must be unique")
            entities.add(spec["entity"])
            low, high = config.facts_per_summary
            own = [slot for slot in spec["slots"] if not slot.startswith(LINK_PREFIX)]
            if not low <= len(own) == profile.facts <= high:
                raise ValueError("facts per summary must lie in the configured range")
            for slot, fact_id in spec["slots"].items():
                fact = facts.get(fact_id)
                if fact is None:
                    raise ValueError("summary slots must reference known facts")
                users[fact_id] += 1
                if slot.startswith(LINK_PREFIX):
                    if (slot != LINK_PREFIX + fact["attribute"] or fact["entity"] == spec["entity"]
                            or owner.get(fact_id) is None):
                        raise ValueError("linked slots must reference an earlier memory's fact")
                elif fact_id in owner or fact["attribute"] != slot or fact["entity"] != spec["entity"]:
                    raise ValueError("summary slots must reference independent facts for their entity")
                else:
                    owner[fact_id] = spec["summary_id"]
        if set(owner) != set(facts):
            raise ValueError("every fact must belong to exactly one summary")
        if max(users.values(), default=1) > config.fan_out:
            raise ValueError("a fact feeds more summaries than fan_out allows")

        keys = [(stamp(event.timestamp), event.sequence_number) for event in self.events]
        if any(a >= b for a, b in zip(keys, keys[1:])):
            raise ValueError("event order keys must be strictly increasing")
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("event IDs must be unique")
        horizon = START + timedelta(microseconds=_horizon_micros(config))
        entity_of = {spec["summary_id"]: spec["entity"] for spec in self.summaries}
        for event in self.events:
            if not START < event.timestamp < horizon:
                raise ValueError("events must lie strictly inside the trace horizon")
            if isinstance(event, QueryEvent):
                if (event.query_id != event.event_id or event.top_k != 1
                        or len(event.expected_fact_ids) != 1
                        or event.expected_fact_ids[0] not in owner
                        or event.query_text != f"{QUERY_WORD} {entity_of[owner[event.expected_fact_ids[0]]]}"):
                    raise ValueError("queries must name one memory's entity and one of its facts")
            elif not isinstance(event, CorrectionEvent) or event.fact_id not in owner:
                raise ValueError("changes must reference known facts")
        expiry_times = {event.valid_to for event in self.events
                        if isinstance(event, CorrectionEvent) and event.valid_to is not None}
        if expiry_times & {event.timestamp for event in self.events}:
            raise ValueError("expiry times must not coincide with event times")
        _replay_ledger(self.facts, self.events)

        if self.phase_starts != phase_starts(config):
            raise ValueError("phase starts must split the horizon evenly")
        for phase in range(config.phases):
            groups = Counter(profile.groups[phase] for profile in self.memories)
            expected = Counter({g: n for g, n in zip(GROUP_BOUNDS, config.group_counts) if n})
            if groups != expected:
                raise ValueError("each phase must match configured group_counts")
        for profile in self.memories:
            if len(profile.groups) != config.phases or len(profile.phase_queries) != config.phases:
                raise ValueError("memory profiles need one group and count per phase")
            for group, count in zip(profile.groups, profile.phase_queries):
                low, high = getattr(config, f"{group}_queries")
                if type(count) is not int or not low <= count <= high:
                    raise ValueError("phase query counts must lie within the group range")

        phase_counts, fresh, episodes = compute_episodes(self.summaries, self.events, self.phase_starts)
        if [list(profile.phase_queries) for profile in self.memories] != [
                phase_counts[profile.summary_id] for profile in self.memories]:
            raise ValueError("phase query counts do not match query events")
        if [profile.fresh_queries for profile in self.memories] != [
                fresh[profile.summary_id] for profile in self.memories]:
            raise ValueError("fresh query counts do not match query events")
        if self.episodes != episodes:
            raise ValueError("episode metadata does not match the event trace")

    def to_dict(self) -> dict:
        events = []
        for event in self.events:
            row = asdict(event)
            row["timestamp"] = stamp(event.timestamp)
            if isinstance(event, CorrectionEvent):
                row["type"] = "correction"
                row["valid_to"] = stamp(event.valid_to) if event.valid_to else None
            else:
                row["type"] = "query"
            events.append(row)
        return {
            "format_version": FORMAT_VERSION,
            "config": asdict(self.config),
            "dataset_id": self.config.dataset_id(),
            "facts": self.facts,
            "summaries": self.summaries,
            "events": events,
            "phase_starts": self.phase_starts,
            "memories": [asdict(profile) for profile in self.memories],
            "episodes": [asdict(episode) for episode in self.episodes],
        }

    def write(self, path: str | Path) -> str:
        """Create a new artifact (never overwrite) and return its SHA-256."""
        self.validate()
        encoded = (json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with Path(path).open("xb") as stream:
            stream.write(encoded)
        return hashlib.sha256(encoded).hexdigest()


def _replay_ledger(facts: list[dict], events: list) -> None:
    """Apply every change through the real ledger; reject no-op changes."""
    db = connect()
    try:
        ledger = FactLedger(db)
        with transaction(db):
            for fact in facts:
                ledger.append_initial_fact(fact["fact_id"], fact["entity"], fact["attribute"],
                                           fact["value"], datetime.fromisoformat(fact["valid_from"]),
                                           f"initial:{fact['fact_id']}")
            for event in events:
                if not isinstance(event, CorrectionEvent):
                    continue
                first = ledger.get_history(event.fact_id)[0]
                current = ledger.get_current_value(first.entity, first.attribute, event.timestamp)
                if event.value == current:
                    raise ValueError(f"change {event.event_id} does not change the current value")
                if event.change_type == ChangeType.CORRECTION:
                    ledger.append_correction(event.fact_id, event.value, event.timestamp,
                                             event.event_id, event.sequence_number)
                elif event.change_type == ChangeType.UPDATE:
                    ledger.append_update(event.fact_id, event.value, event.timestamp,
                                         event.event_id, event.sequence_number)
                elif event.change_type == ChangeType.RETRACTION:
                    ledger.append_retraction(event.fact_id, event.timestamp,
                                             event.event_id, event.sequence_number)
                elif event.change_type == ChangeType.TEMPORARY_EXCEPTION:
                    ledger.append_temporary_exception(event.fact_id, event.value, event.timestamp,
                                                      event.valid_to, event.event_id,
                                                      event.sequence_number)
                else:
                    raise ValueError(f"unsupported change type {event.change_type}")
    except ValueError:
        raise
    except Exception as exc:  # ledger transition errors
        raise ValueError(f"invalid change sequence: {exc}") from exc
    finally:
        db.close()


def compute_episodes(summaries: list[dict], events: list, starts: list[str]):
    """Derive per-phase query counts, fresh counts, and episodes from a trace.

    Mirrors the simulator: every change to any source fact starts a new
    episode, and a temporary exception's expiry starts one only if no later
    change to that fact superseded the exception first (the simulator skips
    superseded expiries). Expiries order before same-time events (sequence -1).
    """
    owner = {fact_id: spec["summary_id"] for spec in summaries
             for slot, fact_id in spec["slots"].items() if not slot.startswith(LINK_PREFIX)}
    readers = defaultdict(list)  # fact -> every summary built on it (owner and linked)
    for spec in summaries:
        for fact_id in spec["slots"].values():
            readers[fact_id].append(spec["summary_id"])
    changes_by_fact = defaultdict(list)
    for event in events:
        if isinstance(event, CorrectionEvent):
            changes_by_fact[event.fact_id].append(event)
    boundaries = defaultdict(list)  # summary -> [(key, episode_id, cause, start)]
    for fact_id, changes in changes_by_fact.items():
        for index, event in enumerate(changes):
            start = stamp(event.timestamp)
            later = changes[index + 1] if index + 1 < len(changes) else None
            expires = (event.change_type == ChangeType.TEMPORARY_EXCEPTION
                       and (later is None or later.timestamp > event.valid_to))
            for summary_id in readers[fact_id]:
                boundaries[summary_id].append(((start, event.sequence_number),
                                               f"{summary_id}:{event.event_id}",
                                               event.change_type.value, start))
                if expires:
                    end = stamp(event.valid_to)
                    boundaries[summary_id].append(((end, -1), f"{summary_id}:{event.event_id}:expiry",
                                                   EXPIRY, end))
    for rows in boundaries.values():
        rows.sort()

    phase_counts = {spec["summary_id"]: [0] * len(starts) for spec in summaries}
    fresh = Counter()
    accesses = Counter()
    for event in events:
        if not isinstance(event, QueryEvent):
            continue
        summary_id = owner[event.expected_fact_ids[0]]
        key = (stamp(event.timestamp), event.sequence_number)
        phase_counts[summary_id][bisect_right(starts, key[0]) - 1] += 1
        rows = boundaries.get(summary_id, [])
        position = bisect_right([row[0] for row in rows], key) - 1
        if position < 0:
            fresh[summary_id] += 1
        else:
            accesses[summary_id, position] += 1

    episodes = []
    for spec in summaries:
        summary_id = spec["summary_id"]
        rows = boundaries.get(summary_id, [])
        for position, (_, episode_id, cause, start) in enumerate(rows):
            end = rows[position + 1][3] if position + 1 < len(rows) else None
            episodes.append(Episode(summary_id, episode_id, cause, start, end,
                                    accesses[summary_id, position]))
    return phase_counts, {spec["summary_id"]: fresh[spec["summary_id"]] for spec in summaries}, episodes


def generate_evolving(config: EvolvingConfig = EvolvingConfig()) -> EvolvingWorkload:
    """Generate a complete trace without storage, model calls, or global RNG."""
    rng = random.Random(config.seed)
    horizon = _horizon_micros(config)
    phases = config.phases
    phase_bounds = _phase_bounds(config)
    groups = [group for group, count in zip(GROUP_BOUNDS, config.group_counts) for _ in range(count)]
    rng.shuffle(groups)
    schedule = [groups]
    for _ in range(1, phases):
        moved = list(schedule[-1])
        rng.shuffle(moved)
        schedule.append(moved)

    used = set()

    def reserve(micros: int, low: int) -> int | None:
        """Claim a unique microsecond strictly after ``low`` and inside the horizon."""
        micros = max(micros, low + 1)
        while micros in used:
            micros += 1
        if micros >= horizon:
            return None
        used.add(micros)
        return micros

    facts, summaries, profiles = [], [], []
    raw = []  # (micros, order, kind, payload)
    link_users = Counter()  # fact -> summaries built on it
    for index in range(1, config.num_memories + 1):
        entity = f"Entity{index:06d}"
        summary_id = f"memory_{index:06d}"
        attributes = [f"attr{j:02d}" for j in range(1, rng.randint(*config.facts_per_summary) + 1)]
        template = f"{entity} record: " + "; ".join(f"{a} {{{a}}}" for a in attributes) + "."
        slots = {a: f"{summary_id}_{a}" for a in attributes}
        earlier = len(facts)
        state = {}
        for attribute, fact_id in slots.items():
            value = rng.choice(VALUES)
            facts.append({"fact_id": fact_id, "entity": entity, "attribute": attribute,
                          "value": value, "valid_from": stamp(START)})
            state[fact_id] = {"base": value, "exception": None}
        linked = {}
        if earlier and config.shared_fact_rate and rng.random() < config.shared_fact_rate:
            # Rejection-sample an earlier memory's fact that still has fan-out room.
            for _ in range(32):
                borrowed = facts[rng.randrange(earlier)]
                if link_users[borrowed["fact_id"]] + 2 <= config.fan_out:  # owner + links + this one
                    break
            else:
                borrowed = None
        else:
            borrowed = None
        if borrowed is not None:
            linked[LINK_PREFIX + borrowed["attribute"]] = borrowed["fact_id"]
            link_users[borrowed["fact_id"]] += 1
            template = template[:-1] + f"; {LINK_PREFIX}{borrowed['attribute']} " \
                + f"{{{LINK_PREFIX}{borrowed['attribute']}}}."
        summaries.append({"summary_id": summary_id, "entity": entity, "template": template,
                          "slots": {**slots, **linked}, "created_at": stamp(START)})

        # Source changes: a Poisson process over the horizon, in time order.
        moment, last = 0.0, 0
        rate = config.changes_per_memory / horizon
        while rate > 0:
            moment += rng.expovariate(rate)
            micros = reserve(int(moment), last) if moment < horizon else None
            if micros is None:
                break
            last = micros
            fact_id = slots[rng.choice(attributes)]
            fact = state[fact_id]
            if fact["exception"] and fact["exception"][1] <= micros:
                fact["exception"] = None  # expired; prior permanent value is current
            current = fact["exception"][0] if fact["exception"] else fact["base"]
            kind = rng.choices(CHANGE_KINDS, weights=config.change_mix)[0]
            if kind == ChangeType.TEMPORARY_EXCEPTION and fact["exception"]:
                kind = ChangeType.CORRECTION
            if kind == ChangeType.RETRACTION and current is None:
                kind = ChangeType.CORRECTION
            value = None if kind == ChangeType.RETRACTION else rng.choice(
                [v for v in VALUES if v != current])
            valid_to = None
            if kind == ChangeType.TEMPORARY_EXCEPTION:
                duration = max(1, int(rng.expovariate(1 / (config.exception_hours * MICROS_PER_HOUR))))
                valid_to = reserve(micros + duration, micros)
                if valid_to is None:  # would expire after the trace ends: make it permanent
                    kind = ChangeType.CORRECTION
            if kind == ChangeType.TEMPORARY_EXCEPTION:
                fact["exception"] = (value, valid_to)
            else:
                fact["base"], fact["exception"] = value, None
            raw.append((micros, 0, "change", (fact_id, kind, value, valid_to)))

        # Queries: exact counts per phase from the memory's group in that phase.
        groups_here, counts = [], []
        for phase, (begin, end) in enumerate(phase_bounds):
            group = schedule[phase][index - 1]
            count = rng.randint(*getattr(config, f"{group}_queries"))
            groups_here.append(group)
            counts.append(count)
            span = end - begin
            if config.demand_mode == "bursty" and count:
                width = max(1, int(config.burst_minutes * 60_000_000))
                centers = [begin + rng.randrange(span) for _ in range(max(1, round(count / 5)))]
                offsets = [rng.choice(centers) - begin + int(rng.expovariate(1 / width)) for _ in range(count)]
                times = [begin + offset % span for offset in offsets]
            else:
                times = [begin + rng.randrange(span) for _ in range(count)]
            for micros in times:
                claimed = reserve(micros, max(begin, 0))
                while claimed is None or claimed >= end:  # wrap within the phase
                    claimed = reserve(begin + rng.randrange(span), begin)
                fact_id = slots[rng.choice(attributes)]
                raw.append((claimed, 1, "query", (entity, fact_id)))
        profiles.append((summary_id, entity, len(attributes), tuple(groups_here), tuple(counts)))

    raw.sort(key=lambda item: (item[0], item[1]))
    events = []
    changes = queries = 0
    for sequence, (micros, _, kind, payload) in enumerate(raw, 1):
        timestamp = START + timedelta(microseconds=micros)
        if kind == "change":
            changes += 1
            fact_id, change_type, value, valid_to = payload
            events.append(CorrectionEvent(
                f"change_{changes:09d}", timestamp, sequence, fact_id, change_type, value,
                START + timedelta(microseconds=valid_to) if valid_to is not None else None))
        else:
            queries += 1
            entity, fact_id = payload
            query_id = f"query_{queries:09d}"
            events.append(QueryEvent(query_id, timestamp, sequence, query_id,
                                     f"{QUERY_WORD} {entity}", 1, (fact_id,)))

    _, fresh, episodes = compute_episodes(summaries, events, phase_starts(config))
    memories = [MemoryProfile(summary_id, entity, count, groups_here, counts, fresh[summary_id])
                for summary_id, entity, count, groups_here, counts in profiles]
    return EvolvingWorkload(config, facts, summaries, events, phase_starts(config), memories, episodes)


def read_evolving(path: str | Path) -> EvolvingWorkload:
    """Read saved inputs; never regenerate a saved experiment from its seed."""
    return _from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _from_dict(raw: dict) -> EvolvingWorkload:
    if raw["format_version"] != FORMAT_VERSION:
        raise ValueError(f"unsupported synthetic format: {raw['format_version']}")
    config = EvolvingConfig(**raw["config"])
    if raw["dataset_id"] != config.dataset_id():
        raise ValueError("synthetic dataset ID does not match its configuration")
    events = []
    for row in raw["events"]:
        data = dict(row)
        event_type = data.pop("type")
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        if event_type == "correction":
            data["change_type"] = ChangeType(data["change_type"])
            data["valid_to"] = datetime.fromisoformat(data["valid_to"]) if data["valid_to"] else None
            events.append(CorrectionEvent(**data))
        elif event_type == "query":
            data["expected_fact_ids"] = tuple(data["expected_fact_ids"])
            events.append(QueryEvent(**data))
        else:
            raise ValueError(f"unsupported synthetic event type: {event_type}")
    memories = [MemoryProfile(**{**row, "groups": tuple(row["groups"]),
                                 "phase_queries": tuple(row["phase_queries"])})
                for row in raw["memories"]]
    workload = EvolvingWorkload(config, raw["facts"], raw["summaries"], events, raw["phase_starts"],
                                memories, [Episode(**row) for row in raw["episodes"]])
    workload.validate()
    return workload


def read_workload(path: str | Path):
    """Read a saved ``synthetic-v1`` or ``synthetic-v2`` workload by its header."""
    from verify_repair.simulator.synthetic import read_synthetic

    version = json.loads(Path(path).read_text(encoding="utf-8")).get("format_version")
    if version == FORMAT_VERSION:
        return read_evolving(path)
    return read_synthetic(path)


def load_evolving(workload: EvolvingWorkload):
    """Build fresh SQLite state; returns the same tuple as ``load_synthetic``."""
    workload.validate()
    db = connect()
    ledger, store = FactLedger(db), SummaryStore(db)
    builder = DeterministicTemplateSummaryBuilder()
    try:
        with transaction(db):
            for fact in workload.facts:
                ledger.append_initial_fact(fact["fact_id"], fact["entity"], fact["attribute"],
                                           fact["value"], datetime.fromisoformat(fact["valid_from"]),
                                           f"initial:{fact['fact_id']}")
            for spec in workload.summaries:
                refs = {slot: FactVersionRef(fact_id, 1) for slot, fact_id in spec["slots"].items()}
                values = {slot: ledger.get_version(ref).value for slot, ref in refs.items()}
                store.create_summary(spec["summary_id"], builder.build(spec["template"], values),
                                     spec["template"], refs, datetime.fromisoformat(spec["created_at"]))
        return db, ledger, store, KeywordRetriever(db), workload.events
    except BaseException:
        db.close()
        raise


def evolving_run_config(workload: EvolvingWorkload) -> RunConfig:
    """Reuse existing nominal costs with the generated seed and dataset ID."""
    return RunConfig(seed=workload.config.seed, dataset_id=workload.config.dataset_id())
