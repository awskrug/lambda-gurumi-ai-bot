"""Tests for src.llms.upstage."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.llms.upstage import UpstageProvider


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


def test_upstage_provider_uses_upstage_base_url_and_api_key():
    """UpstageProvider must instantiate the OpenAI client with the Upstage
    base URL and explicit api_key, so traffic goes to api.upstage.ai."""
    provider = UpstageProvider(
        model="solar-pro2",
        image_model="",
        api_key="up-test",
    )
    with patch("openai.OpenAI") as openai_ctor:
        openai_ctor.return_value = MagicMock()
        provider._get_client()
    kwargs = openai_ctor.call_args.kwargs
    assert kwargs.get("base_url") == "https://api.upstage.ai/v1"
    assert kwargs.get("api_key") == "up-test"


def test_upstage_chat_parses_tool_calls():
    """Solar returns the same wire shape as OpenAI for tool calls; the shared
    parser must turn them into ToolCall objects."""
    provider = UpstageProvider(model="solar-pro2", image_model="", api_key="x")
    provider._client = MagicMock()
    tc = _openai_tool_call("call_u1", "search_web", {"query": "solar"})
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
    assert result.tool_calls[0].arguments == {"query": "solar"}


def test_upstage_chat_uses_legacy_max_tokens():
    """All current Solar chat models accept max_tokens + temperature;
    UpstageProvider must not switch to max_completion_tokens (OpenAI-only)."""
    provider = UpstageProvider(model="solar-pro2", image_model="", api_key="x")
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = _openai_completion(content="hi")
    provider.chat(system="s", messages=[])
    kwargs = provider._client.chat.completions.create.call_args.kwargs
    assert "max_tokens" in kwargs
    assert "temperature" in kwargs
    assert "max_completion_tokens" not in kwargs


def test_upstage_generate_image_raises():
    """Upstage has no image generation endpoint; misrouting there (e.g.
    IMAGE_PROVIDER falling back to LLM_PROVIDER=upstage) must fail with a
    clear message rather than an opaque SDK error."""
    provider = UpstageProvider(model="solar-pro2", image_model="", api_key="x")
    with pytest.raises(NotImplementedError, match="not supported by Upstage"):
        provider.generate_image("a cat")


def test_upstage_edit_image_raises():
    provider = UpstageProvider(model="solar-pro2", image_model="", api_key="x")
    with pytest.raises(NotImplementedError, match="not supported by Upstage"):
        provider.edit_image("make it a sketch", [(b"\x89PNG", "image/png")])
