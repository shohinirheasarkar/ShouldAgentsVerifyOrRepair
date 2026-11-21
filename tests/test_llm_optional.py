"""Opt-in live 4-bit local LLM integration test; requires RUN_LLM=1 and cached weights."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from verify_repair.llm.client import DEFAULT_MODEL_ID, HuggingFace4BitClient, LLMConfig


@pytest.mark.llm
@pytest.mark.skipif(
    os.getenv("RUN_LLM") != "1",
    reason="set RUN_LLM=1 on a GPU node with cached 4-bit Qwen2.5-7B-Instruct weights",
)
def test_live_local_llm_canonical_maryland_to_boston():
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
    client = HuggingFace4BitClient(cfg)
    report = module.run_canonical(client, cfg)
    assert report["runtime_info"]["model_id"] == cfg.model_id
    assert report["policies"]["verify-only"]["answer_summary"]["exact_match_mean"] == 1.0
    assert report["policies"]["eager-refresh"]["answer_summary"]["exact_match_mean"] == 1.0
    assert report["policies"]["repair-on-first-access"]["answer_summary"]["exact_match_mean"] == 1.0
    assert report["policies"]["periodic-refresh-stale"]["answer_summary"]["outdated_fact_error_count"] == 3
