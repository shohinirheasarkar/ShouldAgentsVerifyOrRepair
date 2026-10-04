"""RRF arithmetic and hybrid ranking without model downloads."""
from verify_repair.retrieval.base import RetrievalHit
from verify_repair.retrieval.hybrid import HybridRetriever
from verify_repair.retrieval.rrf import reciprocal_rank_fusion


def hits(ids, backend):
    return [RetrievalHit(summary_id, 1.0 / rank, rank, backend)
            for rank, summary_id in enumerate(ids, 1)]


class FakeRetriever:
    def __init__(self, ids, name):
        self.ids, self.name = ids, name

    def search(self, query_text, top_k):
        return hits(self.ids[:top_k], self.name)

    def index_summary(self, summary_id, text):
        pass

    def remove_summary(self, summary_id):
        pass


def test_rrf_exact_score_and_stable_tie():
    result = reciprocal_rank_fusion([hits(["b", "a"], "keyword"),
                                     hits(["a", "c"], "semantic")], k=1)
    assert [item.summary_id for item in result] == ["a", "b", "c"]
    assert result[0].score == 1 / 3 + 1 / 2
    assert result[1].score == 1 / 2
    assert result[2].score == 1 / 3
    tied = reciprocal_rank_fusion([hits(["b"], "keyword"), hits(["a"], "semantic")], k=1)
    assert [item.summary_id for item in tied] == ["a", "b"]


def test_hybrid_top_k_and_replay():
    hybrid = HybridRetriever(FakeRetriever(["b", "a"], "keyword"),
                             FakeRetriever(["a", "c"], "semantic"), rrf_k=1)
    assert [item.summary_id for item in hybrid.search("query", 2)] == ["a", "b"]
    assert hybrid.search("query", 2) == hybrid.search("query", 2)
