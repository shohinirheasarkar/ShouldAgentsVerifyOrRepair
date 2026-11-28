"""Opt-in live 4-bit local LLM integration test; requires RUN_LLM=1 and cached weights."""
from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from verify_repair.llm.client import DEFAULT_MODEL_ID, HuggingFace4BitClient, LLMConfig
from verify_repair.llm.reader import AnswerReader, EvidencePacket


class _RecordingClient:
    """Persist every live request/response before any behavioral assertion runs."""

    def __init__(self, client, path):
        self.client, self.path, self.config = client, path, client.config

    def runtime_info(self):
        return self.client.runtime_info()

    def generate(self, request):
        row = {"request": asdict(request)}
        try:
            response = self.client.generate(request)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            raise
        else:
            row["response"] = asdict(response)
            return response
        finally:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, sort_keys=True) + "\n")


@pytest.mark.llm
@pytest.mark.skipif(
    os.getenv("RUN_LLM") != "1",
    reason="set RUN_LLM=1 on a GPU node with cached 4-bit Qwen2.5-7B-Instruct weights",
)
def test_live_local_llm_canonical_maryland_to_boston(tmp_path):
    script_path = Path(__file__).resolve().parents[1] / "scripts/run_canonical_llm.py"
    spec = importlib.util.spec_from_file_location("run_canonical_llm", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    cfg = LLMConfig(
        backend="hf-4bit",
        model_id=os.getenv("LLM_MODEL", DEFAULT_MODEL_ID),
        model_revision=os.getenv("LLM_REVISION") or None,
        quantization=os.getenv("LLM_QUANTIZATION", "nf4"),
        compute_dtype=os.getenv("LLM_DTYPE", "auto"),
        timeout_seconds=120.0,
        max_retries=1,
    )
    output_dir = Path(os.getenv("LLM_TEST_OUTPUT_DIR", str(tmp_path)))
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_client = HuggingFace4BitClient(cfg)
    client = _RecordingClient(raw_client, output_dir / "requests_responses.jsonl")
    (output_dir / "runtime.json").write_text(
        json.dumps({"config": cfg.snapshot(), "runtime": client.runtime_info()}, indent=2),
        encoding="utf-8",
    )

    # Reader-v6 must distinguish office from location, while still answering
    # the requested attribute from either current or stale supplied evidence.
    reader = AnswerReader(client)
    for question, evidence, ref, expected, abstained in (
        ("What is Alice's office?", "Alice is a researcher living in Boston.",
         "alice_location@v2", "INSUFFICIENT_EVIDENCE", True),
        ("What is the current office of Alice?", "Alice is a researcher living in Boston.",
         "alice_location@v2", "INSUFFICIENT_EVIDENCE", True),
        ("What is the current location of Alice?", "Alice is a researcher living in Boston.",
         "alice_location@v2", "Boston", False),
        ("What is the current location of Alice?", "Alice is a researcher living in Maryland.",
         "alice_location@v1", "Maryland", False),
    ):
        answer = reader.answer(EvidencePacket(
            query_id="reader_attribute_check", question_text=question,
            summary_ids=("alice_profile",), evidence_mode="summary",
            evidence_text=evidence,
            source_ref_labels=("alice_job@v1", ref, "alice_name@v1"),
            source_fact_keys=(("Alice", "job"), ("Alice", "location"), ("Alice", "name")),
        ))
        diagnostic = f"question={question!r} evidence={evidence!r} answer={answer}"
        assert answer.abstained is abstained, diagnostic
        assert answer.answer_text == expected, diagnostic

    report = module.run_canonical(client, cfg)
    (output_dir / "canonical.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert report["runtime_info"]["model_id"] == cfg.model_id
    assert report["policies"]["verify-only"]["answer_summary"]["exact_match_mean"] == 1.0
    assert report["policies"]["eager-refresh"]["answer_summary"]["exact_match_mean"] == 1.0
    assert report["policies"]["repair-on-first-access"]["answer_summary"]["exact_match_mean"] == 1.0
    assert report["policies"]["periodic-refresh-stale"]["answer_summary"]["outdated_fact_error_count"] == 3
