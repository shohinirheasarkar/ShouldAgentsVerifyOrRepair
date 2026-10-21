"""Controlled workloads: exact demand, seeded artifacts, and real simulator replay.

Run with ``python -m pytest -q tests/test_synthetic.py``; no models or network.
"""
from collections import Counter
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys

import pytest

from verify_repair.policies.eager_refresh import EagerRefresh
from verify_repair.policies.oldest_first import OldestFirst
from verify_repair.policies.periodic_refresh import PeriodicRefresh
from verify_repair.policies.repair_on_first_access import RepairOnFirstAccess
from verify_repair.policies.verify_only import VerifyOnly
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.events import CorrectionEvent, QueryEvent
from verify_repair.simulator.synthetic import (
    GROUP_BOUNDS, SyntheticConfig, generate_synthetic, load_synthetic,
    read_synthetic, synthetic_run_config,
)


def assert_workload_shape(workload):
    count = workload.config.num_memories
    assert len(workload.facts) == 3 * count
    assert len(workload.summaries) == len(workload.demands) == count
    assert len({fact["fact_id"] for fact in workload.facts}) == 3 * count
    assert len({spec["summary_id"] for spec in workload.summaries}) == count
    assert len({demand.entity for demand in workload.demands}) == count
    assert Counter(demand.group for demand in workload.demands) == {
        group: size for group, size in zip(GROUP_BOUNDS, workload.config.group_counts) if size
    }
    corrections = workload.events[:count]
    questions = workload.events[count:]
    assert all(isinstance(event, CorrectionEvent) for event in corrections)
    assert all(isinstance(event, QueryEvent) for event in questions)
    assert len({event.event_id for event in workload.events}) == len(workload.events)
    assert [event.sequence_number for event in workload.events] == list(range(1, len(workload.events) + 1))
    assert all(a.timestamp < b.timestamp for a, b in zip(workload.events, workload.events[1:]))
    initial = {fact["fact_id"]: fact["value"] for fact in workload.facts}
    assert all(initial[event.fact_id] != event.value for event in corrections)
    assert Counter(event.fact_id for event in corrections) == Counter(d.corrected_fact_id for d in workload.demands)
    accesses = Counter(event.expected_fact_ids[0] for event in questions)
    for demand in workload.demands:
        low, high = getattr(workload.config, f"{demand.group}_queries")
        assert low <= demand.future_queries <= high
        assert accesses[demand.corrected_fact_id] == demand.future_queries
    return questions


@pytest.mark.parametrize("kwargs", [
    {"num_memories": 0}, {"num_memories": -1}, {"num_memories": 1.5}, {"num_memories": True},
    {"seed": 1.5}, {"seed": True},
    {"group_counts": (1, 2)}, {"group_counts": (30, 30, 30)},
    {"group_counts": (-1, 1, 100)}, {"group_counts": (0, 0, 100.0)},
    {"cold_queries": (-1, 2)}, {"cold_queries": (0, 3)}, {"cold_queries": (2, 1)},
    {"cold_queries": (0, 1.5)}, {"cold_queries": (1,)},
    {"warm_queries": (2, 10)}, {"warm_queries": (3, 11)},
    {"hot_queries": (10, 50)}, {"hot_queries": (11, 51)},
])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        SyntheticConfig(**kwargs)


