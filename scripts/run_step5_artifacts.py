"""Generate all Step 5 live-LLM toy and canonical artifacts in a single model load.

Inputs: ``data/toy`` and a local 4-bit LLM (``--mode llm``) or offline fake
(``--mode fake``). Reuses one loaded model across the canonical Maryland->Boston
check and the four full toy policy runs so HPC jobs avoid paying repeated
network-filesystem PyTorch import and weight-load overhead.
Outputs: ``runtime_manifest.json``, ``canonical_maryland_boston.json``, and
``toy_<policy>_{logs,answers}.jsonl`` inside ``--output-dir`` (refuses to
overwrite existing files).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from verify_repair.answer_scoring import score_run_answers
from verify_repair.config import load_config
from verify_repair.experiments import make_policy
from verify_repair.llm.client import (
    DEFAULT_MODEL_ID,
    FakeLLMClient,
    HuggingFace4BitClient,
    LLMConfig,
)
from verify_repair.llm.operations import LLMSummaryBuilder, LLMVerifier
from verify_repair.llm.reader import AnswerReader
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.toy import load_toy

TOY_POLICIES = (
    "verify-only",
    "eager-refresh",
    "repair-on-first-access",
    "periodic-refresh-stale",
)


def _load_canonical_module():
    path = ROOT / "scripts/run_canonical_llm.py"
    spec = importlib.util.spec_from_file_location("run_canonical_llm", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _fake_toy_handler(request):
    if request.role == "verify":
        if "Summary ID: alice_digest" in request.user_prompt:
            return json.dumps({
                "verified_evidence": "Alice profile digest: Boston.",
                "cited_sources": ["alice_location@v2"],
                "retracted_slots": [],
            })
        return json.dumps({
            "verified_evidence": "Alice is a researcher living in Boston.",
            "cited_sources": ["alice_name@v1", "alice_job@v1", "alice_location@v2"],
            "retracted_slots": [],
        })
    if request.role == "repair":
        if "Summary ID: alice_digest" in request.user_prompt:
            return json.dumps({
                "summary_text": "Alice profile digest: Boston.",
                "claims": [{"slot": "location", "statement": "Alice lives in Boston.", "source_ref": "alice_location@v2"}],
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
                "claims": [{"slot": "office", "statement": "Erin's office is retracted.", "source_ref": "erin_office@v2"}],
            })
        if "Summary ID: alice_office" in request.user_prompt:
            if "value=Room Z" in request.user_prompt:
                return json.dumps({
                    "summary_text": "Alice works in Room Z.",
                    "claims": [{"slot": "office", "statement": "Alice works in Room Z.", "source_ref": "alice_office@v2"}],
                })
            return json.dumps({
                "summary_text": "Alice works in Room A.",
                "claims": [{"slot": "office", "statement": "Alice works in Room A.", "source_ref": "alice_office@v1"}],
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
        if "office of Alice" in request.user_prompt:
            return json.dumps({"answer": "INSUFFICIENT_EVIDENCE", "abstained": True, "cited_sources": []})
        if "Boston" in request.user_prompt:
            return json.dumps({"answer": "Boston", "abstained": False, "cited_sources": ["alice_location@v2"]})
        if "Maryland" in request.user_prompt:
            return json.dumps({"answer": "Maryland", "abstained": False, "cited_sources": ["alice_location@v1"]})
        return json.dumps({"answer": "INSUFFICIENT_EVIDENCE", "abstained": True, "cited_sources": []})
    raise ValueError(f"unexpected role {request.role}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("llm", "fake"), default="llm")
    parser.add_argument("--llm-model", default=DEFAULT_MODEL_ID)
    parser.add_argument("--llm-revision", default=None)
    parser.add_argument("--llm-quantization", choices=("nf4", "none"), default="nf4")
    parser.add_argument("--llm-dtype", choices=("auto", "float16", "bfloat16"), default="auto")
    parser.add_argument("--llm-timeout", type=float, default=60.0)
    parser.add_argument("--llm-max-retries", type=int, default=2)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    expected_files = [
        args.output_dir / "canonical_maryland_boston.json",
        args.output_dir / "runtime_manifest.json",
        *[args.output_dir / f"toy_{p}_logs.jsonl" for p in TOY_POLICIES],
        *[args.output_dir / f"toy_{p}_answers.jsonl" for p in TOY_POLICIES],
    ]
    for path in expected_files:
        if path.exists():
            parser.error(f"output file {path} already exists")

    canonical_mod = _load_canonical_module()
    if args.mode == "fake":
        client = FakeLLMClient(handler=_fake_toy_handler)
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

    runtime_info = client.runtime_info()

    print("=== Step 5.6 Canonical Maryland->Boston ===")
    canonical_report = canonical_mod.run_canonical(client, llm_config)
    with (args.output_dir / "canonical_maryland_boston.json").open("x", encoding="utf-8") as f:
        f.write(json.dumps(canonical_report, indent=2, sort_keys=True) + "\n")
    for name, info in canonical_report["policies"].items():
        s = info["answer_summary"]
        p = info["persisted_alice_profile"]
        print(
            f"canonical policy={name} verifications={info['verifications']} repairs={info['repairs']} "
            f"is_stale={p['is_stale']} em={s['exact_match_mean']:.2f} "
            f"outdated_errors={s['outdated_fact_error_count']} text={p['text']!r}"
        )

    print("=== Full Toy Trace Across Policies ===")
    verifier = LLMVerifier(client)
    builder = LLMSummaryBuilder(client)
    reader = AnswerReader(client)
    run_cfg = load_config(ROOT / "configs/toy.yaml")
    policy_summaries: dict[str, dict] = {}

    for policy_name in TOY_POLICIES:
        policy = make_policy(policy_name)
        db, ledger, store, retriever, events = load_toy(ROOT / "data/toy")
        try:
            result = Simulator(
                ledger,
                store,
                retriever,
                policy,
                run_cfg,
                summary_builder=builder,
                verifier=verifier,
                answer_reader=reader,
                llm_config=llm_config,
                llm_runtime_info=runtime_info,
            ).run(events)
            scores, summary = score_run_answers(ledger, events, result)
            with (args.output_dir / f"toy_{policy_name}_logs.jsonl").open("x", encoding="utf-8") as f:
                for row in result.logs:
                    f.write(json.dumps(row, sort_keys=True) + "\n")
            with (args.output_dir / f"toy_{policy_name}_answers.jsonl").open("x", encoding="utf-8") as f:
                for score in scores:
                    f.write(json.dumps(score.to_dict(), sort_keys=True) + "\n")
            policy_summaries[policy_name] = asdict(summary)
            em = f"{summary.exact_match_mean:.3f}" if summary.exact_match_mean is not None else "n/a"
            f1 = f"{summary.token_f1_mean:.3f}" if summary.token_f1_mean is not None else "n/a"
            print(
                f"toy policy={policy_name} queries={result.queries} verifications={result.verifications} "
                f"repairs={result.repairs} em={em} f1={f1} abstentions={summary.abstention_count} "
                f"outdated_errors={summary.outdated_fact_error_count} "
                f"prompt_tokens={summary.total_prompt_tokens} completion_tokens={summary.total_completion_tokens}"
            )
        finally:
            db.close()

    manifest = {
        "dataset_id": run_cfg.dataset_id,
        "seed": run_cfg.seed,
        "llm_config": llm_config.snapshot(),
        "runtime_info": runtime_info,
        "toy_policy_summaries": policy_summaries,
    }
    with (args.output_dir / "runtime_manifest.json").open("x", encoding="utf-8") as f:
        f.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
