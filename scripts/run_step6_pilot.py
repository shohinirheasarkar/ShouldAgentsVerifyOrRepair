"""Step 6 pilots: measured operation costs, answer quality, cost profiles, dirty bits.

Loads the model once, then:

1. **Toy pilot:** the toy trace under four policies with the fixed answer
   reader and component timing (``measure_latency``).
2. **1K pilot:** a 1,000-memory synthetic-v2 trace (pilot seed 2001; never used
   for experiments) under Verify-Only (VERIFY samples) and Repair-on-First-Access
   (REPAIR samples), with timing. *Cost runs* replay the first
   ``--cost-queries`` queries without the answer reader (many operations, fast);
   *quality runs* replay the first ``--quality-queries`` queries with the reader
   (answer quality and reader latency).
3. **Cost profiles** (``cost-profile-v1``) from all 1K pilot operations, one
   per metric: operation wall-clock ms, GPU ms, and tokens (LLM mode),
   stratified by summary fact count.
4. **Dirty bits:** structural flag precision on every stale access of the full
   1K trace, and the semantic check on ``--dirty-sample`` sampled accesses.

``--mode deterministic`` runs the same pipeline with template operations and no
reader (fast, offline; wall-ms profile and structural dirty bits only). Outputs
go to ``--output-dir`` (refuses to overwrite). Example (Zaratan GPU node):

    python scripts/run_step6_pilot.py --mode llm --output-dir docs/step6/pilot_<job>
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import traceback
from dataclasses import asdict, replace
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from verify_repair.answer_scoring import score_run_answers  # noqa: E402
from verify_repair.config import load_config  # noqa: E402
from verify_repair.costs import METRICS, build_profile, observations_from_logs  # noqa: E402
from verify_repair.dirty_bit import collect_stale_accesses, judge_sample, summarize  # noqa: E402
from verify_repair.evaluation import percentile  # noqa: E402
from verify_repair.experiments import make_policy  # noqa: E402
from verify_repair.llm.client import DEFAULT_MODEL_ID  # noqa: E402
from verify_repair.simulator.engine import Simulator  # noqa: E402
from verify_repair.simulator.events import QueryEvent  # noqa: E402
from verify_repair.simulator.evolving import (  # noqa: E402
    EvolvingConfig, evolving_run_config, generate_evolving, load_evolving,
)
from verify_repair.simulator.toy import load_toy  # noqa: E402

TOY_POLICIES = ("verify-only", "eager-refresh", "repair-on-first-access", "periodic-refresh-stale")
PILOT_POLICIES = ("verify-only", "repair-on-first-access")


def write_jsonl(path: Path, rows) -> None:
    with path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def write_json(path: Path, data) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")


def distribution(values: list[float]) -> dict | None:
    if not values:
        return None
    return {"n": len(values), "mean": mean(values), "p50": percentile(values, 50),
            "p95": percentile(values, 95), "p99": percentile(values, 99), "min": min(values), "max": max(values)}


def prefix_by_queries(events: list, max_queries: int | None) -> list:
    if max_queries is None:
        return events
    count = 0
    for index, event in enumerate(events):
        count += isinstance(event, QueryEvent)
        if count == max_queries:
            return events[:index + 1]
    return events


def operation_stats(rows: list[dict]) -> dict:
    """Per-operation distributions of every timing component and LLM usage field."""
    stats = {}
    for operation in ("VERIFY", "REPAIR"):
        chosen = [r for r in rows if r["action"] == operation]
        if not chosen:
            continue
        prefix = operation.lower()
        components = sorted({k for r in chosen for k in (r.get("timing_ms") or {}) if k.startswith(prefix)})
        entry = {"count": len(chosen),
                 "timing_ms": {k: distribution([r["timing_ms"][k] for r in chosen if k in r.get("timing_ms", {})])
                               for k in components}}
        for field in ("gpu_ms", "wall_ms", "prompt_tokens", "completion_tokens", "total_tokens"):
            values = [((r.get("maintenance_llm_metadata") or {}).get("usage") or {}).get(field) for r in chosen]
            values = [v for v in values if v is not None]
            if values:
                entry.setdefault("llm_usage", {})[field] = distribution(values)
        attempts = [(r.get("maintenance_llm_metadata") or {}).get("attempts") for r in chosen]
        attempts = [a for a in attempts if a is not None]
        if attempts:
            entry["retried_operations"] = sum(a > 1 for a in attempts)
        fallbacks = [r for r in chosen if (r.get("maintenance_llm_metadata") or {}).get("fallback")]
        entry["fallbacks"] = len(fallbacks)
        entry["fallback_examples"] = [
            {"event_id": r["event_id"], "summary_id": r["summary_id"],
             "error": r["maintenance_llm_metadata"]["error"]} for r in fallbacks[:5]]
        totals = [r["timing_ms"].get(f"{prefix}_total") for r in chosen if r.get("timing_ms")]
        totals = [t for t in totals if t is not None]
        if len(totals) > 1:
            entry["first_call_ms"] = totals[0]  # cold: first operation after model load
            entry["warm_median_ms"] = median(totals[1:])
        stats[operation.lower()] = entry
    queries = [r for r in rows if r["event_type"] == "query" and r.get("latency_ms_instrumentation_only") is not None]
    seen, latency, maintained, plain, reader = set(), [], [], [], []
    for r in queries:
        if r["query_id"] in seen:
            continue
        seen.add(r["query_id"])
        latency.append(r["latency_ms_instrumentation_only"])
        (maintained if r["action"] != "NO_OP" else plain).append(r["latency_ms_instrumentation_only"])
    reader = [r["timing_ms"]["reader"] for r in rows if "reader" in (r.get("timing_ms") or {})]
    stats["query_visible_latency_ms"] = {"all": distribution(latency), "with_maintenance": distribution(maintained),
                                         "without_maintenance": distribution(plain), "reader": distribution(reader)}
    reader_fallbacks = [r for r in rows if (r.get("reader_llm_metadata") or {}).get("fallback")]
    stats["reader_fallbacks"] = len(reader_fallbacks)
    stats["reader_fallback_examples"] = [
        {"query_id": r["query_id"], "error": r["reader_llm_metadata"]["error"]} for r in reader_fallbacks[:5]]
    background = [r["timing_ms"].get("total") for r in rows if r.get("query_visible") is False]
    stats["background_repair_ms"] = distribution([b for b in background if b is not None])
    return stats


def build_llm(args):
    from verify_repair.llm.client import HuggingFace4BitClient, LLMConfig
    from verify_repair.llm.operations import LLMSummaryBuilder, LLMVerifier
    from verify_repair.llm.reader import AnswerReader

    config = LLMConfig(backend="hf-4bit", model_id=args.llm_model, model_revision=args.llm_revision,
                       quantization=args.llm_quantization, compute_dtype=args.llm_dtype,
                       timeout_seconds=args.llm_timeout, max_retries=args.llm_max_retries)
    client = HuggingFace4BitClient(config)
    return {"llm_config": config, "llm_runtime_info": client.runtime_info(), "verifier": LLMVerifier(client),
            "summary_builder": LLMSummaryBuilder(client), "answer_reader": AnswerReader(client)}


def run_policy(stage, name, loader, run_config, llm, out, manifest, *, reader=True):
    """Replay one policy with timing; save logs and scored answers; record failures."""
    if not reader:
        llm = {key: value for key, value in llm.items() if key != "answer_reader"}
    db, ledger, summaries, retriever, events = loader()
    started = time.perf_counter()
    try:
        result = Simulator(ledger, summaries, retriever, make_policy(name), run_config,
                           measure_latency=True, llm_fallback=bool(llm), **llm).run(events)
        write_jsonl(out / f"{stage}_{name}_logs.jsonl", result.logs)
        entry = {"queries": result.queries, "verifications": result.verifications, "repairs": result.repairs,
                 "stale_source_uses": result.stale_source_uses,
                 "seconds": time.perf_counter() - started, "operations": operation_stats(result.logs)}
        if result.answers:
            scores, summary = score_run_answers(ledger, events, result)
            write_jsonl(out / f"{stage}_{name}_answers.jsonl", (s.to_dict() for s in scores))
            entry["answers"] = asdict(summary)
        manifest[stage][name] = entry
        print(f"{stage} policy={name} queries={result.queries} verifications={result.verifications} "
              f"repairs={result.repairs} seconds={entry['seconds']:.0f}", flush=True)
        return result.logs
    except Exception as exc:  # keep later stages running; the failure is part of the evidence
        manifest[stage][name] = {"failed": repr(exc), "traceback": traceback.format_exc(),
                                 "seconds": time.perf_counter() - started}
        print(f"{stage} policy={name} FAILED: {exc!r}", flush=True)
        return []
    finally:
        db.close()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("llm", "deterministic"), default="llm")
    parser.add_argument("--llm-model", default=DEFAULT_MODEL_ID)
    parser.add_argument("--llm-revision")
    parser.add_argument("--llm-quantization", choices=("nf4", "none"), default="nf4")
    parser.add_argument("--llm-dtype", choices=("auto", "float16", "bfloat16"), default="auto")
    parser.add_argument("--llm-timeout", type=float, default=60.0)
    parser.add_argument("--llm-max-retries", type=int, default=2)
    parser.add_argument("--pilot-memories", type=int, default=1000)
    parser.add_argument("--pilot-seed", type=int, default=2001)
    parser.add_argument("--cost-queries", type=int, default=2000,
                        help="queries of the 1K pilot trace replayed per policy without the reader")
    parser.add_argument("--quality-queries", type=int, default=600,
                        help="queries of the 1K pilot trace replayed per policy with the reader (0 skips)")
    parser.add_argument("--dirty-sample", type=int, default=150)
    parser.add_argument("--skip-toy", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        parser.error(f"{out} is not empty")
    stage_seconds = {}
    manifest = {"mode": args.mode, "settings": vars(args), "toy": {}, "pilot": {}, "stage_seconds": stage_seconds}

    started = time.perf_counter()
    llm = build_llm(args) if args.mode == "llm" else {}
    stage_seconds["model_load"] = time.perf_counter() - started
    if llm:
        manifest["llm_config"] = llm["llm_config"].snapshot()
        manifest["runtime_info"] = llm["llm_runtime_info"]

    if not args.skip_toy:
        started = time.perf_counter()
        toy_config = load_config(ROOT / "configs/toy.yaml")
        for name in TOY_POLICIES:
            run_policy("toy", name, lambda: load_toy(ROOT / "data/toy"), toy_config, llm, out, manifest)
        stage_seconds["toy"] = time.perf_counter() - started

    started = time.perf_counter()
    workload = generate_evolving(EvolvingConfig(
        num_memories=args.pilot_memories, seed=args.pilot_seed, facts_per_summary=(1, 6),
        changes_per_memory=3.0))
    manifest["pilot_workload"] = {"dataset_id": workload.config.dataset_id(),
                                  "sha256": workload.write(out / "pilot_workload.json"),
                                  "config": asdict(workload.config)}
    fact_counts = {spec["summary_id"]: len(spec["slots"]) for spec in workload.summaries}
    pilot_config = evolving_run_config(workload)

    def loader_for(max_queries):
        def load():
            db, ledger, summaries, retriever, events = load_evolving(workload)
            return db, ledger, summaries, retriever, prefix_by_queries(events, max_queries)
        return load

    manifest["pilot_quality"] = {}
    pilot_rows = []
    for name in PILOT_POLICIES:
        pilot_rows += run_policy("pilot", name, loader_for(args.cost_queries), pilot_config, llm, out, manifest,
                                 reader=False)
    stage_seconds["pilot_cost"] = time.perf_counter() - started
    started = time.perf_counter()
    if llm and args.quality_queries > 0:
        for name in PILOT_POLICIES:
            pilot_rows += run_policy("pilot_quality", name, loader_for(args.quality_queries), pilot_config, llm,
                                     out, manifest)
    stage_seconds["pilot_quality"] = time.perf_counter() - started

    profiles = {}
    for metric in (METRICS if llm else ("wall_ms",)):
        try:
            observations = observations_from_logs(pilot_rows, fact_counts, metric)
            profile = build_profile(
                f"{args.mode}-{metric}-pilot{args.pilot_seed}", metric, observations,
                {"description": METRICS[metric], "pilot_dataset_id": workload.config.dataset_id(),
                 "cost_queries": args.cost_queries, "quality_queries": args.quality_queries,
                 "policies": list(PILOT_POLICIES), "mode": args.mode,
                 "model": (llm["llm_config"].snapshot() if llm else "deterministic templates"),
                 "hardware": (llm["llm_runtime_info"] if llm else None)})
            digest = profile.write(out / f"cost_profile_{metric}.json")
            profiles[metric] = {"sha256": digest, "verify_samples": len(profile.samples["verify"]["all"]),
                                "repair_samples": len(profile.samples["repair"]["all"]),
                                "estimates": profile.estimates()}
        except ValueError as exc:
            profiles[metric] = {"failed": str(exc)}
    manifest["cost_profiles"] = profiles

    started = time.perf_counter()
    accesses = collect_stale_accesses(workload)
    judgments = []
    reader_failures = []
    if llm and args.dirty_sample > 0:
        try:
            judgments = judge_sample(accesses, llm["answer_reader"], args.dirty_sample, args.pilot_seed,
                                     failures=reader_failures)
        except Exception as exc:
            manifest["dirty_bit_failure"] = repr(exc)
    report = summarize(accesses, judgments)
    report["reader_failures"] = len(reader_failures)
    report["reader_failure_examples"] = reader_failures[:5]
    write_json(out / "dirty_bit_summary.json", report)
    if judgments:
        rows = [j.to_row() for j in judgments]
        with (out / "dirty_bit_sample.csv").open("x", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    manifest["dirty_bit"] = report
    stage_seconds["dirty_bit"] = time.perf_counter() - started

    write_json(out / "pilot_manifest.json", manifest)
    print(json.dumps({"stage_seconds": stage_seconds, "cost_profiles": profiles,
                      "dirty_bit": report}, indent=1, default=str))


if __name__ == "__main__":
    main()
