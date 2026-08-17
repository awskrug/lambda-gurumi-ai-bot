"""Tests for src.llms.xai."""
from __future__ import annotations

import base64
import io
import json
from unittest.mock import MagicMock, patch

import pytest

from src.llms.xai import XAIProvider


def _openai_completion(content="", tool_calls=None, finish="stop"):
    choice = MagicMock()
    choice.finish_reason = finish
    choice.message.content = content
    choice.message.tool_calls = tool_calls or []
    completion = MagicMock()
    completion.choices = [choice]
    completion.usage.prompt_tokens = 10
    completion.usage.completion_tokens = 20
    return completion


def _openai_tool_call(call_id, name, args_obj):
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args_obj)
    return tc


def test_xai_provider_uses_xai_base_url_and_api_key():
    """XAIProvider must instantiate OpenAI client with the xAI base URL and
    the explicit api_key, so traffic goes to api.x.ai rather than OpenAI."""
    provider = XAIProvider(
        model="grok-4-1-fast-reasoning",
        image_model="grok-imagine-image",
        api_key="xai-test",
    )
    with patch("openai.OpenAI") as openai_ctor:
        openai_ctor.return_value = MagicMock()
        provider._get_client()
    kwargs = openai_ctor.call_args.kwargs
    assert kwargs.get("base_url") == "https://api.x.ai/v1"
    assert kwargs.get("api_key") == "xai-test"


def test_xai_chat_parses_tool_calls():
    """Grok returns the same wire shape as OpenAI for tool calls; the
    shared parser must turn them into ToolCall objects."""
    provider = XAIProvider(model="grok-4-1-fast-reasoning", image_model="grok-imagine-image", api_key="x")
    provider._client = MagicMock()
    tc = _openai_tool_call("call_g1", "search_web", {"query": "xai"})
    provider._client.chat.completions.create.return_value = _openai_completion(
        tool_calls=[tc], finish="tool_calls"
    )
    result = provider.chat(
        system="s",
        messages=[],
        tools=[{"name": "search_web", "description": "", "parameters": {"type": "object"}}],
    )
    assert result.stop_reason == "tool_use"
    assert result.tool_calls[0].name == "search_web"
    assert result.tool_calls[0].arguments == {"query": "xai"}


def test_xai_chat_uses_legacy_max_tokens_always():
    """All current grok chat models accept max_tokens + temperature;
    XAIProvider must not switch to max_completion_tokens (OpenAI-only split)."""
    provider = XAIProvider(model="grok-4.20-0309-reasoning", image_model="grok-imagine-image", api_key="x")
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = _openai_completion(content="hi")
    provider.chat(system="s", messages=[])
    kwargs = provider._client.chat.completions.create.call_args.kwargs
    assert "max_tokens" in kwargs
    assert "temperature" in kwargs
    assert "max_completion_tokens" not in kwargs


def test_xai_generate_image_skips_size_and_requests_b64():
    """xAI images.generate rejects `size` (uses aspect_ratio/resolution).
    We must omit it and explicitly ask for b64_json so we can decode bytes
    into files_upload_v2."""
    provider = XAIProvider(model="grok-4-1-fast-reasoning", image_model="grok-imagine-image", api_key="x")
    provider._client = MagicMock()
    response = MagicMock()
    response.data = [MagicMock(b64_json=base64.b64encode(b"xai-bytes").decode())]
    provider._client.images.generate.return_value = response

    assert provider.generate_image("a cat") == b"xai-bytes"
    kwargs = provider._client.images.generate.call_args.kwargs
    assert kwargs["model"] == "grok-imagine-image"
    assert kwargs["prompt"] == "a cat"
    assert kwargs["response_format"] == "b64_json"
    assert "size" not in kwargs  # xAI rejects this


