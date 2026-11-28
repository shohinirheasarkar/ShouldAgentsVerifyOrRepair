"""Pure deterministic validators for LLM VERIFY, REPAIR, and Answer Reader JSON outputs.

Inputs: raw model output string plus authoritative ``FactVersion`` records or
allowed source refs. Outputs: validated typed structures; raises
``LLMVerificationError`` or ``LLMValidationError`` on any schema, provenance,
preservation, stale-value leakage, or unsupported-claim violation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from verify_repair.llm.prompts import format_source_ref
from verify_repair.maintenance.summary_builder import RepairBuildRequest, RepairClaim
from verify_repair.models import FactVersion, LLMValidationError, LLMVerificationError

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)

_RETRACTION_MARKERS = (
    "retracted",
    "[retracted]",
    "removed",
    "unavailable",
    "not available",
    "no longer available",
    "no longer",
    "unknown",
    "not known",
    "none",
    "deleted",
    "withdrawn",
    "unspecified",
    "not specified",
    "not provided",
    "not listed",
    "no information",
    "no record",
    "does not have",
    "has no ",
    "unassigned",
    "not assigned",
    "revoked",
    "n/a",
    "missing",
    "redacted",
    "cleared",
)

_ABSTENTION_MARKERS = frozenset({
    "",
    "insufficient_evidence",
    "insufficient evidence",
    "i don't know",
    "i do not know",
    "unknown",
    "not available",
    "unavailable",
    "retracted",
    "[retracted]",
    "cannot answer",
    "no evidence",
    "n/a",
})


@dataclass(frozen=True)
class ValidatedVerifyOutput:
    verified_evidence: str
    cited_sources: tuple[str, ...]
    retracted_slots: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedRepairOutput:
    summary_text: str
    claims: tuple[RepairClaim, ...]
    fidelity: dict[str, float]


@dataclass(frozen=True)
class ValidatedReaderOutput:
    answer_text: str
    abstained: bool
    cited_sources: tuple[str, ...]


def contains_term(text: str, term: str) -> bool:
    """Case-insensitive boundary-aware containment check for factual values/entities."""
    cleaned = term.strip()
    if not cleaned:
        return False
    pattern = r"(?<!\w)" + re.escape(cleaned) + r"(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _has_retraction_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _RETRACTION_MARKERS)


def _is_unchanged_identity_slot(
    slot: str,
    fact: FactVersion,
    changed_set: set[str],
) -> bool:
    """True for an unchanged ``name`` slot whose value is simply the entity name itself."""
    return (
        slot not in changed_set
        and fact.attribute == "name"
        and fact.value is not None
        and fact.value.strip().lower() == fact.entity.strip().lower()
    )


def extract_json_object(raw_text: str, *, error_cls: type[Exception] = LLMValidationError) -> dict:
    """Parse a single JSON object, stripping optional markdown code fences."""
    stripped = raw_text.strip()
    if not stripped:
        raise error_cls("empty LLM output")
    match = _FENCE_RE.match(stripped)
    if match:
        stripped = match.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise error_cls(f"invalid JSON output: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise error_cls(f"expected JSON object, got {type(parsed).__name__}")
    return parsed


def validate_verify_output(
    raw_text: str,
    old_facts: dict[str, FactVersion],
    current_facts: dict[str, FactVersion],
    changed_slots: tuple[str, ...],
    *,
    query_text: str = "",
) -> ValidatedVerifyOutput:
    """Ground query-scoped evidence in current facts, without requiring a full repair.

    Explicit attribute/entity mentions identify required query slots. For free-form
    questions without literal attribute names (e.g. "Where does Alice live?"),
    validate the cited facts instead; this is a grounding check, not a general
    semantic question parser. Superseded-value leakage is checked across all changes.
    """
    parsed = extract_json_object(raw_text, error_cls=LLMVerificationError)
    evidence = parsed.get("verified_evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        raise LLMVerificationError("verified_evidence must be a non-empty string")
    evidence = evidence.strip()
    max_len = max(500, 120 * max(1, len(current_facts)))
    if len(evidence) > max_len:
        raise LLMVerificationError(f"verified_evidence exceeds maximum length ({len(evidence)} > {max_len})")

    cited_raw = parsed.get("cited_sources")
    if not isinstance(cited_raw, list) or not cited_raw or not all(isinstance(x, str) for x in cited_raw):
        raise LLMVerificationError("cited_sources must be a non-empty list of source_ref strings")

    allowed_refs = {format_source_ref(fact) for fact in current_facts.values()}
    cited_clean = tuple(dict.fromkeys(ref.strip() for ref in cited_raw))
    unknown_refs = [ref for ref in cited_clean if ref not in allowed_refs]
    if unknown_refs:
        raise LLMVerificationError(
            f"cited_sources contains unknown or superseded refs: {unknown_refs}"
        )

    cited_slots = {
        slot for slot, fact in current_facts.items()
        if format_source_ref(fact) in cited_clean
    }
    named_entities = {
        fact.entity for fact in current_facts.values()
        if contains_term(query_text, fact.entity)
    }
    queried_slots = {
        slot for slot, fact in current_facts.items()
        if contains_term(query_text, fact.attribute)
        and (not named_entities or fact.entity in named_entities)
    }
    required_slots = queried_slots or cited_slots
    missing_citations = sorted(required_slots - cited_slots)
    if missing_citations:
        raise LLMVerificationError(
            f"cited_sources missing current refs for queried slots: {missing_citations}"
        )

    retracted_raw = parsed.get("retracted_slots")
    if retracted_raw is None:
        retracted_raw = []
    if not isinstance(retracted_raw, list) or not all(isinstance(x, str) for x in retracted_raw):
        raise LLMVerificationError("retracted_slots must be a list of slot names")
    unknown_retracted = [slot for slot in retracted_raw if slot not in current_facts]
    if unknown_retracted:
        raise LLMVerificationError(f"retracted_slots contains unknown slots: {unknown_retracted}")
    retracted_clean = tuple(dict.fromkeys(retracted_raw))

    active_terms = {
        fact.value.strip().lower()
        for fact in current_facts.values()
        if fact.value is not None
    } | {fact.entity.strip().lower() for fact in current_facts.values()}

    for slot in sorted(required_slots):
        cur_fact = current_facts[slot]
        if cur_fact.value is not None:
            if not contains_term(evidence, cur_fact.value):
                raise LLMVerificationError(
                    f"verified_evidence missing current value {cur_fact.value!r} for queried/cited slot {slot!r}"
                )
        else:
            if not _has_retraction_marker(evidence) and slot not in retracted_clean:
                raise LLMVerificationError(
                    f"verified_evidence must acknowledge retraction of slot {slot!r}"
                )

    for slot in changed_slots:
        cur_fact = current_facts[slot]
        old_fact = old_facts.get(slot)
        if (
            old_fact is not None
            and old_fact.value is not None
            and old_fact.value != cur_fact.value
            and old_fact.value.strip().lower() not in active_terms
            and contains_term(evidence, old_fact.value)
        ):
            raise LLMVerificationError(
                f"verified_evidence leaked superseded value {old_fact.value!r} for slot {slot!r}"
            )

    return ValidatedVerifyOutput(
        verified_evidence=evidence,
        cited_sources=cited_clean,
        retracted_slots=retracted_clean,
    )


def validate_repair_output(
    raw_text: str,
    request: RepairBuildRequest,
) -> ValidatedRepairOutput:
    """Validate LLM REPAIR output for exact provenance, grounding, and preservation."""
    parsed = extract_json_object(raw_text, error_cls=LLMValidationError)
    summary_text = parsed.get("summary_text")
    if not isinstance(summary_text, str) or not summary_text.strip():
        raise LLMValidationError("summary_text must be a non-empty string")
    summary_text = summary_text.strip()

    max_len = max(400, 4 * len(request.old_text) + 200, 100 * len(request.current_facts))
    if len(summary_text) > max_len:
        raise LLMValidationError(
            f"summary_text exceeds maximum allowed length ({len(summary_text)} > {max_len})"
        )

    claims_raw = parsed.get("claims")
    if not isinstance(claims_raw, list) or not claims_raw:
        raise LLMValidationError("claims must be a non-empty list of claim objects")

    seen_slots: set[str] = set()
    claims_by_slot: dict[str, RepairClaim] = {}
    expected_slots = set(request.current_facts)
    changed_set = set(request.changed_slots)

    for item in claims_raw:
        if not isinstance(item, dict):
            raise LLMValidationError("each claim must be a JSON object")
        slot = item.get("slot")
        statement = item.get("statement")
        source_ref = item.get("source_ref")
        if not isinstance(slot, str) or slot not in expected_slots:
            raise LLMValidationError(f"unsupported or unknown claim slot: {slot!r}")
        if slot in seen_slots:
            raise LLMValidationError(f"duplicate claim for slot {slot!r}")
        if not isinstance(statement, str) or not statement.strip():
            raise LLMValidationError(f"claim statement for slot {slot!r} must be non-empty")
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise LLMValidationError(f"claim for slot {slot!r} is missing source_ref")

        expected_ref = format_source_ref(request.current_facts[slot])
        if source_ref.strip() != expected_ref:
            raise LLMValidationError(
                f"claim for slot {slot!r} cited {source_ref!r}, expected exact current ref {expected_ref!r}"
            )
        seen_slots.add(slot)
        claims_by_slot[slot] = RepairClaim(
            slot=slot,
            statement=statement.strip(),
            source_ref=expected_ref,
        )

    required_claim_slots = {
        slot
        for slot, fact in request.current_facts.items()
        if not _is_unchanged_identity_slot(slot, fact, changed_set)
    }
    missing_slots = sorted(required_claim_slots - seen_slots)
    if missing_slots:
        raise LLMValidationError(f"claims missing required slots: {missing_slots}")

    # Preserve every entity that appeared in the stored summary (keeps FTS5 retrieval working
    # while safely allowing synthetic-v2 linked_* slots whose borrowed entity was not in old_text).
    entities_in_old = {
        fact.entity
        for fact in request.current_facts.values()
        if contains_term(request.old_text, fact.entity)
    }
    for entity in sorted(entities_in_old):
        if not contains_term(summary_text, entity):
            raise LLMValidationError(
                f"summary_text dropped required entity {entity!r} present in prior summary"
            )

    active_terms = {
        fact.value.strip().lower()
        for fact in request.current_facts.values()
        if fact.value is not None
    } | {entity.strip().lower() for entity in entities_in_old}

    unchanged_total = 0
    unchanged_preserved = 0
    changed_total = 0
    changed_updated = 0

    for slot in sorted(request.current_facts):
        cur_fact = request.current_facts[slot]
        old_fact = request.old_facts.get(slot)
        claim = claims_by_slot.get(slot)
        is_changed = slot in changed_set

        if cur_fact.value is not None:
            if not contains_term(summary_text, cur_fact.value):
                kind = "changed" if is_changed else "unchanged"
                raise LLMValidationError(
                    f"summary_text missing {kind} value {cur_fact.value!r} for slot {slot!r}"
                )
            if claim is not None and not contains_term(claim.statement, cur_fact.value):
                raise LLMValidationError(
                    f"claim statement for slot {slot!r} missing current value {cur_fact.value!r}"
                )
            if is_changed:
                changed_total += 1
                changed_updated += 1
            else:
                unchanged_total += 1
                unchanged_preserved += 1
        else:
            if not _has_retraction_marker(summary_text):
                raise LLMValidationError(
                    f"summary_text must explicitly mark retracted slot {slot!r} as retracted/unavailable"
                )
            if claim is not None and not _has_retraction_marker(claim.statement):
                raise LLMValidationError(
                    f"claim statement for retracted slot {slot!r} must state retraction"
                )
            if is_changed:
                changed_total += 1
                changed_updated += 1
            else:
                unchanged_total += 1
                unchanged_preserved += 1

        if (
            old_fact is not None
            and old_fact.value is not None
            and old_fact.value != cur_fact.value
            and old_fact.value.strip().lower() not in active_terms
            and contains_term(summary_text, old_fact.value)
        ):
            raise LLMValidationError(
                f"summary_text retained superseded value {old_fact.value!r} for slot {slot!r}"
            )

    claims_ordered = tuple(claims_by_slot[slot] for slot in sorted(claims_by_slot))
    fidelity = {
        "unchanged_preserved_ratio": (
            unchanged_preserved / unchanged_total if unchanged_total else 1.0
        ),
        "changed_updated_ratio": (
            changed_updated / changed_total if changed_total else 1.0
        ),
        "citation_exact_match": 1.0,
    }
    return ValidatedRepairOutput(
        summary_text=summary_text,
        claims=claims_ordered,
        fidelity=fidelity,
    )


def validate_reader_output(
    raw_text: str,
    allowed_source_refs: set[str],
) -> ValidatedReaderOutput:
    """Validate frozen Answer Reader JSON output and citation bounds."""
    parsed = extract_json_object(raw_text, error_cls=LLMValidationError)
    answer = parsed.get("answer")
    abstained = parsed.get("abstained")
    cited_raw = parsed.get("cited_sources")
    if cited_raw is None:
        cited_raw = []

    if not isinstance(answer, str):
        raise LLMValidationError("reader output 'answer' must be a string")
    if not isinstance(abstained, bool):
        raise LLMValidationError("reader output 'abstained' must be a boolean")
    if not isinstance(cited_raw, list) or not all(isinstance(x, str) for x in cited_raw):
        raise LLMValidationError("reader output 'cited_sources' must be a list of strings")

    cited_clean = tuple(dict.fromkeys(ref.strip() for ref in cited_raw if ref.strip()))
    unknown_refs = [ref for ref in cited_clean if ref not in allowed_source_refs]
    if unknown_refs:
        raise LLMValidationError(f"reader cited unknown source_refs: {unknown_refs}")

    answer_clean = answer.strip()
    if answer_clean.lower() in _ABSTENTION_MARKERS:
        abstained = True
        if not answer_clean:
            answer_clean = "INSUFFICIENT_EVIDENCE"

    return ValidatedReaderOutput(
        answer_text=answer_clean,
        abstained=abstained,
        cited_sources=cited_clean,
    )
