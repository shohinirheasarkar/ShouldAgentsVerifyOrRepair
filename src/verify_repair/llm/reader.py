"""One fixed Answer Reader shared across every maintenance policy (Step 5.4).

Inputs: an ``EvidencePacket`` assembled from a query's retrieved/verified/repaired
evidence (never policy name or hidden staleness flags) and an ``LLMClient``.
Outputs: ``AnswerResult`` with generated answer text, abstention flag, cited
sources, evidence refs, and token/timing usage. Also provides
``render_reader_question()`` for keyword-style traces without mutating
``QueryEvent`` or ``trace_hash``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from verify_repair.llm.client import LLMClient, LLMRequest, LLMUsage
from verify_repair.llm.prompts import (
    PROMPT_VERSION,
    READER_PROMPT_ID,
    build_reader_prompt,
)
from verify_repair.llm.validation import validate_reader_output
from verify_repair.memory.ledger import FactLedger
from verify_repair.models import FactVersionRef, LLMTimeoutError, LLMValidationError
from verify_repair.simulator.events import QueryEvent


@dataclass(frozen=True)
class EvidencePacket:
    """Policy-neutral evidence handed to the frozen Answer Reader for one query."""

    query_id: str
    question_text: str
    summary_ids: tuple[str, ...]
    evidence_mode: str
    evidence_text: str
    source_ref_labels: tuple[str, ...]


@dataclass(frozen=True)
class AnswerResult:
    """Generated answer, abstention flag, citations, and usage for one query."""

    query_id: str
    question_text: str
    answer_text: str
    abstained: bool
    cited_sources: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    evidence_mode: str
    prompt_id: str
    prompt_version: str
    llm_metadata: dict[str, Any]


def format_ref_labels(source_refs: dict[str, FactVersionRef | None]) -> tuple[str, ...]:
    """Deterministic sorted tuple of ``<fact_id>@v<version>`` strings."""
    labels = sorted({
        f"{ref.fact_id}@v{ref.version}"
        for ref in source_refs.values()
        if ref is not None
    })
    return tuple(labels)


def format_authoritative_evidence(
    current_evidence: dict[str, str | None],
    current_source_refs: dict[str, FactVersionRef | None],
) -> str:
    """Deterministic verified-evidence text fallback when VERIFY is non-LLM."""
    lines = []
    for slot in sorted(current_evidence):
        ref = current_source_refs.get(slot)
        ref_label = f"{ref.fact_id}@v{ref.version}" if ref is not None else "none"
        value = "[RETRACTED]" if current_evidence[slot] is None else current_evidence[slot]
        lines.append(f"{slot}: {value} [{ref_label}]")
    return "; ".join(lines)


def render_reader_question(event: QueryEvent, ledger: FactLedger) -> str:
    """Return a clear natural-language question for the reader without leaking values.

    Traces that already carry natural-language questions (e.g., ``synthetic-v1``
    or LongMemEval) use ``event.query_text`` verbatim. Keyword-style traces
    (``data/toy`` and ``synthetic-v2``) derive the question strictly from the
    immutable ``(entity, attribute)`` key of ``event.expected_fact_ids``.
    """
    text = event.query_text.strip()
    if text.endswith("?") or not event.expected_fact_ids:
        return text

    targets: list[tuple[str, str]] = []
    for fact_id in event.expected_fact_ids:
        history = ledger.get_history(fact_id)
        if history:
            targets.append((history[0].entity, history[0].attribute))
    if not targets:
        return text
    if len(targets) == 1:
        entity, attribute = targets[0]
        return f"What is the current {attribute} of {entity}?"
    parts = ", ".join(f"{entity}'s {attribute}" for entity, attribute in targets)
    return f"What are the current values for {parts}?"


def _sum_usage(usages: list[LLMUsage]) -> dict[str, Any]:
    wall_vals = [u.wall_ms for u in usages if u.wall_ms is not None]
    gpu_vals = [u.gpu_ms for u in usages if u.gpu_ms is not None]
    return {
        "prompt_tokens": sum(u.prompt_tokens for u in usages),
        "completion_tokens": sum(u.completion_tokens for u in usages),
        "total_tokens": sum(u.total_tokens for u in usages),
        "wall_ms": sum(wall_vals) if len(wall_vals) == len(usages) and wall_vals else None,
        "gpu_ms": sum(gpu_vals) if len(gpu_vals) == len(usages) and gpu_vals else None,
    }


class AnswerReader:
    """Frozen local-LLM answer reader identical across all maintenance policies."""

    def __init__(self, client: LLMClient):
        self.client = client

    def answer(self, packet: EvidencePacket) -> AnswerResult:
        feedback: str | None = None
        usages: list[LLMUsage] = []
        last_error: LLMValidationError | None = None
        max_attempts = self.client.config.max_retries + 1
        allowed_set = set(packet.source_ref_labels)

        for attempt in range(1, max_attempts + 1):
            system_prompt, user_prompt = build_reader_prompt(
                question_text=packet.question_text,
                evidence_text=packet.evidence_text,
                allowed_sources=packet.source_ref_labels,
                validation_feedback=feedback,
            )
            request = LLMRequest(
                role="reader",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_new_tokens=self.client.config.reader_max_new_tokens,
                timeout_seconds=self.client.config.timeout_seconds,
                prompt_id=READER_PROMPT_ID,
                prompt_version=PROMPT_VERSION,
            )
            try:
                response = self.client.generate(request)
            except LLMTimeoutError:
                raise
            usages.append(response.usage)
            try:
                validated = validate_reader_output(response.text, allowed_set)
            except LLMValidationError as exc:
                last_error = exc
                feedback = str(exc)
                continue

            return AnswerResult(
                query_id=packet.query_id,
                question_text=packet.question_text,
                answer_text=validated.answer_text,
                abstained=validated.abstained,
                cited_sources=validated.cited_sources,
                evidence_refs=packet.source_ref_labels,
                evidence_mode=packet.evidence_mode,
                prompt_id=READER_PROMPT_ID,
                prompt_version=PROMPT_VERSION,
                llm_metadata={
                    "role": "reader",
                    "prompt_id": READER_PROMPT_ID,
                    "prompt_version": PROMPT_VERSION,
                    "model_id": response.model_id,
                    "model_revision": response.model_revision,
                    "quantization": response.quantization,
                    "attempts": attempt,
                    "finish_reason": response.finish_reason,
                    "usage": _sum_usage(usages),
                },
            )

        assert last_error is not None
        raise LLMValidationError(
            f"AnswerReader failed validation after {max_attempts} attempt(s): {last_error}"
        ) from last_error
