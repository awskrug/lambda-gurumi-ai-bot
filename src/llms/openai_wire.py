"""OpenAI-wire helpers and the OpenAICompat base provider.

OpenAI and xAI both speak the OpenAI chat-completions wire format, so their
providers share this module. BedrockProvider does NOT use any of these.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import time
import uuid
from typing import Any, Callable, Literal

from src.llms.base import LLMResult, ToolCall, ToolSpec

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# OpenAI
# --------------------------------------------------------------------------- #


_OPENAI_NEW_GENERATION_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _is_new_gen_openai(model: str) -> bool:
    """Newer OpenAI models (gpt-5, o1/o3/o4 reasoning) use `max_completion_tokens`
    and disallow `temperature` overrides."""
    return any(model.startswith(p) for p in _OPENAI_NEW_GENERATION_PREFIXES)


_MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def _image_filename(index: int, mime: str) -> str:
    """Filename hint for multipart upload — OpenAI uses it for content-type detection."""
    ext = _MIME_TO_EXT.get((mime or "").lower(), "png")
    return f"image_{index}.{ext}"


# --------------------------------------------------------------------------- #
# Module-level helpers shared between OpenAI-compatible providers (OpenAI, xAI)
# --------------------------------------------------------------------------- #


def _to_openai_wire_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate canonical messages (our agent's shape) to OpenAI's wire shape."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False),
                            },
                        }
                        for tc in msg["tool_calls"]
                    ],
                }
            )
        else:
            out.append(msg)
    return out


def _build_openai_tools_payload(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ]


def _map_openai_finish_reason(finish: str | None) -> Literal["end_turn", "tool_use", "max_tokens", "other"]:
    if finish == "tool_calls":
        return "tool_use"
    if finish == "length":
        return "max_tokens"
    if finish in {"stop", None}:
        return "end_turn"
    return "other"


def _extract_openai_usage(usage_obj) -> dict[str, int]:
    if not usage_obj:
        return {}
    return {
        "input": getattr(usage_obj, "prompt_tokens", 0) or 0,
        "output": getattr(usage_obj, "completion_tokens", 0) or 0,
    }


def _parse_openai_completion(completion) -> LLMResult:
    choice = completion.choices[0]
    msg = choice.message
    tool_calls: list[ToolCall] = []
    for call in (msg.tool_calls or []):
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append(ToolCall(id=call.id, name=call.function.name, arguments=args))

    return LLMResult(
        content=msg.content or "",
        tool_calls=tool_calls,
        stop_reason=_map_openai_finish_reason(choice.finish_reason),
        token_usage=_extract_openai_usage(getattr(completion, "usage", None)),
    )


def _consume_openai_stream(stream, on_delta: Callable[[str], None]) -> LLMResult:
    """Drain an OpenAI-compatible chat completion stream.

    Stops forwarding content to `on_delta` once a tool_calls delta arrives —
    any trailing commentary would otherwise leak into the final user reply.
    tool_calls chunks are accumulated by index and returned as ToolCall list.
    """
    content_parts: list[str] = []
    tool_calls_accum: dict[int, dict[str, Any]] = {}
    saw_tool_calls = False
    finish_reason: str | None = None
    usage_obj = None

    for chunk in stream:
        usage_obj = getattr(chunk, "usage", None) or usage_obj
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        if getattr(delta, "tool_calls", None):
            saw_tool_calls = True
            for tc in delta.tool_calls:
                idx = tc.index
                slot = tool_calls_accum.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] += fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments
        if getattr(delta, "content", None):
            content_parts.append(delta.content)
            if not saw_tool_calls:
                on_delta(delta.content)
        if getattr(choice, "finish_reason", None):
            finish_reason = choice.finish_reason

    tool_calls: list[ToolCall] = []
    for idx in sorted(tool_calls_accum):
        slot = tool_calls_accum[idx]
        try:
            args = json.loads(slot["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append(ToolCall(id=slot["id"] or "", name=slot["name"], arguments=args))

    return LLMResult(
        content="".join(content_parts),
        tool_calls=tool_calls,
        stop_reason=_map_openai_finish_reason(finish_reason),
        token_usage=_extract_openai_usage(usage_obj),
    )


class _OpenAICompatProvider:
    """Shared machinery for any OpenAI-wire-compatible chat/vision/image API.

    Subclasses set BASE_URL / API_KEY_ENV_VAR and override small hooks
    (`_token_params`, `_image_generate_kwargs`). The heavy lifting —
    payload assembly, streaming, tool_calls parsing — lives on this base
    and on the module-level helpers above.
    """

    BASE_URL: str | None = None  # None = OpenAI default
    API_KEY_ENV_VAR: str = "OPENAI_API_KEY"

    def __init__(self, model: str, image_model: str, api_key: str | None = None):
        self.model = model
        self.image_model = image_model
        self._api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            kwargs: dict[str, Any] = {}
            if self.BASE_URL:
                kwargs["base_url"] = self.BASE_URL
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = OpenAI(**kwargs)
        return self._client

    # -- hooks -------------------------------------------------------------- #

    def _token_params(self, max_tokens: int) -> dict[str, Any]:
        """Default: OpenAI legacy models use max_tokens+temperature."""
        return {"max_tokens": max_tokens, "temperature": 0.2}

    def _image_generate_kwargs(self, prompt: str) -> dict[str, Any]:
        """Default OpenAI (dall-e / gpt-image-1) image call kwargs."""
        kwargs: dict[str, Any] = {
            "model": self.image_model,
            "prompt": prompt,
            "size": "1024x1024",
        }
        # gpt-image-1 rejects `response_format` (b64 is the default); only legacy
        # DALL-E models need the explicit flag.
        if self.image_model.startswith("dall-e"):
            kwargs["response_format"] = "b64_json"
        return kwargs

    def _image_edit_kwargs(self, prompt: str) -> dict[str, Any]:
        """Default OpenAI image edit call kwargs (image= filled by edit_image)."""
        kwargs: dict[str, Any] = {
            "model": self.image_model,
            "prompt": prompt,
        }
        # Mirror generate(): gpt-image-1 rejects response_format, dall-e-2 needs it.
        if self.image_model.startswith("dall-e"):
            kwargs["response_format"] = "b64_json"
            kwargs["size"] = "1024x1024"
        return kwargs

    # -- LLMProvider surface ----------------------------------------------- #

    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        on_delta: Callable[[str], None] | None = None,
    ) -> LLMResult:
        client = self._get_client()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *_to_openai_wire_messages(messages)],
            **self._token_params(max_tokens),
        }
        if tools:
            payload["tools"] = _build_openai_tools_payload(tools)
            payload["tool_choice"] = "auto"

        if on_delta is None:
            completion = client.chat.completions.create(**payload)
            return _parse_openai_completion(completion)

        payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        stream = client.chat.completions.create(**payload)
        return _consume_openai_stream(stream, on_delta)

    def stream_chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        on_delta: Callable[[str], None],
        max_tokens: int = 1024,
    ) -> str:
        client = self._get_client()
        stream = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *_to_openai_wire_messages(messages)],
            stream=True,
            **self._token_params(max_tokens),
        )
        full = ""
        for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                full += delta
                on_delta(delta)
        return full

    def describe_image(self, image_bytes: bytes, mime_type: str) -> str:
        client = self._get_client()
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        completion = client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image for a Slack conversation."},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
                    ],
                }
            ],
        )
        return completion.choices[0].message.content or ""

    def generate_image(self, prompt: str) -> bytes:
        client = self._get_client()
        response = client.images.generate(**self._image_generate_kwargs(prompt))
        return base64.b64decode(response.data[0].b64_json)

    def edit_image(self, prompt: str, images: list[tuple[bytes, str]]) -> bytes:
        if not images:
            raise ValueError("edit_image requires at least one input image")
        client = self._get_client()
        kwargs = self._image_edit_kwargs(prompt)
        # OpenAI multipart `image` accepts a tuple (filename, bytes_or_fileobj,
        # content_type). gpt-image-1 supports a list of these (multi-image
        # composition); dall-e-2 only takes a single PNG with alpha channel.
        files = [
            (_image_filename(idx, mime), io.BytesIO(data), mime)
            for idx, (data, mime) in enumerate(images)
        ]
        kwargs["image"] = files if len(files) > 1 else files[0]
        response = client.images.edit(**kwargs)
        return base64.b64decode(response.data[0].b64_json)
