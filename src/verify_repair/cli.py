"""Generate or replay deterministic workloads; costs are nominal accounting units."""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from verify_repair.config import load_config
from verify_repair.demand import PredictionConfig
from verify_repair.experiments import (
    POLICY_NAMES,
    RESULT_FIELDS,
    make_policy,
    read_rows,
    run_experiment,
    summarize_rows,
    summary_fields,
    write_rows,
)
from verify_repair.retrieval.hybrid import HybridRetriever
from verify_repair.simulator.engine import RunConfig, Simulator
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
        choices=POLICY_NAMES,
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
    run.add_argument("--p2-half-life-seconds", type=float,
                     help="P2 access-rate half-life (default: 3600 seconds)")
    run.add_argument("--p2-horizon-seconds", type=float,
                     help="P2 rolling forecast horizon (default: 86400 seconds)")
    run.add_argument("--p2-min-accesses", type=int,
                     help="P2 minimum completed accesses for a confident estimate (default: 3)")

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
    experiment = sub.add_parser("run-experiment",
                                help="replay saved workloads under several policies and costs; write CSV")
    experiment.add_argument("--workload", type=Path, nargs="+", required=True)
    experiment.add_argument("--policies", nargs="+", choices=POLICY_NAMES, default=list(POLICY_NAMES))
    experiment.add_argument("--verify-cost", type=float, default=1.0, help="nominal cost per verification")
    experiment.add_argument("--repair-costs", type=float, nargs="+", default=[5.0],
                            help="nominal repair costs to sweep (one replay per value)")
    experiment.add_argument("--refresh-interval-events", type=int, default=100)
    experiment.add_argument("--repair-slots-per-tick", type=int, default=1)
    experiment.add_argument("--measure-latency", action="store_true",
                            help="record wall-clock query latency (non-deterministic)")
    experiment.add_argument("--output", type=Path, required=True, help="new results CSV (must not exist)")
    summarize = sub.add_parser("summarize-experiment", help="average result CSVs across seeds with 95%% CIs")
    summarize.add_argument("--input", type=Path, nargs="+", required=True)
    summarize.add_argument("--output", type=Path, required=True, help="new summary CSV (must not exist)")
    args = parser.parse_args(argv)

    if args.command == "run-experiment":
        try:
            if args.output.exists():
                raise ValueError(f"{args.output} already exists")
            if any(args.output.resolve() == path.resolve() for path in args.workload):
                raise ValueError("--output must differ from the input workloads")
            cost_pairs = [(args.verify_cost, cost) for cost in args.repair_costs]
            # Validate every setting before creating the output file.
            for pair in cost_pairs:
                replace(RunConfig(), verify_cost_units=pair[0], repair_cost_units=pair[1])
            for name in args.policies:
                make_policy(name, args.refresh_interval_events, args.repair_slots_per_tick)
            rows = write_rows(args.output, run_experiment(
                args.workload, args.policies, cost_pairs,
                refresh_interval_events=args.refresh_interval_events,
                repair_slots_per_tick=args.repair_slots_per_tick,
                measure_latency=args.measure_latency,
            ), RESULT_FIELDS)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(f"rows={rows} workloads={len(args.workload)} policies={len(args.policies)} "
              f"repair_costs={args.repair_costs} verify_cost={args.verify_cost:g} output={args.output}")
        return

    if args.command == "summarize-experiment":
        try:
            rows = write_rows(args.output, summarize_rows(read_rows(args.input)), summary_fields())
        except (ValueError, OSError, KeyError) as exc:
            parser.error(str(exc))
        print(f"settings={rows} output={args.output}")
        return

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

    try:
        prediction_options = {
            "half_life_seconds": args.p2_half_life_seconds,
            "horizon_seconds": args.p2_horizon_seconds,
            "min_accesses": args.p2_min_accesses,
        }
        if args.policy != "p2" and any(value is not None for value in prediction_options.values()):
            raise ValueError("--p2-* options require --policy p2")
        policy = make_policy(
            args.policy, args.refresh_interval_events, args.repair_slots_per_tick,
            PredictionConfig(**{
                key: value for key, value in prediction_options.items() if value is not None
            }) if args.policy == "p2" else None,
        )
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