def test_100_memories_structure_seed_isolation_and_saved_round_trip(tmp_path):
    state = random.getstate()
    workload = generate_synthetic(SyntheticConfig())
    assert random.getstate() == state
    assert workload.config.group_counts == (34, 33, 33)
    assert {d.entity_type for d in workload.demands} == {"person", "project", "place"}
    assert_workload_shape(workload)
    assert workload == generate_synthetic(SyntheticConfig())
    changed = generate_synthetic(SyntheticConfig(seed=54321))
    assert workload.config.dataset_id() != changed.config.dataset_id()
    assert workload.events != changed.events
    assert workload.demands != changed.demands
    path = tmp_path / "workload.json"
    digest = workload.write(path)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    restored = read_synthetic(path)
    assert restored == workload
    assert restored.write(tmp_path / "roundtrip.json") == digest
    with pytest.raises(FileExistsError):
        changed.write(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("count, expected", [(1, (1, 0, 0)), (2, (1, 1, 0)),
                                              (3, (1, 1, 1)), (4, (2, 1, 1))])
def test_small_balanced_populations(count, expected):
    workload = generate_synthetic(SyntheticConfig(num_memories=count))
    assert workload.config.group_counts == expected
    assert_workload_shape(workload)


def test_exact_counts_including_never_accessed_memories():
    workload = generate_synthetic(SyntheticConfig(num_memories=100, group_counts=(50, 30, 20),
                                                cold_queries=(0, 0), warm_queries=(7, 7), hot_queries=(50, 50)))
    assert len(assert_workload_shape(workload)) == 30 * 7 + 20 * 50
    assert {d.future_queries for d in workload.demands} == {0, 7, 50}
    cold_only = generate_synthetic(SyntheticConfig(num_memories=100, group_counts=(100, 0, 0), cold_queries=(0, 0)))
    assert assert_workload_shape(cold_only) == []
    db, ledger, store, retriever, events = load_synthetic(cold_only)
    try:
        result = Simulator(ledger, store, retriever, RepairOnFirstAccess(), synthetic_run_config(cold_only)).run(events)
        assert result.queries == result.repairs == result.verifications == 0
        assert len(store.list_stale_summary_ids()) == 100
    finally:
        db.close()


@pytest.mark.parametrize("policy_factory", [VerifyOnly, EagerRefresh, RepairOnFirstAccess,
                                            PeriodicRefresh, OldestFirst])
def test_100_memory_replay_exact_demand_current_evidence_and_immutable_history(policy_factory):
    workload = generate_synthetic()
    original_events = list(workload.events)
    db, ledger, store, retriever, events = load_synthetic(workload)
    try:
        initial_rows = list(map(tuple, db.execute("SELECT * FROM fact_versions ORDER BY fact_id")))
        texts = {d.summary_id: store.get_summary(d.summary_id).text for d in workload.demands}
        sources = {d.summary_id: store.get_source_facts(d.summary_id) for d in workload.demands}
        assert store.list_stale_summary_ids() == []
        assert all(ref.version == 1 for refs in sources.values() for ref in refs.values())
        # Every intended query must find exactly its own memory, even while stale.
        targets = {d.corrected_fact_id: d.summary_id for d in workload.demands}
        for event in events[100:]:
            assert [h.summary_id for h in retriever.search(event.query_text, 2)] == [targets[event.expected_fact_ids[0]]]
        result = Simulator(ledger, store, retriever, policy_factory(), synthetic_run_config(workload)).run(events)
        query_rows = [row for row in result.logs if row["event_type"] == "query" and row["maintenance_trigger"] is None]
        assert result.queries == len(query_rows) == sum(d.future_queries for d in workload.demands)
        assert result.corrections == 100
        assert result.stale_source_uses == 0
        assert all(row["required_fact_current"] for row in query_rows)
        accesses = Counter(row["summary_id"] for row in query_rows)
        assert all(accesses[d.summary_id] == d.future_queries for d in workload.demands)
        assert all(row["seed"] == workload.config.seed and row["dataset_id"] == workload.config.dataset_id()
                   for row in result.logs)
        assert all("demands" not in row and "group" not in row["config_snapshot"] for row in result.logs)
        assert initial_rows == list(map(tuple, db.execute("SELECT * FROM fact_versions WHERE version=1 ORDER BY fact_id")))
        assert db.execute("SELECT count(*) FROM fact_versions").fetchone()[0] == 400
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM fact_versions WHERE version=1")
        for event in events[100:]:
            assert [h.summary_id for h in retriever.search(event.query_text, 2)] == [targets[event.expected_fact_ids[0]]]
        for demand in workload.demands:
            summary = store.get_summary(demand.summary_id)
            if policy_factory is VerifyOnly or (policy_factory is RepairOnFirstAccess and demand.future_queries == 0):
                assert summary.is_stale
                assert summary.text == texts[demand.summary_id]
                assert store.get_source_facts(demand.summary_id) == sources[demand.summary_id]
            elif policy_factory in (EagerRefresh, RepairOnFirstAccess):
                assert not summary.is_stale
                assert summary.text != texts[demand.summary_id]
                refs = store.get_source_facts(demand.summary_id)
                assert next(ref.version for ref in refs.values() if ref.fact_id == demand.corrected_fact_id) == 2
        if policy_factory is VerifyOnly:
            assert result.repairs == 0 and result.verifications == result.queries
        elif policy_factory is EagerRefresh:
            assert result.repairs == 100 and result.verifications == 0
        elif policy_factory is RepairOnFirstAccess:
            assert result.repairs == sum(d.future_queries > 0 for d in workload.demands)
            assert result.verifications == 0
        assert workload.events == original_events
    finally:
        db.close()


def test_saved_workload_replay_produces_identical_logs(tmp_path):
    workload = generate_synthetic()
    workload.write(tmp_path / "inputs.json")
    runs = []
    for inputs in (workload, read_synthetic(tmp_path / "inputs.json")):
        db, ledger, store, retriever, events = load_synthetic(inputs)
        try:
            runs.append(Simulator(ledger, store, retriever, VerifyOnly(), synthetic_run_config(inputs)).run(events))
        finally:
            db.close()
    assert runs[0] == runs[1]


@pytest.mark.parametrize("count", [1000, 10000])
def test_larger_workloads_reproduce_identical_artifacts(count, tmp_path):
    config = SyntheticConfig(num_memories=count)
    first = generate_synthetic(config)
    assert_workload_shape(first)
    first_hash = first.write(tmp_path / "first.json")
    second = generate_synthetic(config)
    assert first == second
    assert first_hash == second.write(tmp_path / "second.json")


@pytest.mark.parametrize("field, value, message", [
    ("format_version", "synthetic-v999", "unsupported synthetic format"),
    ("dataset_id", "wrong", "does not match"),
])
def test_saved_workload_rejects_invalid_header(tmp_path, field, value, message):
    payload = generate_synthetic(SyntheticConfig(num_memories=1)).to_dict()
    payload[field] = value
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=message):
        read_synthetic(path)


