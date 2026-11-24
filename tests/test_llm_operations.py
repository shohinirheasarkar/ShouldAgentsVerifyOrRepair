"""Offline FakeLLMClient tests for LLM VERIFY, LLM REPAIR, retries, timeouts, and rollback."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from verify_repair.db import connect
from verify_repair.llm.client import FakeLLMClient, LLMConfig
from verify_repair.llm.operations import LLMSummaryBuilder, LLMVerifier
from verify_repair.maintenance.operations import MaintenanceService
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.staleness import mark_stale_from_fact_change
from verify_repair.memory.summaries import SummaryStore
from verify_repair.models import (
    ChangeType,
    FactVersionRef,
    LLMTimeoutError,
    LLMValidationError,
    LLMVerificationError,
)
from verify_repair.policies.predicted_threshold import PredictedThreshold
from verify_repair.policies.repair_on_first_access import RepairOnFirstAccess
from verify_repair.policies.verify_only import VerifyOnly
from verify_repair.retrieval.keyword import KeywordRetriever
from verify_repair.simulator.budget import RepairBudget
from verify_repair.simulator.engine import RunConfig, Simulator
from verify_repair.simulator.events import CorrectionEvent, QueryEvent

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)


def _setup_alice():
    db = connect()
    ledger, store = FactLedger(db), SummaryStore(db)
    ledger.append_initial_fact("alice_name", "Alice", "name", "Alice", T0, "init_name")
    ledger.append_initial_fact("alice_job", "Alice", "job", "researcher", T0, "init_job")
    ledger.append_initial_fact("alice_location", "Alice", "location", "Maryland", T0, "init_loc")
    store.create_summary(
        "alice_profile",
        "Alice is a researcher living in Maryland.",
        "{name} is a {job} living in {location}.",
        {
            "name": FactVersionRef("alice_name", 1),
            "job": FactVersionRef("alice_job", 1),
            "location": FactVersionRef("alice_location", 1),
        },
        T0,
    )
    ledger.append_correction("alice_location", "Boston", T1, "alice_move")
    mark_stale_from_fact_change(store, "alice_location", "alice_move", T1)
    return db, ledger, store


def _valid_verify_json():
    return json.dumps({
        "verified_evidence": "Alice is a researcher currently living in Boston.",
        "cited_sources": ["alice_name@v1", "alice_job@v1", "alice_location@v2"],
        "retracted_slots": [],
    })


def _valid_repair_json():
    return json.dumps({
        "summary_text": "Alice works as a researcher and lives in Boston.",
        "claims": [
            {"slot": "name", "statement": "Name is Alice.", "source_ref": "alice_name@v1"},
            {"slot": "job", "statement": "Alice is a researcher.", "source_ref": "alice_job@v1"},
            {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"},
        ],
    })


def test_llm_verify_does_not_mutate_summary_or_provenance():
    db, ledger, store = _setup_alice()
    try:
        client = FakeLLMClient([_valid_verify_json()])
        service = MaintenanceService(ledger, store, verifier=LLMVerifier(client))
        res = service.verify("alice_profile", T1, query_text="Where does Alice live?")
        assert res.verified_context == "Alice is a researcher currently living in Boston."
        assert res.cited_sources == ("alice_name@v1", "alice_job@v1", "alice_location@v2")
        assert res.llm_metadata["attempts"] == 1
        assert res.llm_metadata["usage"]["total_tokens"] > 0

        # Non-mutation invariants
        summary = store.get_summary("alice_profile")
        assert summary.text == "Alice is a researcher living in Maryland."
        assert summary.is_stale
        assert summary.queries_this_episode == 1
        assert store.get_source_facts("alice_profile")["location"] == FactVersionRef("alice_location", 1)
        assert db.execute("SELECT text FROM summary_fts WHERE summary_id='alice_profile'").fetchone()[0] == (
            "Alice is a researcher living in Maryland."
        )
    finally:
        db.close()


def test_llm_verify_timeout_and_validation_failure_roll_back_counters():
    db, ledger, store = _setup_alice()
    try:
        timeout_client = FakeLLMClient([LLMTimeoutError("verify deadline exceeded")])
        service = MaintenanceService(ledger, store, verifier=LLMVerifier(timeout_client))
        with pytest.raises(LLMTimeoutError):
            service.verify("alice_profile", T1, query_text="Where does Alice live?")
        assert store.get_summary("alice_profile").queries_this_episode == 0
        assert store.get_summary("alice_profile").verification_cost_this_episode == 0.0

        bad_json = json.dumps({
            "verified_evidence": "Alice lives in Boston.",
            "cited_sources": ["alice_location@v1"],  # stale ref!
            "retracted_slots": [],
        })
        cfg = LLMConfig(backend="fake", model_id="fake", max_retries=1)
        fail_client = FakeLLMClient([bad_json, bad_json], config=cfg)
        service2 = MaintenanceService(ledger, store, verifier=LLMVerifier(fail_client))
        with pytest.raises(LLMVerificationError, match="after 2 attempt"):
            service2.verify("alice_profile", T1, query_text="Where does Alice live?")
        assert len(fail_client.requests) == 2
        assert "validation_feedback" in fail_client.requests[1].user_prompt
        assert store.get_summary("alice_profile").queries_this_episode == 0
    finally:
        db.close()


def test_llm_repair_updates_text_provenance_fts_semantic_and_recovers_on_retry():
    db, ledger, store = _setup_alice()
    try:
        bad_first = json.dumps({
            "summary_text": "Alice lives in Boston.",  # dropped 'researcher'!
            "claims": [
                {"slot": "name", "statement": "Alice.", "source_ref": "alice_name@v1"},
                {"slot": "job", "statement": "Alice is a researcher.", "source_ref": "alice_job@v1"},
                {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"},
            ],
        })
        cfg = LLMConfig(backend="fake", model_id="fake", max_retries=1)
        client = FakeLLMClient([bad_first, _valid_repair_json()], config=cfg)
        semantic = {"alice_profile": "Alice is a researcher living in Maryland."}

        service = MaintenanceService(
            ledger,
            store,
            builder=LLMSummaryBuilder(client),
            semantic_index_update=lambda sid, text: semantic.__setitem__(sid, text),
            semantic_index_rebuild=lambda: semantic.__setitem__(
                "alice_profile", store.get_summary("alice_profile").text
            ),
        )
        repaired = service.repair("alice_profile", T1)
        assert len(client.requests) == 2
        assert "validation_feedback" in client.requests[1].user_prompt
        assert repaired.llm_metadata["attempts"] == 2
        assert repaired.fidelity["unchanged_preserved_ratio"] == 1.0
        assert "Alice" in repaired.text and "researcher" in repaired.text and "Boston" in repaired.text
        assert "Maryland" not in repaired.text

        summary = store.get_summary("alice_profile")
        assert not summary.is_stale
        assert summary.text == repaired.text
        assert store.get_source_facts("alice_profile")["location"] == FactVersionRef("alice_location", 2)
        assert semantic["alice_profile"] == repaired.text

        retriever = KeywordRetriever(db)
        assert retriever.search("Maryland", 5) == []
        assert retriever.search("Boston researcher", 1)[0].summary_id == "alice_profile"
    finally:
        db.close()


@pytest.mark.parametrize(
    "failure_response,expected_exc",
    [
        (
            json.dumps({
                "summary_text": "Alice is a researcher in Boston and won a Nobel Prize.",
                "claims": [
                    {"slot": "name", "statement": "Alice", "source_ref": "alice_name@v1"},
                    {"slot": "job", "statement": "researcher", "source_ref": "alice_job@v1"},
                    {"slot": "location", "statement": "Boston", "source_ref": "alice_location@v2"},
                    {"slot": "award", "statement": "Nobel Prize", "source_ref": "fake@v1"},
                ],
            }),
            LLMValidationError,
        ),
        (LLMTimeoutError("repair timed out"), LLMTimeoutError),
    ],
)
def test_llm_repair_failure_rolls_back_sqlite_fts_semantic_access_history_and_budget(
    failure_response, expected_exc
):
    db = connect()
    try:
        ledger, store = FactLedger(db), SummaryStore(db)
        ledger.append_initial_fact("alice_name", "Alice", "name", "Alice", T0, "init_name")
        ledger.append_initial_fact("alice_job", "Alice", "job", "researcher", T0, "init_job")
        ledger.append_initial_fact("alice_location", "Alice", "location", "Maryland", T0, "init_loc")
        store.create_summary(
            "alice_profile",
            "Alice is a researcher living in Maryland.",
            "{name} is a {job} living in {location}.",
            {
                "name": FactVersionRef("alice_name", 1),
                "job": FactVersionRef("alice_job", 1),
                "location": FactVersionRef("alice_location", 1),
            },
            T0,
        )
        events = [
            CorrectionEvent("alice_move", T1, 1, "alice_location", ChangeType.CORRECTION, "Boston"),
            QueryEvent("q1", T1 + timedelta(minutes=1), 2, "q1", "Alice researcher", 1, ("alice_location",)),
        ]
        cfg = LLMConfig(backend="fake", model_id="fake", max_retries=0)
        client = FakeLLMClient([failure_response], config=cfg)
        budget = RepairBudget(units=5.0, window_seconds=3600.0, origin=T0)

        sim = Simulator(
            ledger,
            store,
            KeywordRetriever(db),
            RepairOnFirstAccess(),
            RunConfig(verify_cost_units=1.0, repair_cost_units=5.0),
            budget=budget,
            summary_builder=LLMSummaryBuilder(client),
            llm_config=cfg,
        )
        with pytest.raises(expected_exc):
            sim.run(events)

        summary = store.get_summary("alice_profile")
        assert summary.is_stale
        assert summary.text == "Alice is a researcher living in Maryland."
        assert store.get_source_facts("alice_profile")["location"] == FactVersionRef("alice_location", 1)
        assert db.execute("SELECT text FROM summary_fts WHERE summary_id='alice_profile'").fetchone()[0] == (
            "Alice is a researcher living in Maryland."
        )
    finally:
        db.close()


def test_llm_repair_semantic_index_failure_rolls_back_and_rebuilds():
    db, ledger, store = _setup_alice()
    try:
        client = FakeLLMClient([_valid_repair_json()])
        semantic = {"alice_profile": "Alice is a researcher living in Maryland."}

        def broken_update(sid, text):
            semantic[sid] = text
            raise RuntimeError("injected semantic index crash")

        def rebuild():
            semantic["alice_profile"] = store.get_summary("alice_profile").text

        service = MaintenanceService(
            ledger,
            store,
            builder=LLMSummaryBuilder(client),
            semantic_index_update=broken_update,
            semantic_index_rebuild=rebuild,
        )
        with pytest.raises(RuntimeError, match="semantic index crash"):
            service.repair("alice_profile", T1)

        assert store.get_summary("alice_profile").is_stale
        assert store.get_summary("alice_profile").text == "Alice is a researcher living in Maryland."
        assert semantic["alice_profile"] == "Alice is a researcher living in Maryland."
        assert store.get_source_facts("alice_profile")["location"] == FactVersionRef("alice_location", 1)
    finally:
        db.close()


def test_llm_verify_failure_under_p2_rolls_back_access_history():
    db = connect()
    try:
        ledger, store = FactLedger(db), SummaryStore(db)
        ledger.append_initial_fact("alice_name", "Alice", "name", "Alice", T0, "init_name")
        ledger.append_initial_fact("alice_job", "Alice", "job", "researcher", T0, "init_job")
        ledger.append_initial_fact("alice_location", "Alice", "location", "Maryland", T0, "init_loc")
        store.create_summary(
            "alice_profile",
            "Alice is a researcher living in Maryland.",
            "{name} is a {job} living in {location}.",
            {
                "name": FactVersionRef("alice_name", 1),
                "job": FactVersionRef("alice_job", 1),
                "location": FactVersionRef("alice_location", 1),
            },
            T0,
        )
        events = [
            CorrectionEvent("alice_move", T1, 1, "alice_location", ChangeType.CORRECTION, "Boston"),
            QueryEvent("q1", T1 + timedelta(minutes=1), 2, "q1", "Alice researcher", 1, ("alice_location",)),
        ]
        client = FakeLLMClient([LLMTimeoutError("verify timeout")])
        sim = Simulator(
            ledger,
            store,
            KeywordRetriever(db),
            PredictedThreshold(),
            RunConfig(verify_cost_units=1.0, repair_cost_units=5.0),
            verifier=LLMVerifier(client),
            llm_config=client.config,
        )
        with pytest.raises(LLMTimeoutError):
            sim.run(events)
        assert db.execute("SELECT COUNT(*) FROM summary_access_history").fetchone()[0] == 0
        assert store.get_summary("alice_profile").queries_this_episode == 0
    finally:
        db.close()


def _fallback_run(policy, *, fallback, with_reader=False):
    """Alice trace where every LLM call returns invalid output (two attempts each)."""
    from verify_repair.llm.reader import AnswerReader

    db = connect()
    ledger, store = FactLedger(db), SummaryStore(db)
    for fact, attribute, value in (("alice_name", "name", "Alice"), ("alice_job", "job", "researcher"),
                                   ("alice_location", "location", "Maryland")):
        ledger.append_initial_fact(fact, "Alice", attribute, value, T0, f"init_{attribute}")
    store.create_summary(
        "alice_profile", "Alice is a researcher living in Maryland.", "{name} is a {job} living in {location}.",
        {"name": FactVersionRef("alice_name", 1), "job": FactVersionRef("alice_job", 1),
         "location": FactVersionRef("alice_location", 1)}, T0)
    events = [
        CorrectionEvent("alice_move", T1, 1, "alice_location", ChangeType.CORRECTION, "Boston"),
        QueryEvent("q1", T1 + timedelta(minutes=1), 2, "q1", "Alice researcher", 1, ("alice_location",)),
    ]
    cfg = LLMConfig(backend="fake", model_id="fake", max_retries=1)
    client = FakeLLMClient(handler=lambda request: "not json", config=cfg)
    sim = Simulator(
        ledger, store, KeywordRetriever(db), policy, RunConfig(), measure_latency=True,
        summary_builder=LLMSummaryBuilder(client), verifier=LLMVerifier(client),
        answer_reader=AnswerReader(client) if with_reader else None, llm_config=cfg, llm_fallback=fallback)
    return db, store, sim, events


@pytest.mark.parametrize("policy", [VerifyOnly(), RepairOnFirstAccess()])
def test_failed_llm_operations_still_raise_without_fallback(policy):
    db, store, sim, events = _fallback_run(policy, fallback=False)
    try:
        with pytest.raises((LLMVerificationError, LLMValidationError)):
            sim.run(events)
        assert store.get_summary("alice_profile").text == "Alice is a researcher living in Maryland."
    finally:
        db.close()


def test_verify_fallback_answers_from_current_facts_without_touching_the_summary():
    db, store, sim, events = _fallback_run(VerifyOnly(), fallback=True, with_reader=True)
    try:
        result = sim.run(events)
        row = next(r for r in result.logs if r["action"] == "VERIFY")
        meta = row["maintenance_llm_metadata"]
        assert meta["fallback"] == "deterministic" and meta["attempts"] == 2
        assert meta["usage"]["prompt_tokens"] > 0  # the failed attempts are still charged
        assert row["current_evidence"]["location"] == "Boston"
        assert row["timing_ms"]["verify_generation"] >= 0
        summary = store.get_summary("alice_profile")
        assert summary.is_stale and summary.text == "Alice is a researcher living in Maryland."
        # The reader also fails, so the answer becomes a logged abstention.
        (answer,) = result.answers
        assert answer["abstained"] and answer["llm_metadata"]["fallback"] == "abstain"
        assert result.logs[0]["config_snapshot"]["llm_fallback"] == "deterministic"
    finally:
        db.close()


def test_repair_fallback_rebuilds_from_the_template_with_exact_provenance():
    db, store, sim, events = _fallback_run(RepairOnFirstAccess(), fallback=True)
    try:
        result = sim.run(events)
        row = next(r for r in result.logs if r["action"] == "REPAIR")
        assert row["maintenance_llm_metadata"]["fallback"] == "deterministic"
        summary = store.get_summary("alice_profile")
        assert not summary.is_stale and summary.text == "Alice is a researcher living in Boston."
        assert store.get_source_facts("alice_profile")["location"] == FactVersionRef("alice_location", 2)
        assert db.execute("SELECT text FROM summary_fts WHERE summary_id='alice_profile'").fetchone()[0] == (
            "Alice is a researcher living in Boston.")
    finally:
        db.close()
