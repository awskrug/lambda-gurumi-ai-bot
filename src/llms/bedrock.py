"""BedrockProvider — family-routed across Anthropic Claude, Amazon Nova, Stability."""
from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Any, Callable, Literal

import boto3
from botocore.config import Config

from src.llms.base import LLMResult, ToolCall, ToolSpec, _with_retry

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Bedrock
# --------------------------------------------------------------------------- #


_INFERENCE_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")


def _strip_inference_profile_prefix(model_id: str) -> str:
    """Return the bare family id from a Bedrock model or inference-profile id.

    Inference profile IDs prefix the family with a region routing hint, e.g.
    `us.anthropic.claude-haiku-4-5-20251001-v1:0`. For family-level routing
    ("is this a Claude? a Nova? Titan?") we care about the bare portion.
    """
    for p in _INFERENCE_PROFILE_PREFIXES:
        if model_id.startswith(p):
            return model_id[len(p):]
    return model_id


class BedrockProvider:
    def __init__(self, model: str, image_model: str, region: str):
        self.model = model
        self.image_model = image_model
        self.region = region
        self._client = None

    def _get_client(self):
        if self._client is None:
            # Claude Sonnet/Opus generations regularly run 60-120s server-side.
            # botocore's default read_timeout=60s + legacy retry mode (5 attempts)
            # silently re-invokes the model up to 5x and exhausts Lambda's 300s
            # budget before any exception surfaces (see: 5x AWS/Bedrock
            # Invocations metric for a single user turn). Keep the read_timeout
            # close to the worker Lambda's own timeout, drop retries to standard
            # mode with 2 attempts so a transient throttle still recovers
            # without amplifying read-timeout stalls.
            cfg = Config(
                connect_timeout=10,
                read_timeout=290,
                retries={"max_attempts": 2, "mode": "standard"},
            )
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self.region,
                config=cfg,
            )
        return self._client

    @property
    def _text_family(self) -> str:
        return _strip_inference_profile_prefix(self.model)

    @property
    def _image_family(self) -> str:
        return _strip_inference_profile_prefix(self.image_model)

    # -- text / tool use ---------------------------------------------------- #

    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        on_delta: Callable[[str], None] | None = None,
    ) -> LLMResult:
        # Bedrock tool_use streaming is not yet implemented in this provider;
        # accept the on_delta parameter for API compatibility but use the
        # blocking path, then emit the final content as a single delta so
        # callers still receive *something* through the streaming channel.
        family = self._text_family
        if family.startswith("anthropic.claude"):
            result = self._claude_chat(system, messages, tools, max_tokens)
        elif family.startswith("amazon.nova"):
            result = self._nova_chat(system, messages, tools, max_tokens)
        else:
            result = self._claude_chat(system, messages, None, max_tokens)
        if on_delta is not None and result.content and not result.tool_calls:
            on_delta(result.content)
        return result

    def _claude_chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None,
        max_tokens: int,
    ) -> LLMResult:
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": self._to_anthropic_messages(messages),
        }
        if tools:
            body["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                for t in tools
            ]

        client = self._get_client()
        response = _with_retry(
            lambda: client.invoke_model(modelId=self.model, body=json.dumps(body)),
            label="bedrock.invoke_model",
        )
        payload = json.loads(response["body"].read())

        content_text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in payload.get("content", []):
            if block.get("type") == "text":
                content_text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", str(uuid.uuid4())),
                        name=block.get("name", ""),
                        arguments=block.get("input", {}) or {},
                    )
                )

        stop_reason_raw = payload.get("stop_reason", "end_turn")
        stop_reason: Literal["end_turn", "tool_use", "max_tokens", "other"]
        if stop_reason_raw == "tool_use":
            stop_reason = "tool_use"
        elif stop_reason_raw == "max_tokens":
            stop_reason = "max_tokens"
        elif stop_reason_raw == "end_turn":
            stop_reason = "end_turn"
        else:
            stop_reason = "other"

        usage = payload.get("usage") or {}
        return LLMResult(
            content="".join(content_text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            token_usage={"input": usage.get("input_tokens", 0) or 0, "output": usage.get("output_tokens", 0) or 0},
        )

    def _nova_chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None,
        max_tokens: int,
    ) -> LLMResult:
        client = self._get_client()
        payload: dict[str, Any] = {
            "modelId": self.model,
            "system": [{"text": system}],
            "messages": self._to_nova_messages(messages),
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2},
        }
        if tools:
            payload["toolConfig"] = {
                "tools": [
                    {"toolSpec": {"name": t["name"], "description": t["description"], "inputSchema": {"json": t["parameters"]}}}
                    for t in tools
                ]
            }

        response = _with_retry(lambda: client.converse(**payload), label="bedrock.converse")
        out_msg = response.get("output", {}).get("message", {})
        content_text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in out_msg.get("content", []):
            if "text" in block:
                content_text_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append(
                    ToolCall(id=tu.get("toolUseId") or str(uuid.uuid4()), name=tu.get("name", ""), arguments=tu.get("input", {}) or {})
                )

        stop_reason_raw = response.get("stopReason", "end_turn")
        stop_reason: Literal["end_turn", "tool_use", "max_tokens", "other"]
        if stop_reason_raw == "tool_use":
            stop_reason = "tool_use"
        elif stop_reason_raw == "max_tokens":
            stop_reason = "max_tokens"
        elif stop_reason_raw == "end_turn":
            stop_reason = "end_turn"
        else:
            stop_reason = "other"

        usage = response.get("usage") or {}
        return LLMResult(
            content="".join(content_text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            token_usage={"input": usage.get("inputTokens", 0) or 0, "output": usage.get("outputTokens", 0) or 0},
        )

    # -- streaming --------------------------------------------------------- #

    def stream_chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        on_delta: Callable[[str], None],
        max_tokens: int = 1024,
    ) -> str:
        # Bedrock streaming implementation: Claude Messages stream or Converse stream.
        client = self._get_client()
        full = ""
        family = self._text_family
        if family.startswith("anthropic.claude"):
            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "system": system,
                "messages": self._to_anthropic_messages(messages),
            }
            response = client.invoke_model_with_response_stream(modelId=self.model, body=json.dumps(body))
            for event in response.get("body", []):
                chunk = event.get("chunk", {})
                if not chunk:
                    continue
                payload = json.loads(chunk.get("bytes", b"{}"))
                if payload.get("type") == "content_block_delta":
                    delta = (payload.get("delta") or {}).get("text") or ""
                    if delta:
                        full += delta
                        on_delta(delta)
            return full

        # Nova Converse stream
        response = client.converse_stream(
            modelId=self.model,
            system=[{"text": system}],
            messages=self._to_nova_messages(messages),
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2},
        )
        for event in response.get("stream", []):
            cbd = event.get("contentBlockDelta")
            if cbd:
                delta = (cbd.get("delta") or {}).get("text") or ""
                if delta:
                    full += delta
                    on_delta(delta)
        return full

    # -- vision / image ----------------------------------------------------- #

    _NOVA_IMAGE_FORMATS = {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/gif": "gif",
        "image/webp": "webp",
    }

    def describe_image(self, image_bytes: bytes, mime_type: str) -> str:
        # Family-route the same way chat() does. Nova models speak the Converse
        # API with an `image` content block; sending Claude's Messages body at a
        # Nova ID raises ValidationException. Claude (and unknown families, to
        # preserve prior behaviour for new Claude-compatible IDs) stays on the
        # Messages API.
        if self._text_family.startswith("amazon.nova"):
            return self._nova_describe_image(image_bytes, mime_type)
        return self._claude_describe_image(image_bytes, mime_type)

    def _claude_describe_image(self, image_bytes: bytes, mime_type: str) -> str:
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image for a Slack conversation."},
                        {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": encoded}},
                    ],
                }
            ],
        }
        client = self._get_client()
        response = client.invoke_model(modelId=self.model, body=json.dumps(body))
        payload = json.loads(response["body"].read())
        for block in payload.get("content", []):
            if block.get("type") == "text":
                return block.get("text", "")
        return ""

    def _nova_describe_image(self, image_bytes: bytes, mime_type: str) -> str:
        fmt = self._NOVA_IMAGE_FORMATS.get((mime_type or "").lower(), "png")
        client = self._get_client()
        response = client.converse(
            modelId=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"text": "Describe this image for a Slack conversation."},
                        {"image": {"format": fmt, "source": {"bytes": image_bytes}}},
                    ],
                }
            ],
            inferenceConfig={"maxTokens": 512, "temperature": 0.2},
        )
        for block in response.get("output", {}).get("message", {}).get("content", []):
            if "text" in block:
                return block.get("text", "")
        return ""

    def generate_image(self, prompt: str) -> bytes:
        body = self._build_image_body(prompt)
        client = self._get_client()
        response = client.invoke_model(modelId=self.image_model, body=json.dumps(body))
        payload = json.loads(response["body"].read())
        return self._extract_image_bytes(payload)

    def edit_image(self, prompt: str, images: list[tuple[bytes, str]]) -> bytes:
        # Titan / Nova Canvas / Stability all support image-to-image, but each
        # uses a different request shape (Titan IMAGE_VARIATION, Nova
        # COLOR_GUIDED_GENERATION, Stability init_image). We don't have a
        # tested code path for any of them yet; raise so the executor surfaces
        # a clear "edit not supported on this provider" message instead of
        # silently routing to text-to-image.
        raise NotImplementedError(
            f"edit_image is not supported by Bedrock image model '{self.image_model}'. "
            "Switch IMAGE_PROVIDER to 'openai' or 'xai' for editing."
        )

    def _build_image_body(self, prompt: str) -> dict[str, Any]:
        family = self._image_family
        if family.startswith("amazon.titan-image") or family.startswith("amazon.nova-canvas"):
            return {
                "taskType": "TEXT_IMAGE",
                "textToImageParams": {"text": prompt},
                "imageGenerationConfig": {"numberOfImages": 1, "quality": "standard", "height": 1024, "width": 1024},
            }
        if family.startswith("stability."):
            return {"text_prompts": [{"text": prompt}], "cfg_scale": 7, "steps": 30, "seed": 0}
        raise ValueError(f"unsupported Bedrock image model: {self.image_model}")

    def _extract_image_bytes(self, payload: dict[str, Any]) -> bytes:
        if "images" in payload and payload["images"]:
            return base64.b64decode(payload["images"][0])
        if "artifacts" in payload and payload["artifacts"]:
            return base64.b64decode(payload["artifacts"][0]["base64"])
        raise ValueError("no image returned from Bedrock")

    # -- format helpers ----------------------------------------------------- #

    @staticmethod
    def _to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate our canonical messages format into Anthropic Messages API shape.

        Our format mirrors OpenAI's: role=user/assistant/tool, content can be str
        or list. We map `tool` role to a user message with a tool_result block,
        and `assistant` messages with tool_calls into tool_use content blocks.
        """
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id", ""),
                                "content": msg.get("content", ""),
                            }
                        ],
                    }
                )
            elif role == "assistant" and msg.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    blocks.append(
                        {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc.get("arguments", {})}
                    )
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": role or "user", "content": msg.get("content", "")})
        return out

    @staticmethod
    def _coerce_nova_text(content: Any) -> str:
        """Convert arbitrary content into a string suitable for Nova's
        `{"text": ...}` content block.

        Strings pass through unchanged. Non-strings (dict, list — e.g. if a
        future caller hands us raw tool_result dicts) are JSON-serialized
        instead of going through `str(...)`, which would emit Python repr and
        leave the LLM unable to parse downstream.
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False)

    @staticmethod
    def _to_nova_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "toolResult": {
                                    "toolUseId": msg.get("tool_call_id", ""),
                                    "content": [{"text": BedrockProvider._coerce_nova_text(msg.get("content"))}],
                                }
                            }
                        ],
                    }
                )
            elif role == "assistant" and msg.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    blocks.append({"text": BedrockProvider._coerce_nova_text(msg["content"])})
                for tc in msg["tool_calls"]:
                    blocks.append({"toolUse": {"toolUseId": tc["id"], "name": tc["name"], "input": tc.get("arguments", {})}})
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": role or "user", "content": [{"text": BedrockProvider._coerce_nova_text(msg.get("content"))}]})
        return out
