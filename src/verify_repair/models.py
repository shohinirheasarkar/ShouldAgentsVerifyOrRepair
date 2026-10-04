"""Stable domain records and UTC timestamp conversion for the core API."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def stamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds")


def parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class ChangeType(StrEnum):
    INITIAL = "INITIAL"
    CORRECTION = "CORRECTION"
    UPDATE = "UPDATE"
    RETRACTION = "RETRACTION"
    TEMPORARY_EXCEPTION = "TEMPORARY_EXCEPTION"


@dataclass(frozen=True, order=True)
class FactVersionRef:
    fact_id: str
    version: int


@dataclass(frozen=True)
class FactVersion:
    fact_id: str
    version: int
    entity: str
    attribute: str
    value: str | None
    change_type: ChangeType
    valid_from: datetime
    valid_to: datetime | None
    supersedes: FactVersionRef | None
    event_id: str
    event_sequence: int

    @property
    def ref(self) -> FactVersionRef:
        return FactVersionRef(self.fact_id, self.version)


class FactNotFound(LookupError):
    pass


class FactVersionNotFound(LookupError):
    pass


class InvalidVersionTransition(ValueError):
    pass


class SummaryNotFound(LookupError):
    pass


class DependencyCycleError(ValueError):
    pass


class InvalidProvenanceError(ValueError):
    pass


class RepairBuildError(ValueError):
    pass


class RetrievalIndexError(RuntimeError):
    pass
