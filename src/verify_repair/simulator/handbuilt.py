"""Tiny hand-written synthetic-v2 traces for regression tests and hand checks.

Times are whole minutes after ``START``. Each memory ``m`` has entity
``Entity00000m`` and facts ``attr01..attrNN`` that all start as "amber".
"""
from collections import Counter
from datetime import timedelta

from verify_repair.models import stamp
from verify_repair.simulator.events import CorrectionEvent, QueryEvent
from verify_repair.simulator.evolving import (
    LINK_PREFIX, QUERY_WORD, EvolvingConfig, EvolvingWorkload, MemoryProfile, compute_episodes,
    phase_starts,
)
from verify_repair.simulator.synthetic import GROUP_BOUNDS, START


def workload_from_spec(changes, queries, facts=2, memories=1, hours=1.0, links=None) -> EvolvingWorkload:
    """Build and validate a workload.

    ``changes``: (minute, memory, slot, ChangeType, value, minutes_valid or None).
    ``queries``: (minute, memory, slot). ``links`` maps a memory to the
    (earlier memory, slot) it also reads. Each memory's group is the one whose
    range contains its query count.
    """
    links = links or {}
    per_memory = Counter(m for _, m, _ in queries)
    group_of = {m: next(g for g, (lo, hi) in GROUP_BOUNDS.items() if lo <= per_memory[m] <= hi)
                for m in range(1, memories + 1)}
    config = EvolvingConfig(num_memories=memories, horizon_hours=hours, facts_per_summary=(facts, facts),
                            group_counts=tuple(list(group_of.values()).count(g) for g in GROUP_BOUNDS),
                            **({"fan_out": 2, "shared_fact_rate": 0.5} if links else {}))
    fact_rows, summaries = [], []
    for m in range(1, memories + 1):
        entity, summary_id = f"Entity{m:06d}", f"memory_{m:06d}"
        attributes = [f"attr{j:02d}" for j in range(1, facts + 1)]
        slots = {a: f"{summary_id}_{a}" for a in attributes}
        fact_rows += [{"fact_id": f, "entity": entity, "attribute": a, "value": "amber",
                       "valid_from": stamp(START)} for a, f in slots.items()]
        names = list(attributes)
        if m in links:
            other, slot = links[m]
            names.append(f"{LINK_PREFIX}attr{slot:02d}")
            slots[names[-1]] = f"memory_{other:06d}_attr{slot:02d}"
        summaries.append({"summary_id": summary_id, "entity": entity, "created_at": stamp(START), "slots": slots,
                          "template": f"{entity} record: " + "; ".join(f"{a} {{{a}}}" for a in names) + "."})
    raw = [(minute, 0, ("change", m, slot, kind, value, valid)) for minute, m, slot, kind, value, valid in changes]
    raw += [(minute, 1, ("query", m, slot)) for minute, m, slot in queries]
    events = []
    for sequence, (minute, _, item) in enumerate(sorted(raw, key=lambda r: (r[0], r[1])), 1):
        at = START + timedelta(minutes=minute)
        summary_id = f"memory_{item[1]:06d}"
        fact_id = f"{summary_id}_attr{item[2]:02d}"
        if item[0] == "change":
            _, _, _, kind, value, valid = item
            events.append(CorrectionEvent(f"change_{sequence:09d}", at, sequence, fact_id, kind, value,
                                          at + timedelta(minutes=valid) if valid else None))
        else:
            events.append(QueryEvent(f"query_{sequence:09d}", at, sequence, f"query_{sequence:09d}",
                                     f"{QUERY_WORD} Entity{item[1]:06d}", 1, (fact_id,)))
    starts = phase_starts(config)
    counts, fresh, episodes = compute_episodes(summaries, events, starts)
    profiles = [MemoryProfile(s["summary_id"], s["entity"], facts, (group_of[m],), tuple(counts[s["summary_id"]]),
                              fresh[s["summary_id"]]) for m, s in enumerate(summaries, 1)]
    workload = EvolvingWorkload(config, fact_rows, summaries, events, starts, profiles, episodes)
    workload.validate()
    return workload
