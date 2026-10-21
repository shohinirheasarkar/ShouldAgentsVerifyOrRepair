"""Generate/save seeded controlled workloads; replay via ``load_synthetic``.

Run ``python -m verify_repair.cli generate-synthetic --help`` for options.
Each independent memory has three initial facts, one summary, one correction,
and an exact sampled number of subsequent queries. Demand labels are evaluator
metadata only; the simulator and maintenance policies never receive them.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from verify_repair.db import connect, transaction
from verify_repair.maintenance.summary_builder import DeterministicTemplateSummaryBuilder
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.summaries import SummaryStore
from verify_repair.models import ChangeType, FactVersionRef, stamp
from verify_repair.retrieval.keyword import KeywordRetriever
from verify_repair.simulator.engine import RunConfig
from verify_repair.simulator.events import CorrectionEvent, QueryEvent

FORMAT_VERSION = "synthetic-v1"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
GROUP_BOUNDS = {"cold": (0, 2), "warm": (3, 10), "hot": (11, 50)}


@dataclass(frozen=True)
class SyntheticConfig:
    """Group sizes sum to memory count; query ranges are inclusive.

    Omitted group sizes divide memories equally, with remainders assigned to
    cold, then warm. Equal query endpoints select an exact count for a group.
    """

    num_memories: int = 100
    seed: int = 12345
    group_counts: tuple[int, int, int] | None = None
    cold_queries: tuple[int, int] = (0, 2)
    warm_queries: tuple[int, int] = (3, 10)
    hot_queries: tuple[int, int] = (11, 50)

    def __post_init__(self):
        if type(self.num_memories) is not int or self.num_memories <= 0:
            raise ValueError("num_memories must be a positive integer")
        if type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        counts = self.group_counts
        if counts is None:
            size, remainder = divmod(self.num_memories, 3)
            counts = tuple(size + int(i < remainder) for i in range(3))
        if (len(counts) != 3 or any(type(n) is not int or n < 0 for n in counts)
                or sum(counts) != self.num_memories):
            raise ValueError("group_counts must be three nonnegative integers summing to num_memories")
        object.__setattr__(self, "group_counts", tuple(counts))
        for group, (lower, upper) in GROUP_BOUNDS.items():
            bounds = getattr(self, f"{group}_queries")
            if (len(bounds) != 2 or any(type(n) is not int for n in bounds)
                    or not lower <= bounds[0] <= bounds[1] <= upper):
                raise ValueError(f"{group}_queries must be an inclusive range within {lower}–{upper}")
            object.__setattr__(self, f"{group}_queries", tuple(bounds))

    def dataset_id(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode("utf-8")
        return f"{FORMAT_VERSION}-{hashlib.sha256(payload).hexdigest()[:16]}"


@dataclass(frozen=True)
class MemoryDemand:
    """Ground truth for evaluation; never supplied to policy hooks."""

    summary_id: str
    entity: str
    entity_type: str
    corrected_fact_id: str
    group: str
    future_queries: int


@dataclass
class SyntheticWorkload:
    config: SyntheticConfig
    facts: list[dict]
    summaries: list[dict]
    events: list[CorrectionEvent | QueryEvent]
    demands: list[MemoryDemand]

    def validate(self) -> None:
        """Reject inconsistent v1 inputs before saving or opening a database."""
        count = self.config.num_memories
        facts = {fact["fact_id"]: fact for fact in self.facts}
        summary_ids = {spec["summary_id"] for spec in self.summaries}
        demands = {demand.summary_id: demand for demand in self.demands}
        if (len(self.facts) != 3 * count or len(facts) != 3 * count
                or len(self.summaries) != count or len(summary_ids) != count
                or len(self.demands) != count or set(demands) != summary_ids):
            raise ValueError("synthetic facts, summaries, and demands must match num_memories with unique IDs")
        sources = {}
        for spec in self.summaries:
            if len(spec["slots"]) != 3:
                raise ValueError("synthetic summaries must have three source slots")
            for slot, fact_id in spec["slots"].items():
                if (fact_id not in facts or fact_id in sources
                        or facts[fact_id]["attribute"] != slot
                        or facts[fact_id]["entity"] != demands[spec["summary_id"]].entity):
                    raise ValueError("synthetic source slots must reference independent facts for their entity")
                sources[fact_id] = spec["summary_id"]
        expected_counts = {}
        for demand in self.demands:
            if demand.group not in GROUP_BOUNDS:
                raise ValueError("unknown synthetic demand group")
            low, high = getattr(self.config, f"{demand.group}_queries")
            if type(demand.future_queries) is not int or not low <= demand.future_queries <= high:
                raise ValueError("synthetic future query counts must lie within the configured group range")
            if sources.get(demand.corrected_fact_id) != demand.summary_id:
                raise ValueError("synthetic demand must identify a source fact of its summary")
            expected_counts[demand.corrected_fact_id] = demand.future_queries
        if Counter(d.group for d in self.demands) != Counter(dict(zip(GROUP_BOUNDS, self.config.group_counts))):
            raise ValueError("synthetic demand groups must match configured group_counts")
        corrections = self.events[:count]
        queries = self.events[count:]
        if (any(not isinstance(event, CorrectionEvent) for event in corrections)
                or len(corrections) != count
                or any(not isinstance(event, QueryEvent) for event in queries)):
            raise ValueError("synthetic events must contain one correction per memory before all queries")
        if Counter(event.fact_id for event in corrections) != Counter(expected_counts.keys()):
            raise ValueError("synthetic corrections must cover every demand exactly once")
        if any(event.change_type != ChangeType.CORRECTION or event.valid_to is not None
               or event.value is None or event.value == facts[event.fact_id]["value"] for event in corrections):
            raise ValueError("synthetic corrections must change the initial fact value permanently")
        if any(event.top_k != 1 or len(event.expected_fact_ids) != 1 for event in queries):
            raise ValueError("synthetic queries must target one corrected fact and retrieve one summary")
        if Counter(event.expected_fact_ids[0] for event in queries) != Counter(expected_counts):
            raise ValueError("synthetic future query counts do not match query events")
        keys = [(stamp(event.timestamp), event.sequence_number) for event in self.events]
        if any(a >= b for a, b in zip(keys, keys[1:])):
            raise ValueError("synthetic event order keys must be strictly increasing")
        if (len({event.event_id for event in self.events}) != len(self.events)
                or len({event.query_id for event in queries}) != len(queries)):
            raise ValueError("synthetic event IDs and query IDs must be unique")

    def to_dict(self) -> dict:
        """Return versioned JSON-compatible inputs, including evaluator metadata."""
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
            "demands": [asdict(demand) for demand in self.demands],
        }

    def write(self, path: str | Path) -> str:
        """Create a new artifact and return its SHA-256, with no wall-clock data.

        Exclusive creation prevents accidental overwrite of another experiment.
        Parent directories must already exist.
        """
        self.validate()
        encoded = (json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with Path(path).open("xb") as stream:
            stream.write(encoded)
        return hashlib.sha256(encoded).hexdigest()


def generate_synthetic(config: SyntheticConfig = SyntheticConfig()) -> SyntheticWorkload:
    """Generate the complete inputs without storage, model calls, or global RNG."""
    rng = random.Random(config.seed)
    groups = [group for group, count in zip(GROUP_BOUNDS, config.group_counts)
              for _ in range(count)]
    rng.shuffle(groups)
    facts, summaries, demands = [], [], []
    corrections = []
    queries = []

    for index, group in enumerate(groups, 1):
        kind = ("person", "project", "place")[(index - 1) % 3]
        entity = f"{kind.title()}{index:06d}"
        summary_id = f"memory_{index:06d}"
        if kind == "person":
            attribute = "location"
            values = {"name": entity, "job": rng.choice(("researcher", "engineer", "teacher"))}
            choices = ("Maryland", "Boston", "Seattle", "Austin", "Denver")
            template = "{name} is a {job} living in {location}."
            question = f"Where does {entity} reside?"
        elif kind == "project":
            attribute = "status"
            values = {"name": entity, "topic": rng.choice(("robotics", "memory", "climate"))}
            choices = ("planned", "active", "paused", "completed")
            template = "{name} studies {topic} and is {status}."
            question = f"Which phase for {entity}?"
        else:
            attribute = "capacity"
            values = {"name": entity, "purpose": rng.choice(("library", "laboratory", "auditorium"))}
            choices = ("25", "50", "100", "200", "500")
            template = "{name} is a {purpose} with {capacity} seats."
            question = f"How many fit inside {entity}?"
        old_value = rng.choice(choices)
        new_value = rng.choice([value for value in choices if value != old_value])
        values[attribute] = old_value
        slots = {}
        for slot, value in values.items():
            fact_id = f"{summary_id}_{slot}"
            slots[slot] = fact_id
            facts.append({"fact_id": fact_id, "entity": entity, "attribute": slot,
                          "value": value, "valid_from": stamp(START)})
        summaries.append({"summary_id": summary_id, "template": template,
                          "slots": slots, "created_at": stamp(START)})
        count = rng.randint(*getattr(config, f"{group}_queries"))
        demands.append(MemoryDemand(summary_id, entity, kind, slots[attribute], group, count))
        corrections.append((slots[attribute], new_value))
        # Other words in these questions never occur in generated summary text.
        # The immutable entity token remains searchable across every repair.
        queries.extend((question, slots[attribute]) for _ in range(count))

    rng.shuffle(corrections)
    rng.shuffle(queries)
    events = []
    for sequence, (fact_id, value) in enumerate(corrections, 1):
        events.append(CorrectionEvent(f"correction_{sequence:06d}",
                                      START + timedelta(seconds=sequence), sequence,
                                      fact_id, ChangeType.CORRECTION, value))
    for query_number, (text, fact_id) in enumerate(queries, 1):
        sequence = config.num_memories + query_number
        query_id = f"query_{query_number:09d}"
        events.append(QueryEvent(query_id, START + timedelta(seconds=sequence), sequence,
                                 query_id, text, 1, (fact_id,)))
    return SyntheticWorkload(config, facts, summaries, events, demands)


def read_synthetic(path: str | Path) -> SyntheticWorkload:
    """Read saved inputs; never regenerate a saved experiment from its seed."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw["format_version"] != FORMAT_VERSION:
        raise ValueError(f"unsupported synthetic format: {raw['format_version']}")
    config = SyntheticConfig(**raw["config"])
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
    workload = SyntheticWorkload(config, raw["facts"], raw["summaries"], events,
                                 [MemoryDemand(**row) for row in raw["demands"]])
    workload.validate()
    return workload


def load_synthetic(workload: SyntheticWorkload):
    """Build fresh SQLite state with exact v1 provenance, using existing APIs.

    Returns the same tuple as ``load_toy``. Corrections are applied only when
    the returned events are replayed; loading itself creates fresh memories.
    """
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


def synthetic_run_config(workload: SyntheticWorkload) -> RunConfig:
    """Reuse existing nominal costs with the generated seed and dataset ID."""
    return RunConfig(seed=workload.config.seed, dataset_id=workload.config.dataset_id())
