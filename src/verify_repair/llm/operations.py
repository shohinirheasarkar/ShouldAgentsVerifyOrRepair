"""Opt-in LLM VERIFY reasoner and LLM REPAIR summary builder.

Inputs: authoritative ``FactVersion`` records, stored summary text, and an
``LLMClient``. Outputs: validated ``LLMVerifyOutput`` (without mutating stored
state) or ``RepairBuildOutput`` (exact source-linked claims preserving unrelated
correct content). Retries validation errors up to ``client.config.max_retries``
and fails loudly on timeouts or unrecoverable validation failures.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

from verify_repair.llm.client import LLMClient, LLMRequest, LLMUsage
from verify_repair.llm.prompts import (
    PROMPT_VERSION,
    REPAIR_PROMPT_ID,
    VERIFY_PROMPT_ID,
    build_repair_prompt,
    build_verify_prompt,
)
from verify_repair.llm.validation import validate_repair_output, validate_verify_output
from verify_repair.maintenance.summary_builder import (
    RepairBuildOutput,
    RepairBuildRequest,
)
from verify_repair.models import (
    FactVersion,
    LLMTimeoutError,
    LLMValidationError,
    LLMVerificationError,
    RepairBuildError,
)


@dataclass(frozen=True)
class LLMVerifyOutput:
    """Validated query-scoped verified evidence produced by LLM VERIFY."""

    verified_evidence: str
    cited_sources: tuple[str, ...]
    retracted_slots: tuple[str, ...]
    llm_metadata: dict[str, Any]


class VerifyReasoner(Protocol):
    def verify(
        self,
        summary_id: str,
        query_text: str,
        stored_summary_text: str,
        old_facts: dict[str, FactVersion],
        current_facts: dict[str, FactVersion],
        changed_slots: tuple[str, ...],
    ) -> LLMVerifyOutput: ...


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


class LLMVerifier:
    """Query-local LLM verification against authoritative current facts."""

    def __init__(self, client: LLMClient):
        self.client = client

    def verify(
        self,
        summary_id: str,
        query_text: str,
        stored_summary_text: str,
        old_facts: dict[str, FactVersion],
        current_facts: dict[str, FactVersion],
        changed_slots: tuple[str, ...],
    ) -> LLMVerifyOutput:
        feedback: str | None = None
        usages: list[LLMUsage] = []
        last_error: LLMVerificationError | None = None
        max_attempts = self.client.config.max_retries + 1

        for attempt in range(1, max_attempts + 1):
            system_prompt, user_prompt = build_verify_prompt(
                summary_id=summary_id,
                query_text=query_text,
                stored_summary_text=stored_summary_text,
                old_facts=old_facts,
                current_facts=current_facts,
                changed_slots=changed_slots,
                validation_feedback=feedback,
            )
            request = LLMRequest(
                role="verify",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_new_tokens=self.client.config.verify_max_new_tokens,
                timeout_seconds=self.client.config.timeout_seconds,
                prompt_id=VERIFY_PROMPT_ID,
                prompt_version=PROMPT_VERSION,
            )
            try:
                response = self.client.generate(request)
            except LLMTimeoutError:
                raise
            usages.append(response.usage)
            try:
                validated = validate_verify_output(
                    response.text, old_facts, current_facts, changed_slots
                )
            except LLMVerificationError as exc:
                last_error = exc
                feedback = str(exc)
                continue

            return LLMVerifyOutput(
                verified_evidence=validated.verified_evidence,
                cited_sources=validated.cited_sources,
                retracted_slots=validated.retracted_slots,
                llm_metadata={
                    "role": "verify",
                    "prompt_id": VERIFY_PROMPT_ID,
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
        raise LLMVerificationError(
            f"LLM VERIFY failed validation after {max_attempts} attempt(s): {last_error}"
        ) from last_error


class LLMSummaryBuilder:
    """Contextual LLM summary builder with strict claim/citation validation."""

    def __init__(self, client: LLMClient):
        self.client = client

    def build(self, template: str, current_facts: dict[str, str | None]) -> str:
        raise RepairBuildError(
            "LLMSummaryBuilder requires build_repair(RepairBuildRequest) with exact fact provenance"
        )

    def build_repair(self, request: RepairBuildRequest) -> RepairBuildOutput:
        feedback: str | None = None
        usages: list[LLMUsage] = []
        last_error: LLMValidationError | None = None
        max_attempts = self.client.config.max_retries + 1
        # Scale token budget for wide synthetic-v2 summaries (up to 20 facts) while capping cleanly.
        effective_max_tokens = min(
            1024,
            max(self.client.config.repair_max_new_tokens, 128 + 36 * len(request.current_facts)),
        )

        for attempt in range(1, max_attempts + 1):
            system_prompt, user_prompt = build_repair_prompt(
                request,
                validation_feedback=feedback,
            )
            llm_request = LLMRequest(
                role="repair",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_new_tokens=effective_max_tokens,
                timeout_seconds=self.client.config.timeout_seconds,
                prompt_id=REPAIR_PROMPT_ID,
                prompt_version=PROMPT_VERSION,
            )
            try:
                response = self.client.generate(llm_request)
            except LLMTimeoutError:
                raise
            usages.append(response.usage)
            try:
                validated = validate_repair_output(response.text, request)
            except LLMValidationError as exc:
                last_error = exc
                feedback = str(exc)
                continue

            return RepairBuildOutput(
                text=validated.summary_text,
                claims=validated.claims,
                fidelity=validated.fidelity,
                llm_metadata={
                    "role": "repair",
                    "prompt_id": REPAIR_PROMPT_ID,
                    "prompt_version": PROMPT_VERSION,
                    "model_id": response.model_id,
                    "model_revision": response.model_revision,
                    "quantization": response.quantization,
                    "attempts": attempt,
                    "finish_reason": response.finish_reason,
                    "max_new_tokens": effective_max_tokens,
                    "usage": _sum_usage(usages),
                    "claims": [asdict(claim) for claim in validated.claims],
                    "fidelity": dict(validated.fidelity),
                },
            )

        assert last_error is not None
        raise LLMValidationError(
            f"LLM REPAIR failed validation after {max_attempts} attempt(s): {last_error}"
        ) from last_error
