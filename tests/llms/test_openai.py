"""Tests for src.llms.openai."""
from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock

from src.llms.openai import OpenAIProvider


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


def _make_stream_chunk(*, content=None, tool_calls=None, finish=None, usage=None):
    chunk = MagicMock()
    if usage is not None:
        chunk.usage = usage
    else:
        chunk.usage = None
    if content is None and tool_calls is None and finish is None:
        chunk.choices = []
        return chunk
    choice = MagicMock()
    choice.finish_reason = finish
    choice.delta.content = content
    choice.delta.tool_calls = tool_calls
    chunk.choices = [choice]
    return chunk


def _stream_tool_call(index, call_id=None, name=None, arguments=None):
    tc = MagicMock()
    tc.index = index
    tc.id = call_id
    fn = MagicMock()
    fn.name = name
    fn.arguments = arguments
    tc.function = fn
    return tc


def test_openai_chat_parses_text():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = _openai_completion(content="hello")
    result = provider.chat(system="s", messages=[{"role": "user", "content": "hi"}])
    assert result.content == "hello"
    assert result.stop_reason == "end_turn"
    assert result.tool_calls == []
    assert result.token_usage == {"input": 10, "output": 20}


def test_openai_legacy_model_uses_max_tokens_and_temperature():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = _openai_completion(content="x")
    provider.chat(system="s", messages=[])
    kwargs = provider._client.chat.completions.create.call_args.kwargs
    assert "max_tokens" in kwargs
    assert "temperature" in kwargs
    assert "max_completion_tokens" not in kwargs


def test_openai_new_generation_uses_max_completion_tokens():
    provider = OpenAIProvider(model="gpt-5.4", image_model="gpt-image-1")
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = _openai_completion(content="x")
    provider.chat(system="s", messages=[])
    kwargs = provider._client.chat.completions.create.call_args.kwargs
    assert "max_completion_tokens" in kwargs
    assert "max_tokens" not in kwargs
    assert "temperature" not in kwargs


def test_openai_o1_model_uses_max_completion_tokens():
    provider = OpenAIProvider(model="o1-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = _openai_completion(content="x")
    provider.chat(system="s", messages=[])
    kwargs = provider._client.chat.completions.create.call_args.kwargs
    assert "max_completion_tokens" in kwargs
    assert "temperature" not in kwargs


def test_openai_chat_streams_content_when_on_delta_given():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    chunks = [
        _make_stream_chunk(content="hello"),
        _make_stream_chunk(content=" world"),
        _make_stream_chunk(finish="stop"),
    ]
    provider._client.chat.completions.create.return_value = iter(chunks)

    received: list[str] = []
    result = provider.chat(system="s", messages=[], on_delta=received.append)

    assert received == ["hello", " world"]
    assert result.content == "hello world"
    assert result.stop_reason == "end_turn"
    assert result.tool_calls == []
    # must have requested streaming
    kwargs = provider._client.chat.completions.create.call_args.kwargs
    assert kwargs.get("stream") is True


def test_openai_chat_stream_suppresses_content_after_tool_calls_begin():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    chunks = [
        # tool_call begins first (no content yet)
        _make_stream_chunk(tool_calls=[_stream_tool_call(0, call_id="c1", name="search_web")]),
        _make_stream_chunk(tool_calls=[_stream_tool_call(0, arguments='{"query":')]),
        _make_stream_chunk(tool_calls=[_stream_tool_call(0, arguments='"x"}')]),
        # Model sometimes also emits commentary content after initiating a tool_call.
        _make_stream_chunk(content="I'll search."),
        _make_stream_chunk(finish="tool_calls"),
    ]
    provider._client.chat.completions.create.return_value = iter(chunks)

    received: list[str] = []
    result = provider.chat(
        system="s",
        messages=[],
        tools=[{"name": "search_web", "description": "", "parameters": {"type": "object"}}],
        on_delta=received.append,
    )

    # Content after tool_calls started must NOT reach on_delta.
    assert received == []
    assert result.tool_calls[0].name == "search_web"
    assert result.tool_calls[0].arguments == {"query": "x"}
    assert result.stop_reason == "tool_use"


def test_openai_chat_stream_accumulates_usage_from_last_chunk():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    usage = MagicMock()
    usage.prompt_tokens = 42
    usage.completion_tokens = 7
    chunks = [
        _make_stream_chunk(content="hi"),
        _make_stream_chunk(finish="stop", usage=usage),
    ]
    provider._client.chat.completions.create.return_value = iter(chunks)

    result = provider.chat(system="s", messages=[], on_delta=lambda _: None)
    assert result.token_usage == {"input": 42, "output": 7}


def test_openai_translates_canonical_tool_calls():
    """Canonical assistant tool_calls must be serialized to OpenAI's wire format."""
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = _openai_completion(content="done")

    canonical = [
        {"role": "user", "content": "ask"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "search_web", "arguments": {"query": "q"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "{\"ok\": true}"},
    ]
    provider.chat(system="s", messages=canonical)
    sent = provider._client.chat.completions.create.call_args.kwargs["messages"]
    # system + 3 canonical = 4
    assert len(sent) == 4
    assistant = sent[2]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["type"] == "function"
    assert assistant["tool_calls"][0]["function"]["name"] == "search_web"
    # arguments must be a JSON string, not a dict
    assert isinstance(assistant["tool_calls"][0]["function"]["arguments"], str)
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"query": "q"}


def test_openai_chat_parses_tool_calls():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    tc = _openai_tool_call("call_1", "search_web", {"query": "aws"})
    provider._client.chat.completions.create.return_value = _openai_completion(
        tool_calls=[tc], finish="tool_calls"
    )
    result = provider.chat(system="s", messages=[], tools=[{"name": "search_web", "description": "", "parameters": {}}])
    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "search_web"
    assert result.tool_calls[0].arguments == {"query": "aws"}


def test_openai_chat_handles_bad_tool_arguments():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    tc = MagicMock()
    tc.id = "x"
    tc.function.name = "search_web"
    tc.function.arguments = "not json"
    provider._client.chat.completions.create.return_value = _openai_completion(
        tool_calls=[tc], finish="tool_calls"
    )
    result = provider.chat(system="s", messages=[], tools=[{"name": "search_web", "description": "", "parameters": {}}])
    assert result.tool_calls[0].arguments == {}


def test_openai_stream_chat_invokes_callback():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()

    def _chunk(text):
        ch = MagicMock()
        ch.choices[0].delta.content = text
        return ch

    provider._client.chat.completions.create.return_value = iter([_chunk("he"), _chunk("llo")])
    seen = []
    result = provider.stream_chat(system="s", messages=[], on_delta=seen.append)
    assert result == "hello"
    assert seen == ["he", "llo"]


def test_openai_describe_image_uses_vision_format():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    provider._client.chat.completions.create.return_value = _openai_completion(content="it's a cat")
    out = provider.describe_image(b"\x89PNG", "image/png")
    assert out == "it's a cat"
    args = provider._client.chat.completions.create.call_args.kwargs
    assert args["messages"][0]["content"][1]["type"] == "image_url"


def test_openai_generate_image_decodes_b64():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    response = MagicMock()
    response.data = [MagicMock(b64_json=base64.b64encode(b"hello").decode())]
    provider._client.images.generate.return_value = response
    assert provider.generate_image("cat") == b"hello"
    kwargs = provider._client.images.generate.call_args.kwargs
    # gpt-image-1 must NOT send response_format (API rejects it)
    assert "response_format" not in kwargs


def test_openai_generate_image_dalle_sends_response_format():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="dall-e-3")
    provider._client = MagicMock()
    response = MagicMock()
    response.data = [MagicMock(b64_json=base64.b64encode(b"ok").decode())]
    provider._client.images.generate.return_value = response
    provider.generate_image("cat")
    kwargs = provider._client.images.generate.call_args.kwargs
    assert kwargs["response_format"] == "b64_json"