def test_xai_edit_image_posts_json_with_data_uri():
    """xAI's /v1/images/edits is JSON (not multipart) and the image is
    embedded as a `{url: data:..., type: image_url}` block. We bypass the
    OpenAI SDK because xAI explicitly does not support its images.edit()."""
    provider = XAIProvider(model="grok-4-1-fast-reasoning", image_model="grok-imagine-image", api_key="xai-key")
    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        ctx = MagicMock()
        ctx.__enter__ = lambda self: self
        ctx.__exit__ = lambda *a: False
        ctx.read.return_value = json.dumps(
            {"data": [{"b64_json": base64.b64encode(b"edited").decode()}]}
        ).encode()
        return ctx

    with patch("src.llms.xai.urllib.request.urlopen", side_effect=_fake_urlopen):
        out = provider.edit_image("make it a sketch", [(b"\x89PNG-data", "image/png")])

    assert out == b"edited"
    assert captured["url"] == "https://api.x.ai/v1/images/edits"
    # Auth header lookup is case-insensitive in real Request; header_items() is
    # already lower-keyed by urllib for what we add via constructor.
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lower["authorization"] == "Bearer xai-key"
    assert headers_lower["content-type"] == "application/json"
    body = captured["body"]
    assert body["model"] == "grok-imagine-image"
    assert body["prompt"] == "make it a sketch"
    assert body["response_format"] == "b64_json"
    # Single image -> bare object, not array (matches xAI docs example).
    assert body["image"]["type"] == "image_url"
    assert body["image"]["url"].startswith("data:image/png;base64,")


def test_xai_edit_image_sends_array_when_multiple_images():
    provider = XAIProvider(model="grok-4-1-fast-reasoning", image_model="grok-imagine-image", api_key="x")
    captured: dict = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        ctx = MagicMock()
        ctx.__enter__ = lambda self: self
        ctx.__exit__ = lambda *a: False
        ctx.read.return_value = json.dumps(
            {"data": [{"b64_json": base64.b64encode(b"x").decode()}]}
        ).encode()
        return ctx

    with patch("src.llms.xai.urllib.request.urlopen", side_effect=_fake_urlopen):
        provider.edit_image(
            "merge these",
            [(b"a", "image/png"), (b"b", "image/jpeg")],
        )

    assert isinstance(captured["body"]["image"], list)
    assert len(captured["body"]["image"]) == 2
    assert captured["body"]["image"][1]["url"].startswith("data:image/jpeg;base64,")


def test_xai_edit_image_requires_at_least_one_image():
    provider = XAIProvider(model="grok-4-1-fast-reasoning", image_model="grok-imagine-image", api_key="x")
    with pytest.raises(ValueError, match="at least one input image"):
        provider.edit_image("hi", [])


def test_xai_edit_image_raises_on_http_error():
    """HTTPError from xAI must be wrapped with the response body so the LLM
    can see what went wrong (model-not-allowed, bad prompt, etc.) and recover."""
    import urllib.error

    provider = XAIProvider(model="grok-4-1-fast-reasoning", image_model="grok-imagine-image", api_key="x")

    def _fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", hdrs=None, fp=io.BytesIO(b'{"error":"oops"}')
        )

    with patch("src.llms.xai.urllib.request.urlopen", side_effect=_fake_urlopen):
        with pytest.raises(ValueError, match="HTTP 400"):
            provider.edit_image("x", [(b"a", "image/png")])


def test_xai_stream_chat_emits_deltas():
    provider = XAIProvider(model="grok-4-1-fast-reasoning", image_model="grok-imagine-image", api_key="x")
    provider._client = MagicMock()

    def _chunk(text):
        ch = MagicMock()
        ch.choices[0].delta.content = text
        return ch

    provider._client.chat.completions.create.return_value = iter([_chunk("gr"), _chunk("ok")])
    seen: list[str] = []
    result = provider.stream_chat(system="s", messages=[], on_delta=seen.append)
    assert result == "grok"
    assert seen == ["gr", "ok"]
