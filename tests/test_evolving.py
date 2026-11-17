"""Evolving (synthetic-v2) workloads: generation, validation, and real replay.

Run with ``python -m pytest -q tests/test_evolving.py``; no models or network.
"""
from collections import Counter
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys

import pytest

from verify_repair.cli import main
from verify_repair.models import ChangeType, stamp
from verify_repair.policies.eager_refresh import EagerRefresh
from verify_repair.policies.verify_only import VerifyOnly
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.events import CorrectionEvent, QueryEvent
from verify_repair.simulator.evolving import (
    DEMAND_MODES, EXPIRY, QUERY_WORD, EvolvingConfig, EvolvingWorkload, MemoryProfile,
    compute_episodes, evolving_run_config, generate_evolving, load_evolving, phase_starts,
    read_evolving, read_workload,
)
from verify_repair.simulator.synthetic import GROUP_BOUNDS, START, SyntheticConfig, generate_synthetic


def replay(workload, policy):
    db, ledger, summaries, retriever, events = load_evolving(workload)
    try:
        result = Simulator(ledger, summaries, retriever, policy, evolving_run_config(workload)).run(events)
        return result, ledger, summaries
    finally:
        db.close()


def query_rows(result):
    return [row for row in result.logs if row["event_type"] == "query"]


@pytest.mark.parametrize("kwargs", [
    {"num_memories": 0}, {"seed": 1.5}, {"group_counts": (1, 2, 3)},
    {"demand_mode": "random"}, {"num_phases": 0}, {"horizon_hours": 0},
    {"changes_per_memory": -1}, {"change_mix": (0, 0, 0, 0)}, {"change_mix": (1, 1, 1)},
    {"facts_per_summary": (0, 3)}, {"facts_per_summary": (3, 21)}, {"facts_per_summary": (4, 3)},
    {"exception_hours": 0}, {"burst_minutes": True}, {"hot_queries": (10, 50)},
    {"fan_out": 0}, {"fan_out": 9}, {"shared_fact_rate": 1.5}, {"shared_fact_rate": 0.5},
])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        EvolvingConfig(**kwargs)


@pytest.mark.parametrize("mode", DEMAND_MODES)
def test_generation_is_deterministic_and_round_trips_byte_identically(mode, tmp_path):
    state = random.getstate()
    config = EvolvingConfig(num_memories=60, demand_mode=mode, num_phases=3, facts_per_summary=(1, 20))
    workload = generate_evolving(config)
    assert random.getstate() == state
    assert workload == generate_evolving(config)
    assert workload.events != generate_evolving(replace(config, seed=999)).events
    digest = workload.write(tmp_path / "a.json")
    restored = read_evolving(tmp_path / "a.json")
    assert restored == workload
    assert restored.write(tmp_path / "b.json") == digest
    assert generate_evolving(config).write(tmp_path / "c.json") == digest
    with pytest.raises(FileExistsError):
        workload.write(tmp_path / "a.json")


@pytest.mark.parametrize("mode", DEMAND_MODES)
def test_trace_has_interleaved_repeated_changes_of_every_kind(mode):
    workload = generate_evolving(EvolvingConfig(num_memories=200, demand_mode=mode, changes_per_memory=4))
    kinds = [type(event) for event in workload.events]
    first_query = kinds.index(QueryEvent)
    assert CorrectionEvent in kinds[first_query:]  # changes after queries begin
    per_fact = Counter(e.fact_id for e in workload.events if isinstance(e, CorrectionEvent))
    assert max(per_fact.values()) > 1  # repeated changes to one fact
    causes = Counter(episode.cause for episode in workload.episodes)
    assert set(causes) == {*(kind.value for kind in ChangeType if kind != ChangeType.INITIAL), EXPIRY}
    assert sum(e.accesses for e in workload.episodes) + sum(m.fresh_queries for m in workload.memories) \
        == sum(isinstance(e, QueryEvent) for e in workload.events)


def test_shifting_demand_moves_memories_between_groups_per_phase():
    config = EvolvingConfig(num_memories=90, demand_mode="shifting", num_phases=3)
    workload = generate_evolving(config)
    assert len(workload.phase_starts) == 3 and workload.phase_starts == phase_starts(config)
    assert any(len(set(m.groups)) > 1 for m in workload.memories)
    for phase in range(3):
        assert Counter(m.groups[phase] for m in workload.memories) == {"cold": 30, "warm": 30, "hot": 30}
    stable = generate_evolving(replace(config, demand_mode="stable"))
    assert len(stable.phase_starts) == 1 and all(len(m.groups) == 1 for m in stable.memories)