def test_openai_edit_image_sends_single_file_for_one_image():
    """gpt-image-1 accepts a single (filename, fileobj, mime) tuple when
    only one input image is given; we pass it through `image=` not `image[]`."""
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    response = MagicMock()
    response.data = [MagicMock(b64_json=base64.b64encode(b"out").decode())]
    provider._client.images.edit.return_value = response

    out = provider.edit_image("style as pencil", [(b"\x89PNG-bytes", "image/png")])

    assert out == b"out"
    kwargs = provider._client.images.edit.call_args.kwargs
    assert kwargs["model"] == "gpt-image-1"
    assert kwargs["prompt"] == "style as pencil"
    # gpt-image-1 rejects response_format and is multi-image capable but
    # given one image we pass a bare tuple, not a list.
    assert "response_format" not in kwargs
    image_arg = kwargs["image"]
    assert isinstance(image_arg, tuple)
    filename, fileobj, mime = image_arg
    assert filename == "image_0.png"
    assert mime == "image/png"
    assert fileobj.read() == b"\x89PNG-bytes"


def test_openai_edit_image_sends_list_for_multiple_images():
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    provider._client = MagicMock()
    response = MagicMock()
    response.data = [MagicMock(b64_json=base64.b64encode(b"o").decode())]
    provider._client.images.edit.return_value = response

    provider.edit_image(
        "merge",
        [(b"a", "image/png"), (b"b", "image/jpeg")],
    )

    image_arg = provider._client.images.edit.call_args.kwargs["image"]
    assert isinstance(image_arg, list)
    assert len(image_arg) == 2
    # Filename hint follows the mime type so OpenAI's content-type detection
    # picks the right format on the multipart side.
    assert image_arg[0][0] == "image_0.png"
    assert image_arg[1][0] == "image_1.jpg"


def test_openai_edit_image_dalle_includes_response_format_and_size():
    """dall-e-2 still needs the explicit b64_json + size; gpt-image-1 doesn't."""
    provider = OpenAIProvider(model="gpt-4o-mini", image_model="dall-e-2")
    provider._client = MagicMock()
    response = MagicMock()
    response.data = [MagicMock(b64_json=base64.b64encode(b"ok").decode())]
    provider._client.images.edit.return_value = response

    provider.edit_image("change", [(b"x", "image/png")])

    kwargs = provider._client.images.edit.call_args.kwargs
    assert kwargs["response_format"] == "b64_json"
    assert kwargs["size"] == "1024x1024"


def test_openai_edit_image_requires_at_least_one_image():
    import pytest

    provider = OpenAIProvider(model="gpt-4o-mini", image_model="gpt-image-1")
    with pytest.raises(ValueError, match="at least one input image"):
        provider.edit_image("hi", [])
