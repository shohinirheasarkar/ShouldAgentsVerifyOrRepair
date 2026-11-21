"""Optional local LLM adapters, prompts, validators, and fixed Answer Reader.

Importing this package does not import ``torch``, ``transformers``, or
``bitsandbytes``; heavy dependencies are loaded lazily inside
``HuggingFace4BitClient``.
"""
from verify_repair.llm.client import (
    DEFAULT_MODEL_ID,
    FakeLLMClient,
    HuggingFace4BitClient,
    LLMClient,
    LLMConfig,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)
from verify_repair.llm.prompts import (
    PROMPT_VERSION,
    READER_PROMPT_ID,
    REPAIR_PROMPT_ID,
    VERIFY_PROMPT_ID,
    format_source_ref,
    prompt_bundle_sha256,
)

__all__ = [
    "DEFAULT_MODEL_ID",
    "FakeLLMClient",
    "HuggingFace4BitClient",
    "LLMClient",
    "LLMConfig",
    "LLMRequest",
    "LLMResponse",
    "LLMUsage",
    "PROMPT_VERSION",
    "READER_PROMPT_ID",
    "REPAIR_PROMPT_ID",
    "VERIFY_PROMPT_ID",
    "format_source_ref",
    "prompt_bundle_sha256",
]