def test_saved_workload_rejects_unknown_event_type(tmp_path):
    payload = generate_synthetic(SyntheticConfig(num_memories=1)).to_dict()
    payload["events"][0]["type"] = "unexpected"
    path = tmp_path / "bad-event.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="unsupported synthetic event type"):
        read_synthetic(path)


@pytest.mark.parametrize("case, message", [
    ("wrong_demand_count", "do not match query events"),
    ("missing_summary", "match num_memories"),
    ("duplicate_fact", "unique IDs"),
    ("missing_source", "independent facts"),
    ("shared_source", "independent facts"),
    ("wrong_group_sizes", "match configured group_counts"),
    ("unknown_summary", "match num_memories"),
    ("foreign_corrected_fact", "source fact of its summary"),
    ("missing_correction", "one correction per memory"),
    ("no_change", "change the initial fact value"),
    ("unknown_query_target", "do not match query events"),
    ("multiple_hits", "retrieve one summary"),
    ("duplicate_query_id", "query IDs must be unique"),
    ("duplicate_event_id", "event IDs and query IDs must be unique"),
    ("out_of_order", "strictly increasing"),
])
def test_inconsistent_saved_inputs_are_rejected(tmp_path, case, message):
    payload = generate_synthetic(SyntheticConfig(num_memories=3)).to_dict()
    cold = next(d for d in payload["demands"] if d["group"] == "cold")
    if case == "wrong_demand_count":
        cold["future_queries"] = (cold["future_queries"] + 1) % 3
    elif case == "missing_summary":
        payload["summaries"].pop()
    elif case == "duplicate_fact":
        payload["facts"][1]["fact_id"] = payload["facts"][0]["fact_id"]
    elif case == "missing_source":
        payload["summaries"][0]["slots"]["name"] = "missing"
    elif case == "shared_source":
        payload["summaries"][1]["slots"]["name"] = payload["summaries"][0]["slots"]["name"]
    elif case == "wrong_group_sizes":
        cold["group"], cold["future_queries"] = "warm", 3
    elif case == "unknown_summary":
        cold["summary_id"] = "missing"
    elif case == "foreign_corrected_fact":
        cold["corrected_fact_id"] = next(d["corrected_fact_id"] for d in payload["demands"] if d is not cold)
    elif case == "missing_correction":
        payload["events"].pop(0)
    elif case == "no_change":
        event = payload["events"][0]
        event["value"] = next(f["value"] for f in payload["facts"] if f["fact_id"] == event["fact_id"])
    elif case == "unknown_query_target":
        payload["events"][3]["expected_fact_ids"] = ["missing"]
    elif case == "multiple_hits":
        payload["events"][3]["top_k"] = 2
    elif case == "duplicate_query_id":
        payload["events"][4]["query_id"] = payload["events"][3]["query_id"]
    elif case == "duplicate_event_id":
        payload["events"][1]["event_id"] = payload["events"][0]["event_id"]
    elif case == "out_of_order":
        payload["events"][-1]["timestamp"] = payload["events"][0]["timestamp"]
    path = tmp_path / "inconsistent.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=message):
        read_synthetic(path)


