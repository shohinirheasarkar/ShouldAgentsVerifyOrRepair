"""Evaluator-only scoring of generated answers against event-time ledger truth.

Inputs: a populated ``FactLedger`` (after replay), the trace ``events``, and
``RunResult.answers`` produced by the frozen ``AnswerReader``.
Outputs: per-query ``AnswerScore`` rows and an aggregate ``AnswerSummary``
reporting exact match, token F1, abstention accuracy/rates, and outdated-fact
errors strictly separated from substrate ``used_stale_source``.
"""
from __future__ import annotations

import re
import string
from collections import Counter
from dataclasses import asdict, dataclass

from verify_repair.memory.ledger import FactLedger
from verify_repair.models import stamp
from verify_repair.simulator.engine import RunResult
from verify_repair.simulator.events import CorrectionEvent, QueryEvent

_ARTICLES_RE = re.compile(r"\b(a|an|the)\b")
_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def normalize_answer(text: str) -> str:
    """Standard SQuAD-style normalization: lowercase, strip punctuation/articles/whitespace."""
    lowered = text.lower().translate(_PUNCT_TABLE)
    without_articles = _ARTICLES_RE.sub(" ", lowered)
    return " ".join(without_articles.split())


def exact_match(prediction: str, gold: str) -> float:
    return 1.0 if normalize_answer(prediction) == normalize_answer(gold) else 0.0


def token_f1(prediction: str, gold: str) -> float:
    pred_tokens = normalize_answer(prediction).split()
    gold_tokens = normalize_answer(gold).split()
    if not pred_tokens and not gold_tokens:
        return 1.0
    if not pred_tokens or not gold_tokens:
        return 0.0
    common = sum((Counter(pred_tokens) & Counter(gold_tokens)).values())
    if common == 0:
        return 0.0
    precision = common / len(pred_tokens)
    recall = common / len(gold_tokens)
    return 2.0 * precision * recall / (precision + recall)


def _contains_normalized_value(prediction: str, value: str) -> bool:
    norm_pred = normalize_answer(prediction)
    norm_val = normalize_answer(value)
    if not norm_pred or not norm_val:
        return False
    pattern = r"(?<!\w)" + re.escape(norm_val) + r"(?!\w)"
    return re.search(pattern, norm_pred) is not None


@dataclass(frozen=True)
class EventTimeTruth:
    """Authoritative event-time target values and superseded historical values."""

    expected_values: tuple[str | None, ...]
    outdated_values: tuple[str, ...]
    should_abstain: bool


def resolve_event_time_truth(ledger: FactLedger, event: QueryEvent) -> EventTimeTruth:
    """Resolve current truth and all prior superseded values at ``event.timestamp``."""
    expected: list[str | None] = []
    outdated: set[str] = set()
    now_stamp = stamp(event.timestamp)

    for fact_id in event.expected_fact_ids:
        history = ledger.get_history(fact_id)
        if not history:
            expected.append(None)
            continue
        current = ledger.get_current_record(
            history[0].entity, history[0].attribute, event.timestamp
        )
        current_value = current.value if current is not None else None
        expected.append(current_value)
        for record in history:
            if stamp(record.valid_from) <= now_stamp and record.value is not None:
                if record.value != current_value:
                    outdated.add(record.value)

    should_abstain = (not expected) or all(value is None for value in expected)
    return EventTimeTruth(
        expected_values=tuple(expected),
        outdated_values=tuple(sorted(outdated)),
        should_abstain=should_abstain,
    )


@dataclass(frozen=True)
class AnswerScore:
    """Per-query generated answer evaluation (evaluator-only)."""

    query_id: str
    event_id: str
    event_timestamp: str
    summary_ids: tuple[str, ...]
    actions: tuple[str, ...]
    evidence_mode: str
    used_stale_source: bool
    required_fact_current: bool
    question_text: str
    predicted_answer: str
    abstained: bool
    evidence_refs: tuple[str, ...]
    cited_sources: tuple[str, ...]
    expected_values: tuple[str | None, ...]
    outdated_values: tuple[str, ...]
    should_abstain: bool
    evidence_insufficient: bool
    exact_match: float
    token_f1: float
    abstention_correct: bool | None
    false_abstention: bool
    missed_abstention: bool
    outdated_fact_error: bool

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in (
            "summary_ids",
            "actions",
            "evidence_refs",
            "cited_sources",
            "expected_values",
            "outdated_values",
        ):
            data[key] = list(data[key])
        return data


@dataclass(frozen=True)
class AnswerSummary:
    """Aggregate answer-quality metrics kept distinct from substrate stale-use."""

    total_queries: int
    answerable_queries: int
    unanswerable_queries: int
    exact_match_mean: float | None
    token_f1_mean: float | None
    answerable_exact_match_mean: float | None
    answerable_token_f1_mean: float | None
    abstention_count: int
    abstention_rate: float | None
    abstention_accuracy: float | None
    false_abstention_count: int
    false_abstention_rate: float | None
    missed_abstention_count: int
    outdated_fact_error_count: int
    outdated_fact_error_rate: float | None
    stale_source_use_count: int
    stale_source_use_rate: float | None
    citation_present_rate: float | None
    total_prompt_tokens: int
    total_completion_tokens: int


