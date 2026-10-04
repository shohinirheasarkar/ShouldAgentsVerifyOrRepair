"""Pure reciprocal rank fusion with configurable K and stable ties."""
from __future__ import annotations

from verify_repair.retrieval.base import RetrievalHit


def reciprocal_rank_fusion(rankings: list[list[RetrievalHit]], k: int = 60,
                           weights: list[float] | None = None, top_k: int | None = None) -> list[RetrievalHit]:
    if k < 0 or (weights is not None and len(weights) != len(rankings)):
        raise ValueError("invalid RRF parameters")
    chosen = weights or [1.0] * len(rankings)
    if any(weight < 0 for weight in chosen):
        raise ValueError("RRF weights must be nonnegative")
    scores: dict[str, float] = {}
    for hits, weight in zip(rankings, chosen):
        seen: set[str] = set()
        for rank, hit in enumerate(hits, 1):
            if hit.summary_id in seen:
                raise ValueError("duplicate summary in one ranking")
            seen.add(hit.summary_id)
            scores[hit.summary_id] = scores.get(hit.summary_id, 0.0) + weight / (k + rank)
    ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    if top_k is not None:
        ranked = ranked[:max(0, top_k)]
    return [RetrievalHit(summary_id, score, rank, "hybrid")
            for rank, (summary_id, score) in enumerate(ranked, 1)]
