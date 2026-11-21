"""Local LLM client protocol, 4-bit HuggingFace runtime adapter, and offline test fake.

Inputs: ``LLMConfig`` and ``LLMRequest``.
Outputs: ``LLMResponse`` with generated text, token/timing ``LLMUsage``, and
``runtime_info()`` hardware/library/model metadata. Heavy dependencies
(``torch``, ``transformers``, ``bitsandbytes``) are lazily imported only when
constructing ``HuggingFace4BitClient``.
"""
from __future__ import annotations

import os
import platform
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from importlib.util import find_spec
from math import isfinite
from typing import Any, Protocol

from verify_repair.llm.prompts import PROMPT_VERSION, prompt_bundle_sha256
from verify_repair.models import LLMTimeoutError

DEFAULT_MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"
VALID_BACKENDS = ("hf-4bit", "fake")
VALID_QUANTIZATIONS = ("nf4", "none")
VALID_DTYPES = ("auto", "float16", "bfloat16")


@dataclass(frozen=True)
class LLMConfig:
    """Validated, hashable configuration for local LLM operations and reader."""

    backend: str = "hf-4bit"
    model_id: str = DEFAULT_MODEL_ID
    model_revision: str | None = None
    quantization: str = "nf4"
    compute_dtype: str = "auto"
    do_sample: bool = False
    temperature: float | None = None
    top_p: float | None = None
    verify_max_new_tokens: int = 256
    repair_max_new_tokens: int = 512
    reader_max_new_tokens: int = 128
    timeout_seconds: float = 60.0
    max_retries: int = 1
    seed: int = 12345

    def __post_init__(self) -> None:
        if self.backend not in VALID_BACKENDS:
            raise ValueError(f"llm backend must be one of {VALID_BACKENDS}")
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("llm model_id must be a non-empty string")
        if self.model_revision is not None and (
            not isinstance(self.model_revision, str) or not self.model_revision.strip()
        ):
            raise ValueError("llm model_revision must be None or a non-empty string")
        if self.quantization not in VALID_QUANTIZATIONS:
            raise ValueError(f"llm quantization must be one of {VALID_QUANTIZATIONS}")
        if self.compute_dtype not in VALID_DTYPES:
            raise ValueError(f"llm compute_dtype must be one of {VALID_DTYPES}")
        if not self.do_sample and (self.temperature is not None or self.top_p is not None):
            raise ValueError("temperature and top_p must be None when do_sample is False")
        if self.do_sample:
            if self.temperature is not None and (not isfinite(self.temperature) or self.temperature <= 0):
                raise ValueError("temperature must be finite and positive")
            if self.top_p is not None and (not isfinite(self.top_p) or not 0 < self.top_p <= 1):
                raise ValueError("top_p must lie in (0, 1]")
        for name in ("verify_max_new_tokens", "repair_max_new_tokens", "reader_max_new_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite positive number")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        if type(self.seed) is not int:
            raise ValueError("seed must be an integer")

    def snapshot(self) -> dict[str, Any]:
        """Deterministic config snapshot for ``config_snapshot['llm']`` and hashes."""
        data = asdict(self)
        data["prompt_version"] = PROMPT_VERSION
        data["prompt_bundle_sha256"] = prompt_bundle_sha256()
        return data


