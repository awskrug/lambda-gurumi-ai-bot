"""Tests for src.llms.bedrock."""
from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from src.llms.bedrock import BedrockProvider


def _bedrock_response(payload: dict):
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode()
    return {"body": body}


def test_bedrock_inference_profile_routes_to_claude():
    """us.anthropic.claude-* inference profile IDs must still hit the Claude
    path (tool_use, messages API), not the unknown-family fallback."""
    provider = BedrockProvider(
        model="us.anthropic.claude-opus-4-6-v1",
        image_model="amazon.nova-canvas-v1:0",
        region="us-east-1",
    )
    provider._client = MagicMock()
    provider._client.invoke_model.return_value = _bedrock_response(
        {
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
    )
    tools = [{"name": "search_web", "description": "", "parameters": {"type": "object"}}]
    result = provider.chat(system="s", messages=[], tools=tools)
    assert result.content == "hi"
    # Claude body carries tools (not Nova's toolConfig)
    body = provider._client.invoke_model.call_args.kwargs["body"]
    parsed = json.loads(body)
    assert "tools" in parsed  # routed into _claude_chat, not fallback
    assert parsed["tools"][0]["name"] == "search_web"


def test_bedrock_inference_profile_image_routing():
    """global./us. prefixed image model IDs still reach the Titan/Nova body."""
    provider = BedrockProvider(
        model="us.anthropic.claude-opus-4-6-v1",
        image_model="us.amazon.nova-canvas-v1:0",
        region="us-east-1",
    )
    body = provider._build_image_body("cat")
    assert body["taskType"] == "TEXT_IMAGE"


def test_bedrock_claude_chat_text():
    provider = BedrockProvider(
        model="anthropic.claude-3-5-sonnet-20240620-v1:0",
        image_model="amazon.titan-image-generator-v1",
        region="us-east-1",
    )
    provider._client = MagicMock()
    provider._client.invoke_model.return_value = _bedrock_response(
        {
            "content": [{"type": "text", "text": "안녕"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 7},
        }
    )
    result = provider.chat(system="s", messages=[{"role": "user", "content": "hi"}])
    assert result.content == "안녕"
    assert result.stop_reason == "end_turn"
    assert result.token_usage == {"input": 5, "output": 7}


def test_bedrock_claude_chat_with_tool_use():
    provider = BedrockProvider(
        model="anthropic.claude-3-5-sonnet-20240620-v1:0",
        image_model="amazon.titan-image-generator-v1",
        region="us-east-1",
    )
    provider._client = MagicMock()
    provider._client.invoke_model.return_value = _bedrock_response(
        {
            "content": [
                {"type": "text", "text": "I'll search."},
                {"type": "tool_use", "id": "tu_1", "name": "search_web", "input": {"query": "x"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }
    )
    tools = [{"name": "search_web", "description": "", "parameters": {"type": "object"}}]
    result = provider.chat(system="s", messages=[], tools=tools)
    assert result.stop_reason == "tool_use"
    assert result.tool_calls[0].name == "search_web"
    assert result.tool_calls[0].arguments == {"query": "x"}


def test_bedrock_nova_coerces_dict_tool_content_to_json():
    """If a caller hands us a dict as tool content, Nova's `{"text": ...}`
    block must receive JSON, not Python's repr via str()."""
    msgs = [
        {"role": "tool", "tool_call_id": "t1", "content": {"ok": True, "count": 2}},
    ]
    translated = BedrockProvider._to_nova_messages(msgs)
    text = translated[0]["content"][0]["toolResult"]["content"][0]["text"]
    assert text == '{"ok": true, "count": 2}'


def test_bedrock_nova_preserves_string_tool_content():
    msgs = [
        {"role": "tool", "tool_call_id": "t1", "content": '{"already":"json"}'},
    ]
    translated = BedrockProvider._to_nova_messages(msgs)
    text = translated[0]["content"][0]["toolResult"]["content"][0]["text"]
    assert text == '{"already":"json"}'


def test_bedrock_message_translation_tool_role():
    messages = [
        {"role": "user", "content": "ask"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "foo", "arguments": {"a": 1}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "{\"ok\":true}"},
    ]
    translated = BedrockProvider._to_anthropic_messages(messages)
    assert translated[0] == {"role": "user", "content": "ask"}
    assert translated[1]["role"] == "assistant"
    assert translated[1]["content"][0]["type"] == "tool_use"
    assert translated[1]["content"][0]["name"] == "foo"
    assert translated[2]["role"] == "user"
    assert translated[2]["content"][0]["type"] == "tool_result"


def test_bedrock_nova_chat_text():
    provider = BedrockProvider(model="amazon.nova-pro-v1:0", image_model="amazon.nova-canvas-v1:0", region="us-east-1")
    provider._client = MagicMock()
    provider._client.converse.return_value = {
        "output": {"message": {"content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 2},
    }
    result = provider.chat(system="s", messages=[{"role": "user", "content": "hi"}])
    assert result.content == "hi"
    assert result.stop_reason == "end_turn"
    assert result.token_usage == {"input": 1, "output": 2}


def test_bedrock_nova_tool_use():
    provider = BedrockProvider(model="amazon.nova-pro-v1:0", image_model="amazon.nova-canvas-v1:0", region="us-east-1")
    provider._client = MagicMock()
    provider._client.converse.return_value = {
        "output": {
            "message": {
                "content": [
                    {"text": "let me search"},
                    {"toolUse": {"toolUseId": "tu1", "name": "search_web", "input": {"query": "q"}}},
                ]
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 2, "outputTokens": 3},
    }
    result = provider.chat(
        system="s",
        messages=[],
        tools=[{"name": "search_web", "description": "", "parameters": {"type": "object"}}],
    )
    assert result.stop_reason == "tool_use"
    assert result.tool_calls[0].name == "search_web"


def test_build_image_body_titan():
    provider = BedrockProvider(
        model="anthropic.claude-3-5-sonnet-20240620-v1:0",
        image_model="amazon.titan-image-generator-v1",
        region="us-east-1",
    )
    body = provider._build_image_body("a cat")
    assert body["taskType"] == "TEXT_IMAGE"


def test_build_image_body_stability():
    provider = BedrockProvider(
        model="anthropic.claude-3-5-sonnet-20240620-v1:0",
        image_model="stability.stable-diffusion-xl-v1",
        region="us-east-1",
    )
    body = provider._build_image_body("a cat")
    assert body["text_prompts"][0]["text"] == "a cat"


def test_build_image_body_unknown_raises():
    provider = BedrockProvider(
        model="anthropic.claude-3-5-sonnet-20240620-v1:0",
        image_model="mystery.v1",
        region="us-east-1",
    )
    with pytest.raises(ValueError):
        provider._build_image_body("x")


def test_bedrock_describe_image_returns_text():
    provider = BedrockProvider(
        model="anthropic.claude-3-5-sonnet-20240620-v1:0",
        image_model="amazon.titan-image-generator-v1",
        region="us-east-1",
    )
    provider._client = MagicMock()
    provider._client.invoke_model.return_value = _bedrock_response(
        {"content": [{"type": "text", "text": "a cat"}]}
    )
    out = provider.describe_image(b"fake", "image/png")
    assert out == "a cat"


def test_bedrock_nova_describe_image_uses_converse_not_invoke_model():
    """Nova text models can't accept Claude's Messages body — describe_image
    must route to the Converse API with an `image` content block instead."""
    provider = BedrockProvider(
        model="amazon.nova-pro-v1:0",
        image_model="amazon.nova-canvas-v1:0",
        region="us-east-1",
    )
    provider._client = MagicMock()
    provider._client.converse.return_value = {
        "output": {"message": {"content": [{"text": "a nova cat"}]}},
    }
    out = provider.describe_image(b"bytes", "image/png")
    assert out == "a nova cat"
    provider._client.converse.assert_called_once()
    provider._client.invoke_model.assert_not_called()
    messages = provider._client.converse.call_args.kwargs["messages"]
    img_block = next(b for b in messages[0]["content"] if "image" in b)
    assert img_block["image"]["format"] == "png"
    assert img_block["image"]["source"] == {"bytes": b"bytes"}


def test_bedrock_nova_describe_image_maps_mime_to_nova_format():
    """image/jpeg must be sent as format='jpeg' (Nova only accepts a short
    form: png/jpeg/gif/webp)."""
    provider = BedrockProvider(
        model="amazon.nova-lite-v1:0",
        image_model="amazon.nova-canvas-v1:0",
        region="us-east-1",
    )
    provider._client = MagicMock()
    provider._client.converse.return_value = {
        "output": {"message": {"content": [{"text": "x"}]}},
    }
    provider.describe_image(b"bytes", "image/jpeg")
    messages = provider._client.converse.call_args.kwargs["messages"]
    img_block = next(b for b in messages[0]["content"] if "image" in b)
    assert img_block["image"]["format"] == "jpeg"


def test_bedrock_nova_describe_image_inference_profile_routes_to_converse():
    """`us.amazon.nova-*` inference-profile IDs must still hit the Nova path."""
    provider = BedrockProvider(
        model="us.amazon.nova-pro-v1:0",
        image_model="amazon.nova-canvas-v1:0",
        region="us-east-1",
    )
    provider._client = MagicMock()
    provider._client.converse.return_value = {
        "output": {"message": {"content": [{"text": "ok"}]}},
    }
    provider.describe_image(b"x", "image/png")
    provider._client.converse.assert_called_once()
    provider._client.invoke_model.assert_not_called()


def test_bedrock_nova_describe_image_unknown_mime_falls_back_to_png():
    """Unsupported MIME types must not crash — fall back to 'png' so Nova
    receives a valid format value."""
    provider = BedrockProvider(
        model="amazon.nova-pro-v1:0",
        image_model="amazon.nova-canvas-v1:0",
        region="us-east-1",
    )
    provider._client = MagicMock()
    provider._client.converse.return_value = {
        "output": {"message": {"content": [{"text": "x"}]}},
    }
    provider.describe_image(b"bytes", "application/octet-stream")
    messages = provider._client.converse.call_args.kwargs["messages"]
    img_block = next(b for b in messages[0]["content"] if "image" in b)
    assert img_block["image"]["format"] == "png"


def test_bedrock_generate_image_titan_returns_bytes():
    provider = BedrockProvider(
        model="anthropic.claude-3-5-sonnet-20240620-v1:0",
        image_model="amazon.titan-image-generator-v1",
        region="us-east-1",
    )
    provider._client = MagicMock()
    provider._client.invoke_model.return_value = _bedrock_response(
        {"images": [base64.b64encode(b"imgdata").decode()]}
    )
    assert provider.generate_image("cat") == b"imgdata"


def test_bedrock_edit_image_raises_unsupported():
    """No tested image-edit path on any Bedrock image family yet — surface a
    clear NotImplementedError so the executor returns {ok: False, error: ...}
    and the LLM apologises rather than silently routing to text-to-image."""
    import pytest

    provider = BedrockProvider(
        model="anthropic.claude-3-5-sonnet-20240620-v1:0",
        image_model="amazon.titan-image-generator-v1",
        region="us-east-1",
    )
    with pytest.raises(NotImplementedError, match="not supported"):
        provider.edit_image("change", [(b"x", "image/png")])


def test_bedrock_generate_image_stability_returns_bytes():
    provider = BedrockProvider(
        model="anthropic.claude-3-5-sonnet-20240620-v1:0",
        image_model="stability.stable-diffusion-xl-v1",
        region="us-east-1",
    )
    provider._client = MagicMock()
    provider._client.invoke_model.return_value = _bedrock_response(
        {"artifacts": [{"base64": base64.b64encode(b"xyz").decode()}]}
    )
    assert provider.generate_image("cat") == b"xyz"


def test_bedrock_client_uses_explicit_timeout_and_retry_config():
    """Regression: default botocore read_timeout=60s + legacy retry=5 turns a
    single 80-90s Sonnet generation into 5 silent re-invocations that exhaust
    Lambda's 300s budget before any exception surfaces. The bedrock-runtime
    client must be built with an explicit Config that aligns read_timeout with
    the worker Lambda timeout and caps retries at 2 in standard mode."""
    provider = BedrockProvider(
        model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        image_model="amazon.nova-canvas-v1:0",
        region="us-east-1",
    )
    with patch("src.llms.bedrock.boto3.client") as mock_boto3_client:
        mock_boto3_client.return_value = MagicMock()
        provider._get_client()

    mock_boto3_client.assert_called_once()
    kwargs = mock_boto3_client.call_args.kwargs
    assert kwargs["region_name"] == "us-east-1"
    cfg = kwargs["config"]
    assert cfg.connect_timeout == 10
    assert cfg.read_timeout == 290
    assert cfg.retries == {"max_attempts": 2, "mode": "standard"}

