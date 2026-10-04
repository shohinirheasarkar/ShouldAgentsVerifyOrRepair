"""SQLite FTS5/BM25 keyword retrieval, with stable summary-ID tie breaking."""
from __future__ import annotations

import re
import sqlite3

from verify_repair.db import transaction
from verify_repair.retrieval.base import RetrievalHit


class KeywordRetriever:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def index_summary(self, summary_id: str, text: str) -> None:
        with transaction(self.db):
            self.db.execute("DELETE FROM summary_fts WHERE summary_id=?", (summary_id,))
            self.db.execute("INSERT INTO summary_fts(summary_id,text) VALUES (?,?)", (summary_id, text))

    def remove_summary(self, summary_id: str) -> None:
        self.db.execute("DELETE FROM summary_fts WHERE summary_id=?", (summary_id,))

    def search(self, query_text: str, top_k: int) -> list[RetrievalHit]:
        if top_k <= 0:
            return []
        tokens = re.findall(r"\w+", query_text.lower(), flags=re.UNICODE)
        if not tokens:
            return []
        expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        rows = self.db.execute("""SELECT summary_id,bm25(summary_fts) AS distance FROM summary_fts
            WHERE summary_fts MATCH ? ORDER BY distance ASC,summary_id ASC LIMIT ?""",
            (expression, top_k)).fetchall()
        return [RetrievalHit(row["summary_id"], -float(row["distance"]), rank, "keyword")
                for rank, row in enumerate(rows, 1)]
