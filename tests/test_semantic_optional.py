"""Opt-in BGE-M3/FAISS integration; requires RUN_SEMANTIC=1 and model availability."""
import os
import pytest

from verify_repair.retrieval.hybrid import HybridRetriever
from verify_repair.retrieval.semantic import BGEM3FaissRetriever
from verify_repair.retrieval.base import RetrievalHit


@pytest.mark.semantic
@pytest.mark.skipif(os.getenv("RUN_SEMANTIC") != "1", reason="set RUN_SEMANTIC=1 with cached BGE-M3")
def test_semantic_index_update_and_hybrid():
    semantic = BGEM3FaissRetriever()
    semantic.index_summary("alice", "Alice is a researcher in Maryland")
    semantic.index_summary("bob", "Bob is an engineer in Oregon")
    assert semantic.search("Alice researcher", 1)[0].summary_id == "alice"
    semantic.index_summary("alice", "Alice is a researcher in Boston")
    assert semantic.search("Boston researcher", 1)[0].summary_id == "alice"
    class Keyword:
        def search(self, query_text, top_k):
            return [RetrievalHit("bob", 1.0, 1, "keyword")]
    assert {hit.summary_id for hit in HybridRetriever(Keyword(), semantic).search("Alice", 2)} == {"alice", "bob"}