@dataclass(frozen=True)
class LLMUsage:
    """Per-call token counts and optional timing measurements (Step 6.1 ready)."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    wall_ms: float | None = None
    gpu_ms: float | None = None


@dataclass(frozen=True)
class LLMRequest:
    """One structured prompt request to a local LLM backend."""

    role: str
    system_prompt: str
    user_prompt: str
    max_new_tokens: int
    timeout_seconds: float
    prompt_id: str
    prompt_version: str


@dataclass(frozen=True)
class LLMResponse:
    """Raw text response plus usage and provenance metadata from an LLM backend."""

    text: str
    usage: LLMUsage
    model_id: str
    model_revision: str | None
    quantization: str
    finish_reason: str = "stop"


class LLMClient(Protocol):
    config: LLMConfig

    def runtime_info(self) -> dict[str, Any]: ...

    def generate(self, request: LLMRequest) -> LLMResponse: ...


def _env_offline() -> bool:
    return (
        os.getenv("HF_HUB_OFFLINE") == "1"
        or os.getenv("TRANSFORMERS_OFFLINE") == "1"
    )


def _resolve_offline_source(
    model_id: str,
    model_revision: str | None,
    *,
    local_only: bool,
    hub_cache_dir: str | os.PathLike[str] | None = None,
) -> tuple[str, str | None]:
    """Resolve a cached local HF snapshot directory when running offline.

    Returns ``(path_or_repo_id, resolved_revision)``. Passing the concrete local
    snapshot directory directly avoids ``huggingface_hub`` ``refs/main`` lookups
    when a model was prefetched by commit SHA.
    """
    if not local_only or os.path.isdir(model_id):
        return model_id, model_revision

    from pathlib import Path

    if hub_cache_dir is not None:
        hub_cache = Path(hub_cache_dir)
    elif os.getenv("HF_HUB_CACHE"):
        hub_cache = Path(os.environ["HF_HUB_CACHE"])
    elif os.getenv("HF_HOME"):
        hub_cache = Path(os.environ["HF_HOME"]) / "hub"
    else:
        hub_cache = Path.home() / ".cache" / "huggingface" / "hub"

    repo_cache = hub_cache / ("models--" + model_id.replace("/", "--"))
    snapshots_dir = repo_cache / "snapshots"
    if not snapshots_dir.is_dir():
        return model_id, model_revision

    if model_revision is not None:
        direct = snapshots_dir / model_revision
        if direct.is_dir():
            return str(direct), model_revision
        ref_file = repo_cache / "refs" / model_revision
        if ref_file.is_file():
            sha = ref_file.read_text(encoding="utf-8").strip()
            target = snapshots_dir / sha
            if sha and target.is_dir():
                return str(target), sha
        return model_id, model_revision

    refs_main = repo_cache / "refs" / "main"
    if refs_main.is_file():
        sha = refs_main.read_text(encoding="utf-8").strip()
        target = snapshots_dir / sha
        if sha and target.is_dir():
            return str(target), sha

    snapshots = sorted(p for p in snapshots_dir.iterdir() if p.is_dir())
    if len(snapshots) == 1:
        return str(snapshots[0]), snapshots[0].name
    return model_id, model_revision


class HuggingFace4BitClient:
    """Local HuggingFace ``transformers`` + ``bitsandbytes`` 4-bit NF4 adapter.

    Lazily imports ``torch``, ``transformers``, ``accelerate``, and
    ``bitsandbytes`` on construction only. Respects ``HF_HUB_OFFLINE=1`` and
    ``TRANSFORMERS_OFFLINE=1`` for offline Zaratan compute nodes.
    """

    def __init__(self, config: LLMConfig = LLMConfig()):
        if config.backend != "hf-4bit":
            raise ValueError(f"HuggingFace4BitClient requires backend='hf-4bit', got {config.backend!r}")
        needed = ["torch", "transformers", "accelerate"]
        if config.quantization == "nf4":
            needed.append("bitsandbytes")
        missing = [pkg for pkg in needed if find_spec(pkg) is None]
        if missing:
            raise ImportError(
                f"missing optional LLM dependencies {missing}; install verify-repair[llm]"
            )

        import accelerate
        import torch
        import transformers
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

        self.config = config
        self._torch = torch
        self._transformers = transformers
        local_only = _env_offline()

        if config.compute_dtype == "bfloat16":
            compute_dtype = torch.bfloat16
            resolved_dtype = "bfloat16"
        elif config.compute_dtype == "float16":
            compute_dtype = torch.float16
            resolved_dtype = "float16"
        else:
            if torch.cuda.is_available() and torch.cuda.is_bf16_supported():
                compute_dtype = torch.bfloat16
                resolved_dtype = "bfloat16"
            else:
                compute_dtype = torch.float16
                resolved_dtype = "float16"

        self._resolved_dtype = resolved_dtype

        resolved_model_arg, effective_revision = _resolve_offline_source(
            config.model_id,
            config.model_revision,
            local_only=local_only,
        )

        tokenizer_kwargs: dict[str, Any] = {"local_files_only": local_only}
        if effective_revision is not None and not os.path.isdir(resolved_model_arg):
            tokenizer_kwargs["revision"] = effective_revision
        self._tokenizer = AutoTokenizer.from_pretrained(resolved_model_arg, **tokenizer_kwargs)
        if self._tokenizer.pad_token_id is None and self._tokenizer.eos_token_id is not None:
            self._tokenizer.pad_token_id = self._tokenizer.eos_token_id

        model_kwargs: dict[str, Any] = {
            "device_map": "auto",
            "local_files_only": local_only,
        }
        if effective_revision is not None and not os.path.isdir(resolved_model_arg):
            model_kwargs["revision"] = effective_revision
        if config.quantization == "nf4":
            import bitsandbytes

            self._bnb_version: str | None = getattr(bitsandbytes, "__version__", None)
            model_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=compute_dtype,
            )
        else:
            self._bnb_version = None
            model_kwargs["torch_dtype"] = compute_dtype if torch.cuda.is_available() else torch.float32

        self._model = AutoModelForCausalLM.from_pretrained(resolved_model_arg, **model_kwargs)
        self._model.eval()

        resolved_revision = effective_revision or getattr(
            getattr(self._model, "config", None), "_commit_hash", None
        )
        self._resolved_revision: str | None = resolved_revision

        gpu_name = None
        gpu_vram_gb = None
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            props = torch.cuda.get_device_properties(0)
            gpu_vram_gb = round(props.total_memory / (1024 ** 3), 2)

        self._runtime_info: dict[str, Any] = {
            "backend": config.backend,
            "model_id": config.model_id,
            "model_revision": resolved_revision,
            "quantization": config.quantization,
            "compute_dtype": resolved_dtype,
            "do_sample": config.do_sample,
            "temperature": config.temperature,
            "top_p": config.top_p,
            "prompt_version": PROMPT_VERSION,
            "prompt_bundle_sha256": prompt_bundle_sha256(),
            "offline": local_only,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "torch_version": getattr(torch, "__version__", None),
            "cuda_version": getattr(torch.version, "cuda", None),
            "transformers_version": getattr(transformers, "__version__", None),
            "accelerate_version": getattr(accelerate, "__version__", None),
            "bitsandbytes_version": self._bnb_version,
            "cuda_available": torch.cuda.is_available(),
            "gpu_name": gpu_name,
            "gpu_vram_gb": gpu_vram_gb,
        }

    def runtime_info(self) -> dict[str, Any]:
        return dict(self._runtime_info)

    def generate(self, request: LLMRequest) -> LLMResponse:
        torch = self._torch
        StoppingCriteria = self._transformers.StoppingCriteria
        StoppingCriteriaList = self._transformers.StoppingCriteriaList

        messages = [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_prompt},
        ]
        prompt_text = self._tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        encoded = self._tokenizer(prompt_text, return_tensors="pt")
        device = next(self._model.parameters()).device
        encoded = {key: value.to(device) for key, value in encoded.items()}
        prompt_tokens = int(encoded["input_ids"].shape[-1])

        deadline = time.monotonic() + request.timeout_seconds
        timed_out = False

        class _DeadlineCriteria(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs) -> bool:
                nonlocal timed_out
                if time.monotonic() >= deadline:
                    timed_out = True
                    return True
                return False

        gen_kwargs: dict[str, Any] = {
            "max_new_tokens": request.max_new_tokens,
            "do_sample": self.config.do_sample,
            "pad_token_id": self._tokenizer.pad_token_id,
            "eos_token_id": self._tokenizer.eos_token_id,
            "stopping_criteria": StoppingCriteriaList([_DeadlineCriteria()]),
        }
        if self.config.do_sample:
            if self.config.temperature is not None:
                gen_kwargs["temperature"] = self.config.temperature
            if self.config.top_p is not None:
                gen_kwargs["top_p"] = self.config.top_p
        else:
            # Avoid transformers warnings when model generation_config has sampling defaults.
            gen_kwargs["temperature"] = None
            gen_kwargs["top_p"] = None
            gen_kwargs["top_k"] = None

        use_cuda_timing = torch.cuda.is_available() and device.type == "cuda"
        start_event = torch.cuda.Event(enable_timing=True) if use_cuda_timing else None
        end_event = torch.cuda.Event(enable_timing=True) if use_cuda_timing else None

        wall_start = time.perf_counter()
        if start_event is not None:
            start_event.record()
        with torch.inference_mode():
            output_ids = self._model.generate(**encoded, **gen_kwargs)
        if end_event is not None:
            end_event.record()
            torch.cuda.synchronize()
        wall_ms = (time.perf_counter() - wall_start) * 1000.0
        gpu_ms = float(start_event.elapsed_time(end_event)) if (start_event and end_event) else None

        if timed_out:
            raise LLMTimeoutError(
                f"LLM generation for role={request.role} exceeded timeout of "
                f"{request.timeout_seconds:g}s"
            )

        generated_ids = output_ids[0][prompt_tokens:]
        completion_tokens = int(generated_ids.shape[-1])
        text = self._tokenizer.decode(generated_ids, skip_special_tokens=True).strip()
        finish_reason = "length" if completion_tokens >= request.max_new_tokens else "stop"
        return LLMResponse(
            text=text,
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                wall_ms=wall_ms,
                gpu_ms=gpu_ms,
            ),
            model_id=self.config.model_id,
            model_revision=self._resolved_revision,
            quantization=self.config.quantization,
            finish_reason=finish_reason,
        )


class FakeLLMClient:
    """Deterministic offline LLM test double supporting queued outputs or handlers."""

    def __init__(
        self,
        responses: Iterable[str | LLMResponse | BaseException] | None = None,
        *,
        handler: Callable[[LLMRequest], str | LLMResponse] | None = None,
        config: LLMConfig | None = None,
    ):
        if responses is not None and handler is not None:
            raise ValueError("specify either responses or handler, not both")
        if responses is None and handler is None:
            raise ValueError("FakeLLMClient requires either responses or handler")
        self.config = config or LLMConfig(
            backend="fake",
            model_id="fake-local-llm",
            model_revision="fake-rev-1",
            quantization="nf4",
            compute_dtype="bfloat16",
        )
        self._queue = list(responses) if responses is not None else None
        self._handler = handler
        self.requests: list[LLMRequest] = []

    def runtime_info(self) -> dict[str, Any]:
        return {
            "backend": self.config.backend,
            "model_id": self.config.model_id,
            "model_revision": self.config.model_revision,
            "quantization": self.config.quantization,
            "compute_dtype": self.config.compute_dtype,
            "prompt_version": PROMPT_VERSION,
            "prompt_bundle_sha256": prompt_bundle_sha256(),
            "offline": True,
        }

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._handler is not None:
            outcome: str | LLMResponse | BaseException = self._handler(request)
        else:
            if not self._queue:
                raise RuntimeError(f"FakeLLMClient ran out of scripted responses on role={request.role}")
            outcome = self._queue.pop(0)

        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, LLMResponse):
            return outcome

        prompt_tokens = max(1, len((request.system_prompt + " " + request.user_prompt).split()))
        completion_tokens = max(1, len(outcome.split()))
        return LLMResponse(
            text=outcome,
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                wall_ms=None,
                gpu_ms=None,
            ),
            model_id=self.config.model_id,
            model_revision=self.config.model_revision,
            quantization=self.config.quantization,
            finish_reason="stop",
        )
