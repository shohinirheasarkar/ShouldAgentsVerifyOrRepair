"""Offline tests for frozen AnswerReader, event-time answer scoring, and Step 5.6 canonical check."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verify_repair.answer_scoring import (
    exact_match,
    resolve_event_time_truth,
    score_run_answers,
    token_f1,
)
from verify_repair.db import connect
from verify_repair.llm.client import FakeLLMClient, LLMRequest
from verify_repair.llm.operations import LLMSummaryBuilder, LLMVerifier
from verify_repair.llm.reader import AnswerReader, EvidencePacket, format_fact_keys, render_reader_question
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.summaries import SummaryStore
from verify_repair.models import ChangeType, FactVersionRef
from verify_repair.policies.accumulated_threshold import AccumulatedThreshold
from verify_repair.policies.eager_refresh import EagerRefresh
from verify_repair.policies.periodic_refresh import PeriodicRefresh
from verify_repair.policies.periodic_refresh_stale import PeriodicRefreshStale
from verify_repair.policies.predicted_threshold import PredictedThreshold
from verify_repair.policies.repair_on_first_access import RepairOnFirstAccess
from verify_repair.policies.verify_only import VerifyOnly
from verify_repair.retrieval.keyword import KeywordRetriever
from verify_repair.simulator.engine import RunConfig, Simulator
from verify_repair.simulator.events import CorrectionEvent, QueryEvent
from verify_repair.simulator.evolving import EvolvingConfig, generate_evolving, load_evolving
from verify_repair.simulator.synthetic import SyntheticConfig, generate_synthetic, load_synthetic
from verify_repair.simulator.toy import load_toy

DATA = Path(__file__).resolve().parents[1] / "data/toy"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _smart_toy_handler(request: LLMRequest) -> str:
    if request.role == "verify":
        if "Boston" in request.user_prompt and "researcher" in request.user_prompt:
            return json.dumps({
                "verified_evidence": "Alice is a researcher living in Boston.",
                "cited_sources": ["alice_name@v1", "alice_job@v1", "alice_location@v2"],
                "retracted_slots": [],
            })
        if "Summary ID: alice_digest" in request.user_prompt:
            return json.dumps({
                "verified_evidence": "Alice profile digest: Boston.",
                "cited_sources": ["alice_location@v2"],
                "retracted_slots": [],
            })
        if "Room Z" in request.user_prompt:
            return json.dumps({
                "verified_evidence": "Alice works in Room Z.",
                "cited_sources": ["alice_office@v2"],
                "retracted_slots": [],
            })
        return json.dumps({
            "verified_evidence": "Alice works in Room A.",
            "cited_sources": ["alice_office@v1"],
            "retracted_slots": [],
        })
    if request.role == "repair":
        if "Summary ID: alice_profile" in request.user_prompt:
            return json.dumps({
                "summary_text": "Alice is a researcher living in Boston.",
                "claims": [
                    {"slot": "name", "statement": "Name is Alice.", "source_ref": "alice_name@v1"},
                    {"slot": "job", "statement": "Alice is a researcher.", "source_ref": "alice_job@v1"},
                    {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"},
                ],
            })
        if "Summary ID: alice_digest" in request.user_prompt:
            return json.dumps({
                "summary_text": "Alice profile digest: Boston.",
                "claims": [
                    {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"},
                ],
            })
        if "Summary ID: bob_profile" in request.user_prompt:
            return json.dumps({
                "summary_text": "Bob is an engineer living in Utah.",
                "claims": [
                    {"slot": "name", "statement": "Name is Bob.", "source_ref": "bob_name@v1"},
                    {"slot": "job", "statement": "Bob is an engineer.", "source_ref": "bob_job@v1"},
                    {"slot": "location", "statement": "Bob lives in Utah.", "source_ref": "bob_location@v2"},
                ],
            })
        if "Summary ID: erin_office" in request.user_prompt:
            return json.dumps({
                "summary_text": "Erin's office is retracted.",
                "claims": [
                    {"slot": "office", "statement": "Erin's office is retracted.", "source_ref": "erin_office@v2"},
                ],
            })
        if "Summary ID: alice_office" in request.user_prompt:
            if "value=Room Z" in request.user_prompt:
                return json.dumps({
                    "summary_text": "Alice works in Room Z.",
                    "claims": [
                        {"slot": "office", "statement": "Alice works in Room Z.", "source_ref": "alice_office@v2"},
                    ],
                })
            return json.dumps({
                "summary_text": "Alice works in Room A.",
                "claims": [
                    {"slot": "office", "statement": "Alice works in Room A.", "source_ref": "alice_office@v1"},
                ],
            })
    if request.role == "reader":
        if "office of Alice" in request.user_prompt:
            if "Room Z" in request.user_prompt:
                return json.dumps({"answer": "Room Z", "abstained": False, "cited_sources": ["alice_office@v2"]})
            if "Room A" in request.user_prompt:
                return json.dumps({"answer": "Room A", "abstained": False, "cited_sources": ["alice_office@v1"]})
            return json.dumps({"answer": "INSUFFICIENT_EVIDENCE", "abstained": True, "cited_sources": []})
        if "location of Alice" in request.user_prompt:
            if "Boston" in request.user_prompt:
                return json.dumps({"answer": "Boston", "abstained": False, "cited_sources": ["alice_location@v2"]})
            if "Maryland" in request.user_prompt:
                return json.dumps({"answer": "Maryland", "abstained": False, "cited_sources": ["alice_location@v1"]})
        return json.dumps({"answer": "INSUFFICIENT_EVIDENCE", "abstained": True, "cited_sources": []})
    raise ValueError(f"unhandled prompt for role={request.role}: {request.user_prompt}")


def test_exact_match_and_token_f1_normalization():
    assert exact_match("Boston", "boston.") == 1.0
    assert exact_match("The Boston", "Boston") == 1.0
    assert exact_match("Maryland", "Boston") == 0.0
    assert token_f1("Room Z", "Room Z") == 1.0
    assert 0.0 < token_f1("Room A", "Room Z") < 1.0
    assert token_f1("Maryland", "Boston") == 0.0


def test_reader_scope_guard_rejects_wrong_attribute_without_hiding_model_cost_or_answer():
    for question in ("What is the current office of Alice?", "What is Alice's office?"):
        client = FakeLLMClient([json.dumps({
            "answer": "Boston", "abstained": False, "cited_sources": ["alice_location@v2"],
        })])
        packet = EvidencePacket(
            "q", question, ("alice_profile",), "fresh_summary",
            "Alice is a researcher living in Boston.", ("alice_location@v2",),
            source_fact_keys=(("Alice", "name"), ("Alice", "job"), ("Alice", "location")),
        )
        answer = AnswerReader(client).answer(packet)
        assert answer.abstained and answer.answer_text == "INSUFFICIENT_EVIDENCE"
        assert answer.cited_sources == ()
        assert answer.llm_metadata["scope_guard"]["model_answer"] == "Boston"
        assert answer.llm_metadata["scope_guard"]["model_cited_sources"] == ["alice_location@v2"]
        assert answer.llm_metadata["usage"]["total_tokens"] > 0
        assert len(client.requests) == 1


def test_reader_scope_guard_preserves_stated_location_even_when_packet_is_stale():
    packet = EvidencePacket(
        "q", "What is the current location of Alice?", ("alice_profile",), "fresh_summary",
        "Alice is a researcher living in Maryland.", ("alice_location@v1",),
        source_fact_keys=(("Alice", "location"),),
    )
    prompts = set()
    for mode in ("fresh_summary", "stale_summary", "verified_evidence", "repaired_summary"):
        client = FakeLLMClient([json.dumps({
            "answer": "Maryland", "abstained": False, "cited_sources": ["alice_location@v1"],
        })])
        answer = AnswerReader(client).answer(replace(packet, evidence_mode=mode))
        assert answer.answer_text == "Maryland" and not answer.abstained
        assert "scope_guard" not in answer.llm_metadata
        prompts.add((client.requests[0].system_prompt, client.requests[0].user_prompt))
    assert len(prompts) == 1  # neither source freshness nor policy enters the prompt


def test_reader_scope_guard_accepts_office_evidence_and_checks_entity():
    for entity, expected_abstention in (("Alice", False), ("Bob", True)):
        client = FakeLLMClient([json.dumps({
            "answer": "Room A", "abstained": False, "cited_sources": ["office@v1"],
        })])
        answer = AnswerReader(client).answer(EvidencePacket(
            "q", "What is the current office of Alice?", ("office_summary",), "summary",
            f"{entity} works in Room A.", ("office@v1",), source_fact_keys=((entity, "office"),),
        ))
        assert answer.abstained is expected_abstention
        assert answer.answer_text == ("INSUFFICIENT_EVIDENCE" if expected_abstention else "Room A")


def test_simulator_supplies_source_identities_and_blocks_the_observed_wrong_office_answer():
    def wrong_office_handler(request):
        if request.role == "reader" and "office of Alice" in request.user_prompt:
            return json.dumps({
                "answer": "Boston", "abstained": False, "cited_sources": ["alice_location@v2"],
            })
        return _smart_toy_handler(request)

    db, ledger, store, retriever, events = load_toy(DATA)
    try:
        client = FakeLLMClient(handler=wrong_office_handler)
        result = Simulator(
            ledger, store, retriever, VerifyOnly(), RunConfig(), verifier=LLMVerifier(client),
            answer_reader=AnswerReader(client),
        ).run(events)
        office_answers = [a for a in result.answers if a["query_id"] in ("q4", "q5")]
        assert len(office_answers) == 2
        assert all(a["abstained"] and a["answer_text"] == "INSUFFICIENT_EVIDENCE" for a in office_answers)
        office_rows = [r for r in result.logs if r.get("query_id") in ("q4", "q5")]
        assert all(r["reader_llm_metadata"]["scope_guard"]["model_answer"] == "Boston" for r in office_rows)
        assert format_fact_keys(store.get_source_facts("alice_profile"), ledger) == (
            ("Alice", "job"), ("Alice", "location"), ("Alice", "name"),
        )
    finally:
        db.close()


def test_live_preflight_runs_offline_with_recorded_wrong_attribute_response(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("live_preflight", Path(__file__).with_name("test_llm_optional.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def handler(request):
        if request.role == "reader" and ("office of Alice" in request.user_prompt or "Alice's office" in request.user_prompt):
            return json.dumps({"answer": "Boston", "abstained": False, "cited_sources": ["alice_location@v2"]})
        return _smart_toy_handler(request)

    monkeypatch.setattr(module, "HuggingFace4BitClient", lambda cfg: FakeLLMClient(handler=handler, config=cfg))
    monkeypatch.setenv("LLM_TEST_OUTPUT_DIR", str(tmp_path))
    module.test_live_local_llm_canonical_maryland_to_boston(tmp_path)
    report = json.loads((tmp_path / "canonical.json").read_text())
    assert report["policies"]["periodic-refresh-stale"]["answer_summary"]["outdated_fact_error_count"] == 3
    trace = [json.loads(line) for line in (tmp_path / "requests_responses.jsonl").read_text().splitlines()]
    assert json.loads(trace[0]["response"]["text"])["answer"] == "Boston"  # raw mistake preserved
    assert "What is the current location of Alice?" in trace[3]["request"]["user_prompt"]
    assert "Maryland" in trace[3]["request"]["user_prompt"]


def test_live_preflight_keeps_evidence_when_model_abstains_on_stated_location(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("failing_live_preflight", Path(__file__).with_name("test_llm_optional.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    response = json.dumps({"answer": "INSUFFICIENT_EVIDENCE", "abstained": True, "cited_sources": []})
    monkeypatch.setattr(module, "HuggingFace4BitClient", lambda cfg: FakeLLMClient(handler=lambda req: response, config=cfg))
    monkeypatch.setenv("LLM_TEST_OUTPUT_DIR", str(tmp_path))
    with pytest.raises(AssertionError, match="current location of Alice"):
        module.test_live_local_llm_canonical_maryland_to_boston(tmp_path)
    trace = [json.loads(line) for line in (tmp_path / "requests_responses.jsonl").read_text().splitlines()]
    assert json.loads(trace[-1]["response"]["text"])["abstained"]
    assert (tmp_path / "runtime.json").exists()


def test_event_time_truth_and_outdated_values_across_retraction_and_expiry():
    db = connect()
    try:
        ledger = FactLedger(db)
        ledger.append_initial_fact("office", "Alice", "office", "Room A", T0, "init")
        ledger.append_temporary_exception(
            "office", "Room Z", T0 + timedelta(days=1), T0 + timedelta(days=3), "temp"
        )
        ledger.append_initial_fact("desk", "Erin", "desk", "Desk 9", T0, "init_desk")
        ledger.append_retraction("desk", T0 + timedelta(days=1), "retract_desk")

        q_during = QueryEvent("q1", T0 + timedelta(days=2), 1, "q1", "Alice office", 1, ("office",))
        truth_during = resolve_event_time_truth(ledger, q_during)
        assert truth_during.expected_values == ("Room Z",)
        assert truth_during.outdated_values == ("Room A",)
        assert not truth_during.should_abstain

        q_after = QueryEvent("q2", T0 + timedelta(days=4), 2, "q2", "Alice office", 1, ("office",))
        truth_after = resolve_event_time_truth(ledger, q_after)
        assert truth_after.expected_values == ("Room A",)
        assert truth_after.outdated_values == ("Room Z",)
        assert not truth_after.should_abstain

        q_retracted = QueryEvent("q3", T0 + timedelta(days=2), 3, "q3", "Erin desk", 1, ("desk",))
        truth_retracted = resolve_event_time_truth(ledger, q_retracted)
        assert truth_retracted.expected_values == (None,)
        assert truth_retracted.outdated_values == ("Desk 9",)
        assert truth_retracted.should_abstain
    finally:
        db.close()


def test_fixed_reader_across_all_policies_and_stale_use_outdated_fact_errors():
    policies = (
        VerifyOnly(),
        EagerRefresh(),
        RepairOnFirstAccess(),
        PeriodicRefresh(refresh_interval_events=100),
        PeriodicRefreshStale(refresh_interval_events=100),
        AccumulatedThreshold(),
        PredictedThreshold(),
    )
    reader_prompts = set()
    for policy in policies:
        db, ledger, store, retriever, events = load_toy(DATA)
        try:
            client = FakeLLMClient(handler=_smart_toy_handler)
            result = Simulator(
                ledger,
                store,
                retriever,
                policy,
                RunConfig(),
                summary_builder=LLMSummaryBuilder(client),
                verifier=LLMVerifier(client),
                answer_reader=AnswerReader(client),
                llm_config=client.config,
                llm_runtime_info=client.runtime_info(),
            ).run(events)
            scores, summary = score_run_answers(ledger, events, result)
            reader_prompts.update(req.system_prompt for req in client.requests if req.role == "reader")
            assert len(scores) == 5
            assert summary.total_prompt_tokens > 0
            assert summary.total_completion_tokens > 0

            # q4 and q5 retrieve alice_digest (missing office) -> reader abstains cleanly
            q4_q5 = [s for s in scores if s.query_id in ("q4", "q5")]
            assert all(s.abstained and s.evidence_insufficient and not s.outdated_fact_error for s in q4_q5)

            q1_q3 = [s for s in scores if s.query_id in ("q1", "q2", "q3")]
            if policy.name == "periodic-refresh-stale":
                assert all(s.used_stale_source and s.outdated_fact_error and s.exact_match == 0.0 for s in q1_q3)
                assert summary.outdated_fact_error_count == 3
                assert summary.stale_source_use_count == 5
            else:
                assert all(not s.used_stale_source and not s.outdated_fact_error and s.exact_match == 1.0 for s in q1_q3)
                assert summary.outdated_fact_error_count == 0
        finally:
            db.close()

    # Exact same frozen reader system prompt used for every policy
    assert len(reader_prompts) == 1


def test_retraction_abstention_vs_stale_missed_abstention():
    db = connect()
    try:
        ledger, store = FactLedger(db), SummaryStore(db)
        ledger.append_initial_fact("erin_office", "Erin", "office", "Room E", T0, "init")
        store.create_summary(
            "erin_office",
            "Erin works in Room E.",
            "Erin works in {office}.",
            {"office": FactVersionRef("erin_office", 1)},
            T0,
        )
        events = [
            CorrectionEvent("erin_retract", T0 + timedelta(days=1), 1, "erin_office", ChangeType.RETRACTION, None),
            QueryEvent("q1", T0 + timedelta(days=2), 2, "q1", "Erin", 1, ("erin_office",)),
        ]

        def handler(req: LLMRequest) -> str:
            if req.role == "verify":
                return json.dumps({
                    "verified_evidence": "Erin's office information has been retracted.",
                    "cited_sources": ["erin_office@v2"],
                    "retracted_slots": ["office"],
                })
            if "retracted" in req.user_prompt.lower():
                return json.dumps({"answer": "INSUFFICIENT_EVIDENCE", "abstained": True, "cited_sources": ["erin_office@v2"]})
            return json.dumps({"answer": "Room E", "abstained": False, "cited_sources": ["erin_office@v1"]})

        # VerifyOnly abstains properly on retracted fact
        client_v = FakeLLMClient(handler=handler)
        res_v = Simulator(
            ledger, store, KeywordRetriever(db), VerifyOnly(), RunConfig(),
            verifier=LLMVerifier(client_v), answer_reader=AnswerReader(client_v),
        ).run(events)
        scores_v, summary_v = score_run_answers(ledger, events, res_v)
        assert scores_v[0].should_abstain and scores_v[0].abstained and scores_v[0].abstention_correct
        assert scores_v[0].exact_match == 1.0 and not scores_v[0].outdated_fact_error
        assert summary_v.abstention_accuracy == 1.0
    finally:
        db.close()

    # PeriodicRefreshStale answers stale Room E -> missed_abstention + outdated_fact_error
    db2 = connect()
    try:
        ledger2, store2 = FactLedger(db2), SummaryStore(db2)
        ledger2.append_initial_fact("erin_office", "Erin", "office", "Room E", T0, "init")
        store2.create_summary(
            "erin_office",
            "Erin works in Room E.",
            "Erin works in {office}.",
            {"office": FactVersionRef("erin_office", 1)},
            T0,
        )
        client_s = FakeLLMClient(handler=handler)
        res_s = Simulator(
            ledger2, store2, KeywordRetriever(db2), PeriodicRefreshStale(100), RunConfig(),
            answer_reader=AnswerReader(client_s),
        ).run(events)
        scores_s, summary_s = score_run_answers(ledger2, events, res_s)
        assert scores_s[0].should_abstain and not scores_s[0].abstained
        assert scores_s[0].missed_abstention and scores_s[0].outdated_fact_error
        assert summary_s.abstention_accuracy == 0.0
        assert summary_s.outdated_fact_error_count == 1
    finally:
        db2.close()


def test_render_reader_question_for_synthetic_v1_and_v2():
    w1 = generate_synthetic(SyntheticConfig(num_memories=3, seed=7, group_counts=(1, 1, 1),
                                            cold_queries=(1, 1), warm_queries=(3, 3), hot_queries=(11, 11)))
    db1, ledger1, _, _, events1 = load_synthetic(w1)
    try:
        q1 = next(e for e in events1 if isinstance(e, QueryEvent))
        # synthetic-v1 already ends with '?' -> preserved verbatim
        assert render_reader_question(q1, ledger1) == q1.query_text
    finally:
        db1.close()

    w2 = generate_evolving(EvolvingConfig(num_memories=3, seed=7, group_counts=(1, 1, 1),
                                          cold_queries=(1, 1), warm_queries=(3, 3), hot_queries=(11, 11)))
    db2, ledger2, _, _, events2 = load_evolving(w2)
    try:
        q2 = next(e for e in events2 if isinstance(e, QueryEvent))
        rendered = render_reader_question(q2, ledger2)
        assert rendered.startswith("What is the current ") and rendered.endswith("?")
        assert "Entity" in rendered
    finally:
        db2.close()


def test_canonical_script_fake_mode_and_deterministic_no_llm_imports():
    import importlib.util

    script_path = Path(__file__).resolve().parents[1] / "scripts/run_canonical_llm.py"
    spec = importlib.util.spec_from_file_location("run_canonical_llm", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    client = FakeLLMClient(handler=module._fake_canonical_handler)
    report = module.run_canonical(client, client.config)
    assert set(report["policies"]) == {
        "verify-only",
        "eager-refresh",
        "repair-on-first-access",
        "periodic-refresh-stale",
    }
    assert report["policies"]["verify-only"]["persisted_alice_profile"]["is_stale"] is True
    assert report["policies"]["eager-refresh"]["persisted_alice_profile"]["is_stale"] is False
    assert report["policies"]["periodic-refresh-stale"]["answer_summary"]["outdated_fact_error_count"] == 3

    # Deterministic mode must never import torch/transformers/bitsandbytes
    for heavy in ("torch", "transformers", "bitsandbytes"):
        assert heavy not in sys.modules
