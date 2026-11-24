"""Pure offline unit tests for LLMConfig, prompt versioning, and JSON/claim validators."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from verify_repair.llm.client import LLMConfig
from verify_repair.llm.prompts import (
    PROMPT_VERSION,
    build_repair_prompt,
    build_verify_prompt,
    prompt_bundle_sha256,
)
from verify_repair.llm.validation import (
    extract_json_object,
    validate_reader_output,
    validate_repair_output,
    validate_verify_output,
)
from verify_repair.maintenance.summary_builder import RepairBuildRequest
from verify_repair.models import (
    ChangeType,
    FactVersion,
    FactVersionRef,
    LLMValidationError,
    LLMVerificationError,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)


def _fact(fact_id, version, entity, attribute, value, change=ChangeType.INITIAL):
    return FactVersion(
        fact_id=fact_id,
        version=version,
        entity=entity,
        attribute=attribute,
        value=value,
        change_type=change,
        valid_from=T0 if version == 1 else T1,
        valid_to=None,
        supersedes=FactVersionRef(fact_id, version - 1) if version > 1 else None,
        event_id=f"ev_{fact_id}_{version}",
        event_sequence=version,
    )


def _alice_request() -> RepairBuildRequest:
    old = {
        "name": _fact("alice_name", 1, "Alice", "name", "Alice"),
        "job": _fact("alice_job", 1, "Alice", "job", "researcher"),
        "location": _fact("alice_location", 1, "Alice", "location", "Maryland"),
    }
    current = {
        "name": old["name"],
        "job": old["job"],
        "location": _fact("alice_location", 2, "Alice", "location", "Boston", ChangeType.CORRECTION),
    }
    return RepairBuildRequest(
        summary_id="alice_profile",
        old_text="Alice is a researcher living in Maryland.",
        text_template="{name} is a {job} living in {location}.",
        old_facts=old,
        current_facts=current,
        changed_slots=("location",),
    )


def test_llm_config_validation_and_prompt_digest():
    cfg = LLMConfig()
    snap = cfg.snapshot()
    assert snap["prompt_version"] == PROMPT_VERSION
    assert len(snap["prompt_bundle_sha256"]) == 64
    assert snap["prompt_bundle_sha256"] == prompt_bundle_sha256()

    with pytest.raises(ValueError, match="temperature"):
        LLMConfig(do_sample=False, temperature=0.7)
    with pytest.raises(ValueError, match="timeout_seconds"):
        LLMConfig(timeout_seconds=0)
    with pytest.raises(ValueError, match="timeout_seconds"):
        LLMConfig(timeout_seconds=float("inf"))
    with pytest.raises(ValueError, match="max_retries"):
        LLMConfig(max_retries=-1)
    with pytest.raises(ValueError, match="quantization"):
        LLMConfig(quantization="int8")


def test_extract_json_object_strips_fences_and_rejects_malformed():
    fenced = '```json\n{"answer": "Boston", "abstained": false, "cited_sources": []}\n```'
    assert extract_json_object(fenced)["answer"] == "Boston"
    with pytest.raises(LLMValidationError, match="empty"):
        extract_json_object("   ")
    with pytest.raises(LLMValidationError, match="invalid JSON"):
        extract_json_object("not json at all")
    with pytest.raises(LLMValidationError, match="expected JSON object"):
        extract_json_object('["list", "not", "dict"]')


def test_validate_verify_output_accepts_current_and_rejects_stale_or_fabricated():
    req = _alice_request()
    good = json.dumps({
        "verified_evidence": "Alice is a researcher currently living in Boston.",
        "cited_sources": ["alice_location@v2", "alice_job@v1"],
        "retracted_slots": [],
    })
    out = validate_verify_output(good, req.old_facts, req.current_facts, req.changed_slots)
    assert "Boston" in out.verified_evidence
    assert out.cited_sources == ("alice_location@v2", "alice_job@v1")

    # Stale citation (v1 instead of v2)
    stale_cite = json.dumps({
        "verified_evidence": "Alice lives in Boston.",
        "cited_sources": ["alice_location@v1"],
        "retracted_slots": [],
    })
    with pytest.raises(LLMVerificationError, match="unknown or superseded"):
        validate_verify_output(stale_cite, req.old_facts, req.current_facts, req.changed_slots)

    # Leaked superseded value
    leaked = json.dumps({
        "verified_evidence": "Alice moved from Maryland to Boston.",
        "cited_sources": ["alice_location@v2"],
        "retracted_slots": [],
    })
    with pytest.raises(LLMVerificationError, match="leaked superseded value"):
        validate_verify_output(leaked, req.old_facts, req.current_facts, req.changed_slots)

    # Missing current value
    missing = json.dumps({
        "verified_evidence": "Alice is a researcher.",
        "cited_sources": ["alice_location@v2"],
        "retracted_slots": [],
    })
    with pytest.raises(LLMVerificationError, match="missing current value"):
        validate_verify_output(missing, req.old_facts, req.current_facts, req.changed_slots)


def test_validate_repair_output_accepts_natural_prose_and_checks_invariants():
    req = _alice_request()
    good = json.dumps({
        "summary_text": "Alice works as a researcher and currently lives in Boston.",
        "claims": [
            {"slot": "name", "statement": "The person's name is Alice.", "source_ref": "alice_name@v1"},
            {"slot": "job", "statement": "Alice is a researcher.", "source_ref": "alice_job@v1"},
            {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"},
        ],
    })
    out = validate_repair_output(good, req)
    assert out.fidelity["unchanged_preserved_ratio"] == 1.0
    assert out.fidelity["changed_updated_ratio"] == 1.0
    assert out.fidelity["citation_exact_match"] == 1.0
    assert [c.slot for c in out.claims] == ["job", "location", "name"]


def test_validate_repair_rejects_unsupported_claim_dropped_content_and_stale_leak():
    req = _alice_request()

    # Unsupported extra claim slot (e.g., from hallucination or prompt injection)
    extra_claim = json.dumps({
        "summary_text": "Alice is a researcher living in Boston and owns a spaceship.",
        "claims": [
            {"slot": "name", "statement": "Alice.", "source_ref": "alice_name@v1"},
            {"slot": "job", "statement": "Alice is a researcher.", "source_ref": "alice_job@v1"},
            {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"},
            {"slot": "hobby", "statement": "Alice owns a spaceship.", "source_ref": "fabricated@v1"},
        ],
    })
    with pytest.raises(LLMValidationError, match="unsupported or unknown claim slot"):
        validate_repair_output(extra_claim, req)

    # Dropped unrelated unchanged fact ("researcher")
    dropped_job = json.dumps({
        "summary_text": "Alice lives in Boston.",
        "claims": [
            {"slot": "name", "statement": "Alice.", "source_ref": "alice_name@v1"},
            {"slot": "job", "statement": "Alice is a researcher.", "source_ref": "alice_job@v1"},
            {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"},
        ],
    })
    with pytest.raises(LLMValidationError, match="missing unchanged value 'researcher'"):
        validate_repair_output(dropped_job, req)

    # Stale provenance version in claim
    wrong_ref = json.dumps({
        "summary_text": "Alice is a researcher living in Boston.",
        "claims": [
            {"slot": "name", "statement": "Alice.", "source_ref": "alice_name@v1"},
            {"slot": "job", "statement": "Alice is a researcher.", "source_ref": "alice_job@v1"},
            {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v1"},
        ],
    })
    with pytest.raises(LLMValidationError, match="expected exact current ref 'alice_location@v2'"):
        validate_repair_output(wrong_ref, req)

    # Retained superseded value "Maryland"
    leaked_old = json.dumps({
        "summary_text": "Alice is a researcher who moved from Maryland to Boston.",
        "claims": [
            {"slot": "name", "statement": "Alice.", "source_ref": "alice_name@v1"},
            {"slot": "job", "statement": "Alice is a researcher.", "source_ref": "alice_job@v1"},
            {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"},
        ],
    })
    with pytest.raises(LLMValidationError, match="retained superseded value 'Maryland'"):
        validate_repair_output(leaked_old, req)


def test_validate_repair_allows_shared_value_still_active_in_another_slot():
    # In synthetic-v2, slot1 may change amber->birch while slot2 still holds amber.
    old = {
        "attr01": _fact("m1_a1", 1, "Entity000001", "attr01", "amber"),
        "attr02": _fact("m1_a2", 1, "Entity000001", "attr02", "amber"),
    }
    current = {
        "attr01": _fact("m1_a1", 2, "Entity000001", "attr01", "birch", ChangeType.CORRECTION),
        "attr02": old["attr02"],
    }
    req = RepairBuildRequest(
        summary_id="memory_000001",
        old_text="Entity000001 record: attr01 amber; attr02 amber.",
        text_template="Entity000001 record: attr01 {attr01}; attr02 {attr02}.",
        old_facts=old,
        current_facts=current,
        changed_slots=("attr01",),
    )
    valid = json.dumps({
        "summary_text": "Entity000001 record: attr01 birch; attr02 amber.",
        "claims": [
            {"slot": "attr01", "statement": "attr01 is birch", "source_ref": "m1_a1@v2"},
            {"slot": "attr02", "statement": "attr02 is amber", "source_ref": "m1_a2@v1"},
        ],
    })
    out = validate_repair_output(valid, req)
    assert "birch" in out.summary_text and "amber" in out.summary_text


def test_validate_repair_handles_retraction():
    old = {"office": _fact("erin_office", 1, "Erin", "office", "Room E")}
    current = {"office": _fact("erin_office", 2, "Erin", "office", None, ChangeType.RETRACTION)}
    req = RepairBuildRequest(
        summary_id="erin_office",
        old_text="Erin works in Room E.",
        text_template="Erin works in {office}.",
        old_facts=old,
        current_facts=current,
        changed_slots=("office",),
    )
    good = json.dumps({
        "summary_text": "Erin's office information has been retracted.",
        "claims": [
            {"slot": "office", "statement": "Erin's office is retracted.", "source_ref": "erin_office@v2"}
        ],
    })
    out = validate_repair_output(good, req)
    assert "retracted" in out.summary_text.lower()

    # Retracted value retained -> rejected
    bad = json.dumps({
        "summary_text": "Erin's office Room E was retracted.",
        "claims": [
            {"slot": "office", "statement": "Office retracted.", "source_ref": "erin_office@v2"}
        ],
    })
    with pytest.raises(LLMValidationError, match="retained superseded value 'Room E'"):
        validate_repair_output(bad, req)


def test_validate_reader_output_and_prompts_include_injection_guard():
    good = json.dumps({"answer": "Boston", "abstained": False, "cited_sources": ["alice_location@v2"]})
    out = validate_reader_output(good, {"alice_location@v2"})
    assert out.answer_text == "Boston" and not out.abstained

    # Abstention normalization
    implicit_abstain = json.dumps({"answer": "INSUFFICIENT_EVIDENCE", "abstained": False, "cited_sources": []})
    assert validate_reader_output(implicit_abstain, set()).abstained is True

    # Fabricated citation
    bad_cite = json.dumps({"answer": "Boston", "abstained": False, "cited_sources": ["fake@v9"]})
    with pytest.raises(LLMValidationError, match="unknown source_refs"):
        validate_reader_output(bad_cite, {"alice_location@v2"})

    req = _alice_request()
    sys_v, user_v = build_verify_prompt("alice_profile", "Where?", req.old_text, req.old_facts, req.current_facts, ("location",))
    sys_r, user_r = build_repair_prompt(req)
    assert "untrusted data" in sys_v.lower() and "untrusted data" in sys_r.lower()
    assert "<authoritative_facts>" in user_v and "<authoritative_facts>" in user_r


def test_offline_snapshot_resolution_without_refs_main(tmp_path):
    from verify_repair.llm.client import _resolve_offline_source

    sha = "a09a35458c702b33eeacc393d103063234e8bc28"
    snap = tmp_path / "models--Qwen--Qwen2.5-7B-Instruct" / "snapshots" / sha
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")

    # Offline with revision=None and no refs/main -> resolves single cached snapshot dir + SHA
    resolved_path, resolved_rev = _resolve_offline_source(
        "Qwen/Qwen2.5-7B-Instruct", None, local_only=True, hub_cache_dir=tmp_path
    )
    assert resolved_path == str(snap)
    assert resolved_rev == sha

    # Offline with explicit pinned SHA -> resolves directly to snapshot dir
    resolved_path2, resolved_rev2 = _resolve_offline_source(
        "Qwen/Qwen2.5-7B-Instruct", sha, local_only=True, hub_cache_dir=tmp_path
    )
    assert resolved_path2 == str(snap)
    assert resolved_rev2 == sha

    # Online mode leaves repo ID untouched
    resolved_online, rev_online = _resolve_offline_source(
        "Qwen/Qwen2.5-7B-Instruct", None, local_only=False, hub_cache_dir=tmp_path
    )
    assert resolved_online == "Qwen/Qwen2.5-7B-Instruct"
    assert rev_online is None
<<<<<<< Updated upstream
=======


def test_validate_repair_accepts_exact_qwen_output_without_tautological_name_claim():
    # Exact raw output observed from live 4-bit Qwen/Qwen2.5-7B-Instruct on Zaratan H100.
    req = _alice_request()
    qwen_raw = json.dumps({
        "summary_text": "Alice is a researcher living in Boston.",
        "claims": [
            {"slot": "job", "statement": "Alice is a researcher.", "source_ref": "alice_job@v1"},
            {"slot": "location", "statement": "Alice is living in Boston.", "source_ref": "alice_location@v2"},
        ],
    })
    out = validate_repair_output(qwen_raw, req)
    assert out.summary_text == "Alice is a researcher living in Boston."
    assert [c.slot for c in out.claims] == ["job", "location"]
    assert out.fidelity["unchanged_preserved_ratio"] == 1.0
    assert out.fidelity["changed_updated_ratio"] == 1.0

    # Still rejects if Alice (identity slot) is dropped from summary_text
    dropped_alice = json.dumps({
        "summary_text": "A researcher living in Boston.",
        "claims": [
            {"slot": "job", "statement": "She is a researcher.", "source_ref": "alice_job@v1"},
            {"slot": "location", "statement": "She lives in Boston.", "source_ref": "alice_location@v2"},
        ],
    })
    with pytest.raises(LLMValidationError, match="dropped required entity 'Alice'"):
        validate_repair_output(dropped_alice, req)

    # Still rejects if a non-identity unchanged slot (job) is omitted from claims
    missing_job = json.dumps({
        "summary_text": "Alice is a researcher living in Boston.",
        "claims": [
            {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"},
        ],
    })
    with pytest.raises(LLMValidationError, match="missing required slots: \\['job'\\]"):
        validate_repair_output(missing_job, req)

    # Still rejects duplicated @v1@v1 suffix
    dup_suffix = json.dumps({
        "summary_text": "Alice is a researcher living in Boston.",
        "claims": [
            {"slot": "name", "statement": "Alice", "source_ref": "alice_name@v1@v1"},
            {"slot": "job", "statement": "Alice is a researcher.", "source_ref": "alice_job@v1"},
            {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"},
        ],
    })
    with pytest.raises(LLMValidationError, match="expected exact current ref 'alice_name@v1'"):
        validate_repair_output(dup_suffix, req)

    # Still rejects if name itself changed and is omitted from claims
    changed_name_req = RepairBuildRequest(
        summary_id="alice_profile",
        old_text="Alice is a researcher living in Maryland.",
        text_template="{name} is a {job} living in {location}.",
        old_facts=req.old_facts,
        current_facts={
            **req.old_facts,
            "name": _fact("alice_name", 2, "Alice", "name", "Alicia", ChangeType.CORRECTION),
        },
        changed_slots=("name",),
    )
    missing_changed_name = json.dumps({
        "summary_text": "Alicia is a researcher living in Maryland.",
        "claims": [
            {"slot": "job", "statement": "Alicia is a researcher.", "source_ref": "alice_job@v1"},
            {"slot": "location", "statement": "Alicia lives in Maryland.", "source_ref": "alice_location@v1"},
        ],
    })
    with pytest.raises(LLMValidationError, match="missing required slots: \\['name'\\]"):
        validate_repair_output(missing_changed_name, changed_name_req)


def test_validate_reader_accepts_null_cited_sources_on_abstention():
    raw = json.dumps({"answer": "INSUFFICIENT_EVIDENCE", "abstained": True, "cited_sources": None})
    out = validate_reader_output(raw, {"alice_location@v2"})
    assert out.abstained is True
    assert out.cited_sources == ()


def test_validate_repair_retraction_phrasings_and_expiry_rollback():
    old_ret = {"office": _fact("erin_office", 1, "Erin", "office", "Room E")}
    cur_ret = {"office": _fact("erin_office", 2, "Erin", "office", None, ChangeType.RETRACTION)}
    ret_req = RepairBuildRequest(
        summary_id="erin_office",
        old_text="Erin works in Room E.",
        text_template="Erin works in {office}.",
        old_facts=old_ret,
        current_facts=cur_ret,
        changed_slots=("office",),
    )
    for text, stmt in (
        ("Erin does not have an assigned office.", "Erin does not have an office."),
        ("Erin has no office.", "Erin has no office."),
        ("Erin's office is unassigned.", "Office is unassigned."),
    ):
        raw = json.dumps({
            "summary_text": text,
            "claims": [{"slot": "office", "statement": stmt, "source_ref": "erin_office@v2"}],
        })
        out = validate_repair_output(raw, ret_req)
        assert "Erin" in out.summary_text

    # Temporary exception expiry: v2 (Room Z) reverts back to v1 (Room A).
    old_exp = {"office": _fact("alice_office", 2, "Alice", "office", "Room Z", ChangeType.TEMPORARY_EXCEPTION)}
    cur_exp = {"office": _fact("alice_office", 1, "Alice", "office", "Room A")}
    exp_req = RepairBuildRequest(
        summary_id="alice_office",
        old_text="Alice works in Room Z.",
        text_template="Alice works in {office}.",
        old_facts=old_exp,
        current_facts=cur_exp,
        changed_slots=("office",),
    )
    reverted = json.dumps({
        "summary_text": "Alice works in Room A.",
        "claims": [{"slot": "office", "statement": "Alice works in Room A.", "source_ref": "alice_office@v1"}],
    })
    out_exp = validate_repair_output(reverted, exp_req)
    assert out_exp.summary_text == "Alice works in Room A."
    assert out_exp.claims[0].source_ref == "alice_office@v1"


def test_reader_system_prompt_preserves_verified_stale_answering_contract():
    from verify_repair.llm.prompts import READER_SYSTEM_PROMPT

    assert "only describes a different attribute" not in READER_SYSTEM_PROMPT
    assert "<required_claim_slots>" not in READER_SYSTEM_PROMPT
    assert "using ONLY the facts explicitly stated in <evidence>" in READER_SYSTEM_PROMPT
>>>>>>> Stashed changes
