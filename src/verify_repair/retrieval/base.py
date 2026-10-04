"""Common retrieval result and backend interface."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RetrievalHit:
    summary_id: str
    score: float
    rank: int
    backend: str


class Retriever(Protocol):
    def index_summary(self, summary_id: str, text: str) -> None: ...
    def remove_summary(self, summary_id: str) -> None: ...
    def search(self, query_text: str, top_k: int) -> list[RetrievalHit]: ...
