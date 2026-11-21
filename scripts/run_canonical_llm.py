"""Run the canonical Maryland->Boston Step 5.6 scenario end to end under VERIFY and REPAIR.

Inputs: ``data/toy`` (first 4 events: ``alice_move`` Maryland->Boston followed by
queries ``q1``, ``q2``, ``q3``) and a local LLM client (``--mode llm`` using 4-bit
``Qwen/Qwen2.5-7B-Instruct``, or ``--mode fake`` for offline smoke verification).
Outputs: checks persisted summary text/provenance/staleness/FTS5 state and scored
answers across ``verify-only``, ``eager-refresh``, ``repair-on-first-access``,
and ``periodic-refresh-stale``; optionally writes a JSON report (never overwrites).

Usage:
    python scripts/run_canonical_llm.py --mode llm --output docs/step5/canonical_maryland_boston.json
    python scripts/run_canonical_llm.py --mode fake
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from verify_repair.answer_scoring import score_run_answers
from verify_repair.config import load_config
from verify_repair.llm.client import (
    DEFAULT_MODEL_ID,
    FakeLLMClient,
    HuggingFace4BitClient,
    LLMConfig,
    LLMRequest,
)
from verify_repair.llm.operations import LLMSummaryBuilder, LLMVerifier
from verify_repair.llm.reader import AnswerReader
from verify_repair.models import FactVersionRef
from verify_repair.policies.eager_refresh import EagerRefresh
from verify_repair.policies.periodic_refresh_stale import PeriodicRefreshStale
from verify_repair.policies.repair_on_first_access import RepairOnFirstAccess
from verify_repair.policies.verify_only import VerifyOnly
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.toy import load_toy


def _fake_canonical_handler(request: LLMRequest) -> str:
    if request.role == "verify":
        return json.dumps({
            "verified_evidence": "Alice is a researcher living in Boston.",
            "cited_sources": ["alice_name@v1", "alice_job@v1", "alice_location@v2"],
            "retracted_slots": [],
        })
    if request.role == "repair":
        if "Summary ID: alice_digest" in request.user_prompt:
            return json.dumps({
                "summary_text": "Alice profile digest: Boston.",
                "claims": [
                    {
                        "slot": "location",
                        "statement": "Alice lives in Boston.",
                        "source_ref": "alice_location@v2",
                    }
                ],
            })
        return json.dumps({
            "summary_text": "Alice is a researcher living in Boston.",
            "claims": [
                {"slot": "name", "statement": "Name is Alice.", "source_ref": "alice_name@v1"},
                {"slot": "job", "statement": "Alice is a researcher.", "source_ref": "alice_job@v1"},
                {"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"},
            ],
        })
    if request.role == "reader":
        if "Boston" in request.user_prompt:
            return json.dumps({
                "answer": "Boston",
                "abstained": False,
                "cited_sources": ["alice_location@v2"],
            })
        if "Maryland" in request.user_prompt:
            return json.dumps({
                "answer": "Maryland",
                "abstained": False,
                "cited_sources": ["alice_location@v1"],
            })
        return json.dumps({
            "answer": "INSUFFICIENT_EVIDENCE",
            "abstained": True,
            "cited_sources": [],
        })
    raise ValueError(f"unexpected role {request.role}")


def run_canonical(client, llm_config: LLMConfig) -> dict:
    """Execute the 4-event Maryland->Boston slice across VERIFY, REPAIR, and stale-use policies."""
    verifier = LLMVerifier(client)
    builder = LLMSummaryBuilder(client)
    reader = AnswerReader(client)
    config = load_config(ROOT / "configs/toy.yaml")
    runtime_info = client.runtime_info()

    policies = (
        VerifyOnly(),
        EagerRefresh(),
        RepairOnFirstAccess(),
        PeriodicRefreshStale(refresh_interval_events=100),
    )

    report_policies: dict[str, dict] = {}
    for policy in policies:
        db, ledger, store, retriever, events = load_toy(ROOT / "data/toy")
        try:
            slice_events = events[:4]  # alice_move + q1, q2, q3
            result = Simulator(
                ledger,
                store,
                retriever,
                policy,
                config,
                summary_builder=builder,
                verifier=verifier,
                answer_reader=reader,
                llm_config=llm_config,
                llm_runtime_info=runtime_info,
            ).run(slice_events)
            scores, summary = score_run_answers(ledger, slice_events, result)
            profile = store.get_summary("alice_profile")
            refs = store.get_source_facts("alice_profile")
            fts_maryland = [hit.summary_id for hit in retriever.search("Maryland", 10)]
            fts_boston = [hit.summary_id for hit in retriever.search("Boston", 10)]

            # Enforce Step 5.6 invariants loudly.
            if policy.name == "verify-only":
                assert result.verifications == 3 and result.repairs == 0
                assert profile.is_stale
                assert "Maryland" in profile.text
                assert refs["location"] == FactVersionRef("alice_location", 1)
                assert summary.exact_match_mean == 1.0
                assert summary.outdated_fact_error_count == 0
            elif policy.name in ("eager-refresh", "repair-on-first-access"):
                assert result.verifications == 0 and result.repairs >= 1
                assert not profile.is_stale
                assert "Boston" in profile.text and "Maryland" not in profile.text
                assert "Alice" in profile.text and "researcher" in profile.text
                assert refs["location"] == FactVersionRef("alice_location", 2)
                assert "alice_profile" in fts_boston and "alice_profile" not in fts_maryland
                assert summary.exact_match_mean == 1.0
                assert summary.outdated_fact_error_count == 0
            elif policy.name == "periodic-refresh-stale":
                assert result.verifications == 0 and result.repairs == 0
                assert profile.is_stale and "Maryland" in profile.text
                assert result.stale_source_uses == 3
                assert summary.outdated_fact_error_count == 3

            report_policies[policy.name] = {
                "verifications": result.verifications,
                "repairs": result.repairs,
                "stale_source_uses": result.stale_source_uses,
                "persisted_alice_profile": {
                    "text": profile.text,
                    "is_stale": profile.is_stale,
                    "source_refs": {k: f"{v.fact_id}@v{v.version}" for k, v in refs.items()},
                    "fts_matches_boston": "alice_profile" in fts_boston,
                    "fts_matches_maryland": "alice_profile" in fts_maryland,
                },
                "answer_summary": asdict(summary),
                "answers": [score.to_dict() for score in scores],
            }
        finally:
            db.close()

    return {
        "scenario": "canonical-maryland-to-boston",
        "llm_config": llm_config.snapshot(),
        "runtime_info": runtime_info,
        "policies": report_policies,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("llm", "fake"), default="llm")
    parser.add_argument("--llm-model", default=DEFAULT_MODEL_ID)
    parser.add_argument("--llm-revision", default=None)
    parser.add_argument("--llm-quantization", choices=("nf4", "none"), default="nf4")
    parser.add_argument("--llm-dtype", choices=("auto", "float16", "bfloat16"), default="auto")
    parser.add_argument("--llm-timeout", type=float, default=60.0)
    parser.add_argument("--llm-max-retries", type=int, default=1)
    parser.add_argument("--output", type=Path, default=None, help="new JSON report path (must not exist)")
    args = parser.parse_args(argv)

    if args.output is not None and args.output.exists():
        parser.error(f"--output {args.output} already exists")

    if args.mode == "fake":
        client = FakeLLMClient(handler=_fake_canonical_handler)
        llm_config = client.config
    else:
        llm_config = LLMConfig(
            backend="hf-4bit",
            model_id=args.llm_model,
            model_revision=args.llm_revision,
            quantization=args.llm_quantization,
            compute_dtype=args.llm_dtype,
            timeout_seconds=args.llm_timeout,
            max_retries=args.llm_max_retries,
        )
        client = HuggingFace4BitClient(llm_config)

    report = run_canonical(client, llm_config)
    if args.output is not None:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")

    for name, info in report["policies"].items():
        summary = info["answer_summary"]
        persisted = info["persisted_alice_profile"]
        print(
            f"policy={name} verifications={info['verifications']} repairs={info['repairs']} "
            f"is_stale={persisted['is_stale']} em={summary['exact_match_mean']:.2f} "
            f"f1={summary['token_f1_mean']:.2f} outdated_errors={summary['outdated_fact_error_count']} "
            f"text={persisted['text']!r}"
        )


if __name__ == "__main__":
    main()
