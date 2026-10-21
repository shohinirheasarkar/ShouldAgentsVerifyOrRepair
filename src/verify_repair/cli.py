"""Generate or replay deterministic workloads; costs are nominal accounting units."""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from verify_repair.config import load_config
from verify_repair.policies.accumulated_threshold import AccumulatedThreshold
from verify_repair.policies.eager_refresh import EagerRefresh
from verify_repair.policies.oldest_first import OldestFirst
from verify_repair.policies.periodic_refresh import PeriodicRefresh
from verify_repair.policies.repair_on_first_access import (
    RepairOnFirstAccess,
)
from verify_repair.policies.verify_only import VerifyOnly
from verify_repair.retrieval.hybrid import HybridRetriever
from verify_repair.simulator.engine import Simulator
from verify_repair.simulator.synthetic import (
    GROUP_BOUNDS,
    SyntheticConfig,
    generate_synthetic,
    load_synthetic,
    read_synthetic,
    synthetic_run_config,
)
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


def _add_run_arguments(
    run: argparse.ArgumentParser,
    retrieval_choices: tuple[str, ...] = ("keyword", "hybrid"),
) -> None:
    run.add_argument(
        "--policy",
        choices=[
            "verify-only",
            "eager-refresh",
            "repair-on-first-access",
            "periodic-refresh",
            "oldest-first",
            "p1",
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
        "--repair-slots-per-tick",
        type=int,
        default=1,
        help="maximum summaries repaired per Oldest-First maintenance tick",
    )

    run.add_argument(
        "--retrieval",
        choices=retrieval_choices,
        default="keyword",
    )

    run.add_argument("--verbose", action="store_true")
    run.add_argument("--verify-cost", type=float, help="override nominal cost per verification")
    run.add_argument("--repair-cost", type=float, help="override nominal cost per repair")

    run.add_argument(
        "--jsonl",
        type=Path,
        help="write structured event records",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    toy = sub.add_parser("run-toy", help="replay toy facts, summaries, and events")
    _add_run_arguments(toy)
    synthetic = sub.add_parser("run-synthetic", help="replay a saved synthetic workload")
    _add_run_arguments(synthetic, retrieval_choices=("keyword",))
    synthetic.add_argument("--workload", type=Path, required=True)
    generate = sub.add_parser("generate-synthetic", help="create a seeded synthetic workload JSON file")
    generate.add_argument("--memories", type=int, default=100)
    generate.add_argument("--seed", type=int, default=12345)
    generate.add_argument("--group-counts", type=int, nargs=3, metavar=("COLD", "WARM", "HOT"),
                          help="group sizes summing to --memories; default: balanced")
    for group, bounds in GROUP_BOUNDS.items():
        generate.add_argument(f"--{group}-queries", type=int, nargs=2, default=bounds,
                              metavar=("MIN", "MAX"), help=f"inclusive {group} query range; default: {bounds}")
    generate.add_argument("--output", type=Path, required=True, help="new workload file (must not exist)")
    args = parser.parse_args(argv)

    if args.command == "generate-synthetic":
        try:
            workload = generate_synthetic(SyntheticConfig(
                num_memories=args.memories, seed=args.seed, group_counts=args.group_counts,
                cold_queries=args.cold_queries, warm_queries=args.warm_queries, hot_queries=args.hot_queries,
            ))
            digest = workload.write(args.output)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(f"dataset={workload.config.dataset_id()} memories={workload.config.num_memories} "
              f"questions={len(workload.events) - workload.config.num_memories} "
              f"groups={workload.config.group_counts} seed={workload.config.seed} "
              f"sha256={digest} output={args.output}")
        return

    if args.command not in ("run-toy", "run-synthetic"):
        parser.print_help()
        return

    policies = {
        "verify-only": VerifyOnly,
        "eager-refresh": EagerRefresh,
        "repair-on-first-access": RepairOnFirstAccess,
        "p1": AccumulatedThreshold,
    }

    try:
        if args.policy == "oldest-first":
            policy = OldestFirst(args.refresh_interval_events, args.repair_slots_per_tick)
        elif args.policy == "periodic-refresh":
            policy = PeriodicRefresh(args.refresh_interval_events)
        else:
            policy = policies[args.policy]()
        if args.command == "run-synthetic":
            if args.jsonl and (
                args.jsonl.resolve() == args.workload.resolve()
                or (args.jsonl.exists() and args.workload.exists() and args.jsonl.samefile(args.workload))
            ):
                raise ValueError("--jsonl must differ from the input workload")
            workload = read_synthetic(args.workload)
            config = synthetic_run_config(workload)
        else:
            root = toy_root()
            config = load_config(root / "configs/toy.yaml")

        # Validate resolved costs before opening SQLite or loading a model.
        config = replace(
            config,
            verify_cost_units=config.verify_cost_units if args.verify_cost is None else args.verify_cost,
            repair_cost_units=config.repair_cost_units if args.repair_cost is None else args.repair_cost,
        )
        if args.command == "run-synthetic":
            db, ledger, summaries, retriever, events = load_synthetic(workload)
        else:
            db, ledger, summaries, retriever, events = load_toy(root / "data/toy")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    try:
        if args.retrieval == "hybrid":
            from verify_repair.retrieval.semantic import BGEM3FaissRetriever

            retriever = HybridRetriever(retriever, BGEM3FaissRetriever())

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
                ), encoding="utf-8",
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
    finally:
        db.close()


if __name__ == "__main__":
    main()
