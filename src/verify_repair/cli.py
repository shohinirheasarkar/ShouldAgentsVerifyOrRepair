"""Run the deterministic toy trace; costs are nominal accounting units."""

import argparse
import json
from pathlib import Path

from verify_repair.config import load_config
from verify_repair.policies.eager_refresh import EagerRefresh
from verify_repair.policies.periodic_refresh import PeriodicRefresh
from verify_repair.policies.repair_on_first_access import (
    RepairOnFirstAccess,
)
from verify_repair.policies.verify_only import VerifyOnly
from verify_repair.retrieval.hybrid import HybridRetriever
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.toy import load_toy


def toy_root() -> Path:
    """Find the toy data and configuration in the repository."""
    candidates = [
        Path.cwd(),
        *Path.cwd().parents,
        Path(__file__).resolve().parents[2],
    ]

    for candidate in candidates:
        config = candidate / "configs/toy.yaml"
        events = candidate / "data/toy/events.yaml"

        if config.is_file() and events.is_file():
            return candidate

    raise FileNotFoundError(
        "run the toy demo from the repository checkout "
        "containing configs/ and data/"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "run-toy",
        help="replay toy facts, summaries, and events",
    )

    run.add_argument(
        "--policy",
        choices=[
            "verify-only",
            "eager-refresh",
            "repair-on-first-access",
            "periodic-refresh",
        ],
        required=True,
    )

    run.add_argument(
        "--refresh-interval-events",
        type=int,
        default=100,
        help="number of processed events between periodic repairs",
    )

    run.add_argument(
        "--retrieval",
        choices=["keyword", "hybrid"],
        default="keyword",
    )

    run.add_argument("--verbose", action="store_true")

    run.add_argument(
        "--jsonl",
        type=Path,
        help="write structured event records",
    )

    args = parser.parse_args()

    if args.command != "run-toy":
        parser.print_help()
        return

    root = toy_root()
    config = load_config(root / "configs/toy.yaml")

    db, ledger, summaries, retriever, events = load_toy(
        root / "data/toy"
    )

    if args.retrieval == "hybrid":
        from verify_repair.retrieval.semantic import (
            BGEM3FaissRetriever,
        )

        retriever = HybridRetriever(
            retriever,
            BGEM3FaissRetriever(),
        )

    policies = {
        "verify-only": VerifyOnly,
        "eager-refresh": EagerRefresh,
        "repair-on-first-access": RepairOnFirstAccess,
    }

    if args.policy == "periodic-refresh":
        policy = PeriodicRefresh(
            args.refresh_interval_events
        )
    else:
        policy = policies[args.policy]()

    result = Simulator(
        ledger,
        summaries,
        retriever,
        policy,
        config,
    ).run(events)

    if args.jsonl:
        args.jsonl.write_text(
            "".join(
                json.dumps(row, sort_keys=True) + "\n"
                for row in result.logs
            )
        )

    if args.verbose:
        for row in result.logs:
            print(json.dumps(row, sort_keys=True))

    print(
        f"policy={policy.name} "
        f"queries={result.queries} "
        f"corrections/expiries={result.corrections} "
        f"verifications={result.verifications} "
        f"repairs={result.repairs} "
        f"nominal_verify_cost="
        f"{result.cumulative_verify_cost:g} "
        f"nominal_repair_cost="
        f"{result.cumulative_repair_cost:g} "
        f"stale_source_uses={result.stale_source_uses}"
    )

    db.close()


if __name__ == "__main__":
    main()