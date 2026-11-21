"""One-time login-node weight prefetch for offline Zaratan GPU batch jobs.

Run this on a Zaratan login or data-transfer node with ``HF_HOME`` pointing to
persistent scratch/project storage (e.g., ``export HF_HOME=/scratch/zt1/project/.../hf_cache``).
Downloads the tokenizer and model snapshot once and prints the exact HuggingFace
commit SHA to pin via ``--llm-revision`` during offline Slurm jobs.

Usage:
    HF_HOME=/scratch/zt1/project/cmsc848n/user/$USER/hf_cache \
      python scripts/prefetch_llm_model.py --model Qwen/Qwen2.5-7B-Instruct
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    parser.add_argument("--revision", default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    args = parser.parse_args(argv)

    from huggingface_hub import HfApi, snapshot_download

    api = HfApi()
    info = api.model_info(args.model, revision=args.revision)
    commit_sha = info.sha
    local_dir = snapshot_download(
        repo_id=args.model,
        revision=commit_sha,
    )
    record = {
        "model_id": args.model,
        "model_revision": commit_sha,
        "local_snapshot_dir": local_dir,
        "hf_home": os.getenv("HF_HOME"),
    }
    if args.output_json is not None:
        args.output_json.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
