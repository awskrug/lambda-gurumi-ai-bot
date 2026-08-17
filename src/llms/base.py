"""Shared protocol, dataclasses, and retry helper for LLM providers."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from botocore.exceptions import ClientError

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
    """Retry only Bedrock-style throttle/quota/timeout codes.

    Anything else (4xx misconfig, model not found, transport errors that
    aren't ClientError) re-raises on the first hit — retrying those just
    burns Lambda budget for no recovery.
    """
    delay = 1.0
    for attempt in range(attempts):
        try:
            return fn()
        except ClientError as exc:
            response = getattr(exc, "response", None)
            code = (
                response.get("Error", {}).get("Code")
                if isinstance(response, dict)
                else None
            )
            if code in _RETRYABLE_BEDROCK and attempt < attempts - 1:
                logger.warning("%s retryable (%s), backoff %.1fs", label, code, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise
    # Unreachable: every loop iteration either returns, continues, or raises.
    raise RuntimeError(f"{label}: exhausted retries without raising")
