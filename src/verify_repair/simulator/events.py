"""Typed event records ordered by UTC timestamp and explicit sequence."""
from dataclasses import dataclass
from datetime import datetime

from verify_repair.models import ChangeType


@dataclass(frozen=True)
class CorrectionEvent:
    event_id: str
    timestamp: datetime
    sequence_number: int
    fact_id: str
    change_type: ChangeType
    value: str | None
    valid_to: datetime | None = None


@dataclass(frozen=True)
class QueryEvent:
    event_id: str
    timestamp: datetime
    sequence_number: int
    query_id: str
    query_text: str
    top_k: int
    expected_fact_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpiryEvent:
    """Internal transition when a bounded exception ceases to be effective."""
    event_id: str
    timestamp: datetime
    sequence_number: int
    fact_id: str
    temporary_event_id: str
