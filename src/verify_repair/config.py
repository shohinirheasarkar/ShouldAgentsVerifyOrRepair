"""Load and validate the small, reproducible YAML toy configuration."""
from pathlib import Path
import yaml

from verify_repair.simulator.engine import RunConfig


def load_config(path: str | Path) -> RunConfig:
    data = yaml.safe_load(Path(path).read_text())
    costs = data["costs"]
    if costs["mode"] != "nominal" or costs["verify"] < 0 or costs["repair"] < 0:
        raise ValueError("only nonnegative nominal costs are supported")
    return RunConfig(seed=int(data["seed"]), dataset_id=str(data["dataset_id"]),
                     verify_cost_units=float(costs["verify"]),
                     repair_cost_units=float(costs["repair"]))