def test_bursty_demand_clusters_queries_more_than_stable():
    def median_gap(mode):
        workload = generate_evolving(EvolvingConfig(num_memories=60, demand_mode=mode, group_counts=(0, 0, 60),
                                                    changes_per_memory=0))
        gaps = []
        by_memory = {}
        for event in workload.events:
            by_memory.setdefault(event.expected_fact_ids[0].rsplit("_", 1)[0], []).append(event.timestamp)
        for times in by_memory.values():
            gaps += [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        return sorted(gaps)[len(gaps) // 2]
    assert median_gap("bursty") < median_gap("stable") / 5


@pytest.mark.parametrize("mode", DEMAND_MODES)
def test_simulator_episodes_and_access_counts_match_metadata(mode):
    """The answer key must agree with the real engine's stale episodes."""
    workload = generate_evolving(EvolvingConfig(num_memories=120, demand_mode=mode, changes_per_memory=4,
                                                facts_per_summary=(1, 6)))
    result, ledger, _ = replay(workload, VerifyOnly())
    seen = Counter()
    fresh = Counter()
    for row in query_rows(result):
        if row["stale_episode_id"] is None:
            fresh[row["summary_id"]] += 1
        else:
            seen[row["stale_episode_id"]] += 1
        assert row["used_stale_source"] is False and row["required_fact_current"] is True
    expected = {e.episode_id: e.accesses for e in workload.episodes if e.accesses}
    assert dict(seen) == expected
    assert dict(fresh) == {m.summary_id: m.fresh_queries for m in workload.memories if m.fresh_queries}
    started = {row["event_id"] for row in result.logs if row["event_type"] in ("correction", "expiry")}
    assert {e.episode_id.split(":", 1)[1] for e in workload.episodes} == started


def test_eager_refresh_replays_every_change_without_stale_sources():
    workload = generate_evolving(EvolvingConfig(num_memories=80, changes_per_memory=3))
    result, _, _ = replay(workload, EagerRefresh())
    assert result.repairs == len(workload.episodes)
    assert result.stale_source_uses == 0 and result.verifications == 0


# Small hand-built regression traces (step 1.5).

def build(changes, queries, facts=2, memories=1, hours=1.0, links=None):
    """Construct a tiny valid workload from (minute, memory, slot, kind, value, minutes_valid).

    ``links`` maps a memory number to (earlier memory, slot) it also reads.
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
        names = list(attributes)
        if m in links:
            other, slot = links[m]
            names.append(f"linked_attr{slot:02d}")
            slots[names[-1]] = f"memory_{other:06d}_attr{slot:02d}"
        summaries.append({"summary_id": summary_id, "entity": entity, "created_at": stamp(START), "slots": slots,
                          "template": f"{entity} record: " + "; ".join(f"{a} {{{a}}}" for a in names) + "."})
        fact_rows += [{"fact_id": f, "entity": entity, "attribute": a, "value": "amber",
                       "valid_from": stamp(START)} for a, f in slots.items() if not a.startswith("linked_")]
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


C, U, R, T = (ChangeType.CORRECTION, ChangeType.UPDATE, ChangeType.RETRACTION, ChangeType.TEMPORARY_EXCEPTION)


def test_repeated_corrections_split_episodes_and_keep_stale_since():
    workload = build(changes=[(10, 1, 1, C, "birch", None), (20, 1, 2, U, "cedar", None)],
                     queries=[(5, 1, 1), (15, 1, 1), (25, 1, 2), (30, 1, 1)])
    assert workload.memories[0].fresh_queries == 1
    assert [(e.cause, e.accesses) for e in workload.episodes] == [("CORRECTION", 1), ("UPDATE", 2)]
    assert workload.episodes[0].end_time == workload.episodes[1].start_time
    result, _, _ = replay(workload, VerifyOnly())
    rows = query_rows(result)
    assert [r["stale_episode_id"] for r in rows] == [None] + [e.episode_id for e in workload.episodes
                                                               for _ in range(e.accesses)]


def test_stale_since_is_uninterrupted_across_episodes():
    workload = build(changes=[(10, 1, 1, C, "birch", None), (20, 1, 2, C, "cedar", None)], queries=[(30, 1, 1)])
    db, ledger, store, retriever, events = load_evolving(workload)
    try:
        Simulator(ledger, store, retriever, VerifyOnly(), evolving_run_config(workload)).run(events)
        summary = store.get_summary("memory_000001")
        assert summary.is_stale and summary.stale_since == START + timedelta(minutes=10)
        assert summary.stale_episode_id == workload.episodes[-1].episode_id
        history = ledger.get_history("memory_000001_attr01")
        assert [v.change_type for v in history] == [ChangeType.INITIAL, C]  # immutable history
    finally:
        db.close()


def test_effective_expiry_starts_an_episode_and_restores_the_value():
    workload = build(changes=[(10, 1, 1, T, "ember", 20)], queries=[(15, 1, 1), (40, 1, 1), (45, 1, 1)])
    assert [(e.cause, e.accesses) for e in workload.episodes] == [("TEMPORARY_EXCEPTION", 1), (EXPIRY, 2)]
    result, _, _ = replay(workload, VerifyOnly())
    assert [r["stale_episode_id"] for r in query_rows(result)][1:] == [workload.episodes[1].episode_id] * 2
    evidence = query_rows(result)[-1]["current_evidence"]
    assert "amber" in json.dumps(evidence)  # prior value is current again


def test_superseded_expiry_is_skipped_like_the_engine():
    workload = build(changes=[(10, 1, 1, T, "ember", 30), (20, 1, 1, C, "fjord", None)],
                     queries=[(15, 1, 1), (50, 1, 1)])
    assert [e.cause for e in workload.episodes] == ["TEMPORARY_EXCEPTION", "CORRECTION"]
    result, _, _ = replay(workload, VerifyOnly())
    assert not any(row["event_type"] == "expiry" and row["summary_id"] for row in result.logs)
    assert query_rows(result)[-1]["stale_episode_id"] == workload.episodes[-1].episode_id


def test_retraction_and_never_queried_memory():
    workload = build(changes=[(10, 1, 1, R, None, None), (20, 2, 1, C, "birch", None)],
                     queries=[(30, 1, 1)], memories=2)
    assert [(e.summary_id, e.cause, e.accesses) for e in workload.episodes] == [
        ("memory_000001", "RETRACTION", 1), ("memory_000002", "CORRECTION", 0)]
    result, _, _ = replay(workload, EagerRefresh())
    assert result.repairs == 2 and result.stale_source_uses == 0


def test_fan_out_change_starts_episodes_in_every_dependent_summary():
    workload = build(changes=[(10, 1, 1, C, "birch", None)], queries=[(20, 1, 1), (25, 2, 1), (30, 2, 2)],
                     memories=2, links={2: (1, 1)})
    assert [(e.summary_id, e.episode_id.split(":")[1], e.accesses) for e in workload.episodes] == [
        ("memory_000001", "change_000000001", 1), ("memory_000002", "change_000000001", 2)]
    result, _, _ = replay(workload, VerifyOnly())
    assert [r["stale_episode_id"] for r in query_rows(result)] == [e.episode_id for e in workload.episodes
                                                                    for _ in range(e.accesses)]


def test_generated_fan_out_is_bounded_and_matches_the_engine():
    workload = generate_evolving(EvolvingConfig(num_memories=150, fan_out=3, shared_fact_rate=0.6,
                                                changes_per_memory=4))
    readers = Counter(f for spec in workload.summaries for f in spec["slots"].values())
    assert max(readers.values()) == 3
    result, _, _ = replay(workload, VerifyOnly())
    seen = Counter(r["stale_episode_id"] for r in query_rows(result) if r["stale_episode_id"])
    assert dict(seen) == {e.episode_id: e.accesses for e in workload.episodes if e.accesses}
    workload.config = replace(workload.config, fan_out=2)
    with pytest.raises(ValueError):
        workload.validate()


@pytest.mark.parametrize("case, message", [
    ("episodes", "episode metadata"),
    ("order", "strictly increasing"),
    ("noop", "does not change"),
    ("query_text", "name one memory"),
    ("phase_count", "phase query counts"),
])
def test_inconsistent_inputs_are_rejected(case, message):
    workload = build(changes=[(10, 1, 1, C, "birch", None)], queries=[(15, 1, 1), (20, 1, 1)])
    if case == "episodes":
        workload.episodes = [replace(workload.episodes[0], accesses=5)]
    elif case == "order":
        workload.events = list(reversed(workload.events))
    elif case == "noop":
        workload.events[0] = replace(workload.events[0], value="amber")
    elif case == "query_text":
        workload.events[1] = replace(workload.events[1], query_text="lookup Entity999999")
    else:
        workload.memories = [replace(workload.memories[0], phase_queries=(1,))]
    with pytest.raises(ValueError, match=message):
        workload.validate()


def test_read_workload_dispatches_on_format(tmp_path):
    generate_synthetic(SyntheticConfig(num_memories=3)).write(tmp_path / "v1.json")
    generate_evolving(EvolvingConfig(num_memories=3)).write(tmp_path / "v2.json")
    assert read_workload(tmp_path / "v1.json").config == SyntheticConfig(num_memories=3)
    assert read_workload(tmp_path / "v2.json").config == EvolvingConfig(num_memories=3)


def test_cli_generates_and_replays_v2_reproducibly(tmp_path, capsys):
    outputs = []
    for hash_seed in ("0", "1"):
        path = tmp_path / f"v2-{hash_seed}.json"
        subprocess.run([sys.executable, "-m", "verify_repair.cli", "generate-evolving", "--memories", "30",
                        "--demand-mode", "shifting", "--phases", "2", "--facts-per-summary", "1", "5",
                        "--output", str(path)], check=True, capture_output=True, cwd=tmp_path,
                       env={**os.environ, "PYTHONHASHSEED": hash_seed,
                            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        outputs.append(hashlib.sha256(path.read_bytes()).hexdigest())
    assert outputs[0] == outputs[1]
    main(["run-synthetic", "--workload", str(tmp_path / "v2-0.json"), "--policy", "p1"])
    assert "policy=p1" in capsys.readouterr().out