def test_invalid_workload_is_rejected_before_write_or_database_open(tmp_path, monkeypatch):
    workload = generate_synthetic(SyntheticConfig(num_memories=3))
    index = next(i for i, d in enumerate(workload.demands) if d.group == "cold")
    demand = workload.demands[index]
    workload.demands[index] = replace(demand, future_queries=(demand.future_queries + 1) % 3)
    path = tmp_path / "invalid.json"
    with pytest.raises(ValueError, match="do not match query events"):
        workload.write(path)
    assert not path.exists()

    def unexpected_connection():
        pytest.fail("invalid inputs must be rejected before opening SQLite")

    monkeypatch.setattr("verify_repair.simulator.synthetic.connect", unexpected_connection)
    with pytest.raises(ValueError, match="do not match query events"):
        load_synthetic(workload)


@pytest.mark.parametrize("count", range(51))
def test_every_future_query_count_reaches_its_memory(count):
    group = "cold" if count <= 2 else "warm" if count <= 10 else "hot"
    config = SyntheticConfig(num_memories=1, group_counts=tuple(int(g == group) for g in GROUP_BOUNDS),
                             **{f"{group}_queries": (count, count)})
    workload = generate_synthetic(config)
    assert len(assert_workload_shape(workload)) == count
    for policy in (VerifyOnly(), EagerRefresh(), RepairOnFirstAccess()):
        db, ledger, store, retriever, events = load_synthetic(workload)
        try:
            result = Simulator(ledger, store, retriever, policy, synthetic_run_config(workload)).run(events)
            assert result.queries == count
            assert result.stale_source_uses == 0
            rows = [row for row in result.logs if row["event_type"] == "query"]
            assert len(rows) == count
            assert all(row["summary_id"] == workload.demands[0].summary_id and row["required_fact_current"]
                       for row in rows)
            if isinstance(policy, VerifyOnly):
                assert result.repairs == 0 and result.verifications == count
            else:
                assert result.repairs == (1 if isinstance(policy, EagerRefresh) else int(count > 0))
                assert result.verifications == 0
        finally:
            db.close()


def test_cli_generation_is_reproducible_across_process_hash_seeds(tmp_path):
    hashes = []
    for hash_seed in ("0", "77", "random"):
        path = tmp_path / f"seed-{hash_seed}.json"
        env = dict(os.environ, PYTHONHASHSEED=hash_seed,
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        subprocess.run([sys.executable, "-m", "verify_repair.cli", "generate-synthetic",
                        "--memories", "100", "--seed", "42", "--output", str(path)],
                       cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    assert len(set(hashes)) == 1
