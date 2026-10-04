"""Load inspectable YAML toy facts, summaries, and event trace into fresh SQLite."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import yaml

from verify_repair.db import connect
from verify_repair.memory.ledger import FactLedger
from verify_repair.memory.summaries import SummaryStore
from verify_repair.maintenance.summary_builder import DeterministicTemplateSummaryBuilder
from verify_repair.models import ChangeType, FactVersionRef
from verify_repair.retrieval.keyword import KeywordRetriever
from verify_repair.simulator.events import CorrectionEvent, QueryEvent


def load_toy(data_dir: str | Path):
    root = Path(data_dir)
    facts = yaml.safe_load((root / "facts.yaml").read_text())
    summaries = yaml.safe_load((root / "summaries.yaml").read_text())
    events_data = yaml.safe_load((root / "events.yaml").read_text())
    db = connect()
    ledger, store = FactLedger(db), SummaryStore(db)
    for fact in facts:
        ledger.append_initial_fact(fact["fact_id"], fact["entity"], fact["attribute"],
                                   fact["value"], datetime.fromisoformat(fact["valid_from"]),
                                   f"initial:{fact['fact_id']}")
    builder = DeterministicTemplateSummaryBuilder()
    for spec in summaries:
        refs = {slot: FactVersionRef(fact_id, 1) for slot, fact_id in spec["slots"].items()}
        values = {slot: ledger.get_version(ref).value for slot, ref in refs.items()}
        text = builder.build(spec["template"], values)
        store.create_summary(spec["summary_id"], text, spec["template"], refs,
                             datetime.fromisoformat(spec["created_at"]))
    for spec in summaries:
        for parent in spec.get("depends_on", []):
            store.add_summary_dependency(parent, spec["summary_id"])
    events = []
    for raw in events_data:
        at = datetime.fromisoformat(raw["timestamp"])
        if raw["type"] == "query":
            events.append(QueryEvent(raw["event_id"], at, raw["sequence_number"],
                                     raw["query_id"], raw["query_text"], raw.get("top_k", 1),
                                     tuple(raw.get("expected_fact_ids", []))))
        else:
            events.append(CorrectionEvent(raw["event_id"], at, raw["sequence_number"],
                                          raw["fact_id"], ChangeType(raw["change_type"]),
                                          raw.get("value"), datetime.fromisoformat(raw["valid_to"])
                                          if raw.get("valid_to") else None))
    return db, ledger, store, KeywordRetriever(db), events
