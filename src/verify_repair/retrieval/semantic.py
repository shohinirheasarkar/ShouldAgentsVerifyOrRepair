"""Optional BGE-M3 dense-vector + FAISS adapter; imports only on construction."""
from __future__ import annotations

from importlib.util import find_spec

from verify_repair.retrieval.base import RetrievalHit


class BGEM3FaissRetriever:
    def __init__(self, model_name: str = "BAAI/bge-m3"):
        # On macOS, importing FAISS before initializing BGE-M3 can crash inside
        # native libraries. Check availability without importing FAISS, then
        # initialize the model before loading the FAISS extension.
        if find_spec("faiss") is None or find_spec("FlagEmbedding") is None:
            raise ImportError("install verify-repair[semantic] to use BGE-M3 + FAISS")
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise ImportError("install verify-repair[semantic] to use BGE-M3 + FAISS") from exc
        self.model = BGEM3FlagModel(model_name, use_fp16=False)
        import faiss
        import numpy as np
        self._faiss, self._np = faiss, np
        self._texts: dict[str, str] = {}
        self._ids: list[str] = []
        self._index = None

    def _embed(self, texts: list[str]):
        vectors = self._np.asarray(self.model.encode(
            texts, return_dense=True, return_sparse=False,
            return_colbert_vecs=False)["dense_vecs"], dtype="float32")
        self._faiss.normalize_L2(vectors)
        return vectors

    def rebuild(self, summaries: dict[str, str] | None = None) -> None:
        if summaries is not None:
            self._texts = dict(summaries)
        self._ids = sorted(self._texts)
        if not self._ids:
            self._index = None
            return
        vectors = self._embed([self._texts[summary_id] for summary_id in self._ids])
        self._index = self._faiss.IndexFlatIP(vectors.shape[1])
        self._index.add(vectors)

    def index_summary(self, summary_id: str, text: str) -> None:
        self._texts[summary_id] = text
        self.rebuild()

    def remove_summary(self, summary_id: str) -> None:
        self._texts.pop(summary_id, None)
        self.rebuild()

    def search(self, query_text: str, top_k: int) -> list[RetrievalHit]:
        if top_k <= 0 or self._index is None:
            return []
        # Request all rows before ID tie-breaking, otherwise FAISS may truncate
        # an equal-score group in an implementation-dependent order.
        scores, rows = self._index.search(self._embed([query_text]), len(self._ids))
        pairs = sorted(((self._ids[int(row)], float(score)) for row, score in zip(rows[0], scores[0])
                        if row >= 0), key=lambda item: (-item[1], item[0]))
        return [RetrievalHit(summary_id, score, rank, "semantic")
                for rank, (summary_id, score) in enumerate(pairs[:top_k], 1)]
