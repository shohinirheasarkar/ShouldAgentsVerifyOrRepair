"""Combine lightweight keyword and optional semantic results via RRF."""
from __future__ import annotations

from verify_repair.retrieval.base import Retriever, RetrievalHit
from verify_repair.retrieval.rrf import reciprocal_rank_fusion


class HybridRetriever:
    def __init__(self, keyword_backend: Retriever, semantic_backend: Retriever,
                 rrf_k: int = 60, weights: tuple[float, float] = (1.0, 1.0)):
        self.keyword_backend = keyword_backend
        self.semantic_backend = semantic_backend
        self.rrf_k = rrf_k
        self.weights = weights

    def index_summary(self, summary_id: str, text: str) -> None:
        self.keyword_backend.index_summary(summary_id, text)
        self.semantic_backend.index_summary(summary_id, text)

    def remove_summary(self, summary_id: str) -> None:
        self.keyword_backend.remove_summary(summary_id)
        self.semantic_backend.remove_summary(summary_id)

    def search(self, query_text: str, top_k: int) -> list[RetrievalHit]:
        if top_k <= 0:
            return []
        # Fusion needs each backend's entire candidate set, not merely top_k.
        keyword = self.keyword_backend.search(query_text, 10_000)
        semantic = self.semantic_backend.search(query_text, 10_000)
        return reciprocal_rank_fusion([keyword, semantic], self.rrf_k, list(self.weights), top_k)
