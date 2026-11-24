"""Measured-cost profiles and reproducible keyed cost draws (Step 6.4).

A profile (``cost-profile-v1`` JSON, built by ``scripts/build_cost_profile.py``
from LLM-mode logs) stores measured samples per operation, optionally
stratified by the number of source facts in the summary. During replay every
VERIFY and REPAIR is charged one sample, drawn by hashing a key that does not
depend on the policy:

* VERIFY: ``(seed, query event ID, summary ID)``;
* REPAIR: ``(seed, summary ID, stale episode ID)``. Whatever triggers the
  repair (correction hook, query, tick, or the deferred queue), an episode's
  repair costs the same, so every policy and the oracle see identical draws.

Policies never see the draws. They receive the profile's mean per operation as
their fixed ``c_v`` / ``c_r`` (``CostProfile.estimates``), so P1/P2 keep their
fixed-cost behavior. Units are whatever the profile records (for example GPU
milliseconds); results from replay must be labeled as replay, not as
LLM-in-the-loop measurements.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from math import floor, isfinite
from pathlib import Path
from statistics import mean

FORMAT_VERSION = "cost-profile-v1"
OPERATIONS = ("verify", "repair")
# A stratum (summaries with a given number of facts) is used only when it has
# at least this many samples; otherwise the pooled samples are used.
MIN_STRATUM_SAMPLES = 5


@dataclass(frozen=True)
class CostProfile:
    profile_id: str
    unit: str
    # operation -> stratum ("all" or a fact count as a string) -> sorted samples
    samples: dict[str, dict[str, tuple[float, ...]]]
    source: dict = field(default_factory=dict, compare=False)
    sha256: str = ""

    def __post_init__(self) -> None:
        for operation in OPERATIONS:
            strata = self.samples.get(operation)
            if not strata or not strata.get("all"):
                raise ValueError(f"cost profile needs pooled samples for {operation}")
            for stratum, values in strata.items():
                if stratum != "all" and not stratum.isdigit():
                    raise ValueError(f"unknown cost-profile stratum {stratum!r}")
                if any(not isfinite(v) or v < 0 for v in values):
                    raise ValueError("cost samples must be finite and nonnegative")
                if list(values) != sorted(values):
                    raise ValueError("cost samples must be sorted")

    def estimates(self) -> tuple[float, float]:
        """Policy-visible (c_v, c_r): the mean pooled sample per operation."""
        return mean(self.samples["verify"]["all"]), mean(self.samples["repair"]["all"])

    def values_for(self, operation: str, fact_count: int | None) -> tuple[float, ...]:
        strata = self.samples[operation]
        stratum = strata.get(str(fact_count)) if fact_count is not None else None
        return stratum if stratum is not None and len(stratum) >= MIN_STRATUM_SAMPLES else strata["all"]

    def draw(self, operation: str, key: str, seed: int, fact_count: int | None = None) -> float:
        """Deterministic empirical draw: the same key always returns the same sample."""
        values = self.values_for(operation, fact_count)
        digest = hashlib.sha256(f"{self.profile_id}|{seed}|{operation}|{key}".encode()).digest()
        u = int.from_bytes(digest[:8], "big") / 2 ** 64
        return values[min(floor(u * len(values)), len(values) - 1)]

    def snapshot(self) -> dict:
        c_v, c_r = self.estimates()
        return {"profile_id": self.profile_id, "unit": self.unit, "sha256": self.sha256,
                "verify_estimate": c_v, "repair_estimate": c_r}

    def to_dict(self) -> dict:
        return {"format_version": FORMAT_VERSION, "profile_id": self.profile_id, "unit": self.unit,
                "operations": {op: {k: list(v) for k, v in strata.items()}
                               for op, strata in self.samples.items()},
                "source": self.source}

    def write(self, path: str | Path) -> str:
        encoded = (json.dumps(self.to_dict(), sort_keys=True, indent=1) + "\n").encode()
        with Path(path).open("xb") as stream:
            stream.write(encoded)
        return hashlib.sha256(encoded).hexdigest()


def read_cost_profile(path: str | Path) -> CostProfile:
    raw_bytes = Path(path).read_bytes()
    raw = json.loads(raw_bytes)
    if raw.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"unsupported cost profile format: {raw.get('format_version')}")
    samples = {op: {k: tuple(float(v) for v in values) for k, values in strata.items()}
               for op, strata in raw["operations"].items()}
    return CostProfile(raw["profile_id"], raw["unit"], samples, raw.get("source", {}),
                       hashlib.sha256(raw_bytes).hexdigest())


def build_profile(profile_id: str, unit: str, observations: list[dict], source: dict) -> CostProfile:
    """Group ``{"operation", "fact_count", "value"}`` observations into a profile."""
    samples: dict[str, dict[str, list[float]]] = {op: {"all": []} for op in OPERATIONS}
    for item in observations:
        operation, value = item["operation"], float(item["value"])
        if operation not in samples:
            raise ValueError(f"unknown operation {operation!r}")
        samples[operation]["all"].append(value)
        if item.get("fact_count") is not None:
            samples[operation].setdefault(str(int(item["fact_count"])), []).append(value)
    return CostProfile(profile_id, unit,
                       {op: {k: tuple(sorted(v)) for k, v in strata.items()}
                        for op, strata in samples.items()}, source)


def verify_key(event_id: str, summary_id: str) -> str:
    return f"{event_id}|{summary_id}"


def repair_key(summary_id: str, episode_id: str | None) -> str:
    return f"{summary_id}|{episode_id}"


# Cost metrics that can be extracted from timed LLM-mode logs.
METRICS = {
    "wall_ms": "operation wall-clock milliseconds (lookup + generation + validation + store/index)",
    "gpu_ms": "GPU milliseconds of the operation's LLM generation calls (CUDA events)",
    "total_tokens": "prompt + completion tokens of the operation's LLM calls",
}


def observations_from_logs(rows: list[dict], fact_counts: dict[str, int], metric: str) -> list[dict]:
    """One observation per VERIFY/REPAIR row of a timed run (``measure_latency``).

    ``wall_ms`` uses the operation's measured total (``timing_ms``); ``gpu_ms``
    and ``total_tokens`` use the maintenance LLM usage only, never the answer
    reader's. Rows without the metric (deterministic mode, untimed runs) raise.
    """
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {sorted(METRICS)}")
    observations = []
    for row in rows:
        operation = {"VERIFY": "verify", "REPAIR": "repair"}.get(row["action"])
        if operation is None:
            continue
        if metric == "wall_ms":
            timing = row.get("timing_ms") or {}
            value = timing.get(f"{operation}_total", timing.get("total"))
        else:
            usage = (row.get("maintenance_llm_metadata") or {}).get("usage") or {}
            value = usage.get(metric)
        if value is None:
            raise ValueError(f"row {row['event_id']} has no {metric}; replay with timing in LLM mode")
        observations.append({"operation": operation, "fact_count": fact_counts.get(row["summary_id"]),
                             "value": float(value), "query_visible": row.get("query_visible")})
    return observations
