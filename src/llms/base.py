"""Shared protocol, dataclasses, and retry helper for LLM providers."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #

ToolSpec = dict[str, Any]  # {"name","description","parameters"(JSON Schema)}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "other"] = "end_turn"
    token_usage: dict[str, int] = field(default_factory=dict)


class LLMProvider(Protocol):
    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        on_delta: Callable[[str], None] | None = None,
    ) -> LLMResult: ...

    def stream_chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        on_delta: Callable[[str], None],
        max_tokens: int = 1024,
    ) -> str: ...

    def describe_image(self, image_bytes: bytes, mime_type: str) -> str: ...

    def generate_image(self, prompt: str) -> bytes: ...

    def edit_image(
        self,
        prompt: str,
        images: list[tuple[bytes, str]],
    ) -> bytes: ...


# --------------------------------------------------------------------------- #
# Retry helper
# --------------------------------------------------------------------------- #

_RETRYABLE_BEDROCK = {"ThrottlingException", "ServiceQuotaExceededException", "ModelTimeoutException"}


def _with_retry(fn: Callable[[], Any], label: str, attempts: int = 3) -> Any:
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            code = getattr(getattr(exc, "response", None), "get", lambda _k, _d=None: None)("Error", {}).get("Code") if hasattr(exc, "response") else None
            if code in _RETRYABLE_BEDROCK and attempt < attempts - 1:
                logger.warning("%s retryable (%s), backoff %.1fs", label, code, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise
    if last_exc:
        raise last_exc