def _mean_or_none(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def score_run_answers(
    ledger: FactLedger,
    events: list[CorrectionEvent | QueryEvent],
    result: RunResult,
) -> tuple[list[AnswerScore], AnswerSummary]:
    """Score every saved answer in ``result.answers`` against event-time ledger truth."""
    queries_by_id = {
        event.query_id: event
        for event in events
        if isinstance(event, QueryEvent)
    }

    scores: list[AnswerScore] = []
    for row in result.answers:
        query_id = row["query_id"]
        if query_id not in queries_by_id:
            raise ValueError(f"answer row references unknown query_id {query_id!r}")
        event = queries_by_id[query_id]
        truth = resolve_event_time_truth(ledger, event)

        abstained = bool(row["abstained"])
        predicted = str(row["answer_text"])
        required_current = bool(row["required_fact_current"])
        used_stale = bool(row["used_stale_source"])
        evidence_insufficient = not required_current

        if truth.should_abstain:
            em = 1.0 if abstained else 0.0
            f1 = 1.0 if abstained else 0.0
            abstention_correct: bool | None = abstained
            false_abstention = False
            missed_abstention = not abstained
        else:
            gold_terms = [val for val in truth.expected_values if val is not None]
            gold_joined = " ".join(gold_terms)
            if abstained:
                em = 0.0
                f1 = 0.0
            else:
                em = exact_match(predicted, gold_joined)
                f1 = token_f1(predicted, gold_joined)
            abstention_correct = None
            false_abstention = abstained and required_current
            missed_abstention = False

        outdated_error = (
            (not abstained)
            and em < 1.0
            and any(_contains_normalized_value(predicted, old) for old in truth.outdated_values)
        )

        scores.append(
            AnswerScore(
                query_id=query_id,
                event_id=event.event_id,
                event_timestamp=stamp(event.timestamp),
                summary_ids=tuple(row["summary_ids"]),
                actions=tuple(row["actions"]),
                evidence_mode=str(row["evidence_mode"]),
                used_stale_source=used_stale,
                required_fact_current=required_current,
                question_text=str(row["question_text"]),
                predicted_answer=predicted,
                abstained=abstained,
                evidence_refs=tuple(row["evidence_refs"]),
                cited_sources=tuple(row["cited_sources"]),
                expected_values=truth.expected_values,
                outdated_values=truth.outdated_values,
                should_abstain=truth.should_abstain,
                evidence_insufficient=evidence_insufficient,
                exact_match=em,
                token_f1=f1,
                abstention_correct=abstention_correct,
                false_abstention=false_abstention,
                missed_abstention=missed_abstention,
                outdated_fact_error=outdated_error,
            )
        )

    total = len(scores)
    answerable = [s for s in scores if not s.should_abstain]
    unanswerable = [s for s in scores if s.should_abstain]
    answerable_with_evidence = [s for s in answerable if not s.evidence_insufficient]

    prompt_tokens = 0
    completion_tokens = 0
    for log_row in result.logs:
        usage = log_row.get("llm_usage")
        if isinstance(usage, dict):
            prompt_tokens += int(usage.get("prompt_tokens", 0))
            completion_tokens += int(usage.get("completion_tokens", 0))

    summary = AnswerSummary(
        total_queries=total,
        answerable_queries=len(answerable),
        unanswerable_queries=len(unanswerable),
        exact_match_mean=_mean_or_none([s.exact_match for s in scores]),
        token_f1_mean=_mean_or_none([s.token_f1 for s in scores]),
        answerable_exact_match_mean=_mean_or_none([s.exact_match for s in answerable]),
        answerable_token_f1_mean=_mean_or_none([s.token_f1 for s in answerable]),
        abstention_count=sum(s.abstained for s in scores),
        abstention_rate=_mean_or_none([float(s.abstained) for s in scores]),
        abstention_accuracy=_mean_or_none([float(bool(s.abstention_correct)) for s in unanswerable]),
        false_abstention_count=sum(s.false_abstention for s in scores),
        false_abstention_rate=_mean_or_none(
            [float(s.false_abstention) for s in answerable_with_evidence]
        ),
        missed_abstention_count=sum(s.missed_abstention for s in scores),
        outdated_fact_error_count=sum(s.outdated_fact_error for s in scores),
        outdated_fact_error_rate=_mean_or_none([float(s.outdated_fact_error) for s in scores]),
        stale_source_use_count=sum(s.used_stale_source for s in scores),
        stale_source_use_rate=_mean_or_none([float(s.used_stale_source) for s in scores]),
        citation_present_rate=_mean_or_none(
            [float(bool(s.cited_sources)) for s in scores if not s.abstained]
        ),
        total_prompt_tokens=prompt_tokens,
        total_completion_tokens=completion_tokens,
    )
    return scores, summary
