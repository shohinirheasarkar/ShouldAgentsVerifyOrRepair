"""Profile BGE-M3 + FAISS semantic-index maintenance separately from generation (Step 6.3).

The semantic adapter re-embeds every summary on each REPAIR (``index_summary``
calls a full ``rebuild``). This measures, for several index sizes N:

* ``full_rebuild_ms``: what one REPAIR costs today (embed all N texts + build);
* ``single_embed_ms``: embedding just the repaired text, the lower bound for an
  incremental update;
* ``search_ms``: one query embedding + search.

Summary texts come from the synthetic-v2 generator (seeded). Needs the
``semantic`` extra and cached BGE-M3 weights; run on a GPU node. Example:

    python scripts/profile_semantic_index.py --sizes 10 100 1000 --output semantic_profile.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from verify_repair.simulator.evolving import EvolvingConfig, generate_evolving, load_evolving  # noqa: E402


def timed(function, repeats):
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        function()
        samples.append((time.perf_counter() - started) * 1000.0)
    return {"median_ms": median(samples), "samples_ms": samples}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=int, nargs="+", default=[10, 100, 1000])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=2001)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"{args.output} already exists")

    from verify_repair.retrieval.semantic import BGEM3FaissRetriever

    workload = generate_evolving(EvolvingConfig(num_memories=max(args.sizes), seed=args.seed,
                                                facts_per_summary=(1, 6)))
    db, _, summaries, _, _ = load_evolving(workload)
    texts = {row["summary_id"]: row["text"] for row in db.execute("SELECT summary_id, text FROM summaries")}
    db.close()
    ordered = sorted(texts)

    started = time.perf_counter()
    retriever = BGEM3FaissRetriever()
    load_ms = (time.perf_counter() - started) * 1000.0
    retriever.rebuild({ordered[0]: texts[ordered[0]]})  # warm-up: first CUDA call
    import torch

    report = {"model_load_ms": load_ms, "cuda": torch.cuda.is_available(),
              "device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
              "sizes": {}}
    for size in args.sizes:
        subset = {summary_id: texts[summary_id] for summary_id in ordered[:size]}
        retriever.rebuild(subset)
        target = ordered[0]
        report["sizes"][str(size)] = {
            "full_rebuild_ms": timed(lambda: retriever.index_summary(target, subset[target] + " "), args.repeats),
            "single_embed_ms": timed(lambda: retriever._embed([subset[target]]), args.repeats),
            "search_ms": timed(lambda: retriever.search("lookup Entity000001", 1), args.repeats),
        }
        print(size, {k: round(v["median_ms"], 2) for k, v in report["sizes"][str(size)].items()}, flush=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
