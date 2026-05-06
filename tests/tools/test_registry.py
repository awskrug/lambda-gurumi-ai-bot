"""Tests for src.tools.registry."""
from __future__ import annotations

from tests.tools._helpers import _ctx
from src.llms import ToolCall
from src.tools import default_registry
from src.tools.registry import ToolDef, ToolExecutor, ToolRegistry


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_default_registry_has_expected_tools():
    names = set(default_registry.names())
    assert {
        "read_attached_images",
        "fetch_thread_history",
        "search_web",
        "generate_image",
        "edit_image",
        "get_current_time",
        "read_attached_document",
    }.issubset(names)
    assert "search_slack_messages" not in names  # removed — user-token only, tied to installer


def test_registry_specs_match_llm_shape():
    for spec in default_registry.specs():
        assert set(spec.keys()) == {"name", "description", "parameters"}
        assert spec["parameters"]["type"] == "object"


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #


def test_executor_unknown_tool():
    registry = ToolRegistry()
    executor = ToolExecutor(_ctx(), registry)
    result = executor.execute(ToolCall(id="1", name="nope", arguments={}))
    assert result["ok"] is False
    assert "unknown tool" in result["error"]


def test_executor_timeout_guards_slow_tools():
    import time

    registry = ToolRegistry()

    def slow(ctx):
        time.sleep(1.0)

    registry.register(ToolDef(name="slow", description="", parameters={"type": "object", "properties": {}}, fn=slow))
    executor = ToolExecutor(_ctx(), registry, timeout=0.1)
    result = executor.execute(ToolCall(id="1", name="slow", arguments={}))
    assert result["ok"] is False
    assert "timed out" in result["error"]


def test_executor_wraps_boto_client_error():
    """Bedrock invoke failures (botocore ClientError) must be returned as
    {ok: False, error: ...} so the LLM can plan around the failure instead
    of the exception bubbling out of the agent loop."""
    from botocore.exceptions import ClientError

    registry = ToolRegistry()

    def failing_bedrock(ctx):
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Legacy model"}},
            "InvokeModel",
        )

    registry.register(
        ToolDef(
            name="bedrock_thing",
            description="",
            parameters={"type": "object", "properties": {}},
            fn=failing_bedrock,
        )
    )
    executor = ToolExecutor(_ctx(), registry)
    result = executor.execute(ToolCall(id="1", name="bedrock_thing", arguments={}))
    assert result["ok"] is False
    assert "ResourceNotFoundException" in result["error"] or "Legacy" in result["error"]


def test_executor_captures_tool_error():
    registry = ToolRegistry()

    def boom(ctx):
        raise ValueError("nope")

    registry.register(ToolDef(name="boom", description="", parameters={"type": "object", "properties": {}}, fn=boom))
    executor = ToolExecutor(_ctx(), registry)
    result = executor.execute(ToolCall(id="1", name="boom", arguments={}))
    assert result["ok"] is False
    assert "nope" in result["error"]


def test_executor_per_tool_timeout_override():
    """A tool registered with its own timeout overrides the executor default."""
    import time

    registry = ToolRegistry()

    def moderately_slow(ctx):
        time.sleep(0.3)
        return "done"

    registry.register(
        ToolDef(
            name="slowish",
            description="",
            parameters={"type": "object", "properties": {}},
            fn=moderately_slow,
            timeout=1.0,
        )
    )
    # Default timeout short enough to kill a naïve tool; per-tool override lets
    # this one finish.
    executor = ToolExecutor(_ctx(), registry, timeout=0.1)
    result = executor.execute(ToolCall(id="1", name="slowish", arguments={}))
    assert result["ok"] is True
    assert result["result"] == "done"


def test_generate_image_tool_has_extended_timeout():
    """Image generation is slow; its registered timeout must be > default."""
    td = default_registry.get("generate_image")
    assert td is not None
    assert td.timeout is not None
    assert td.timeout >= 60.0


def test_edit_image_tool_has_extended_timeout():
    """Edit is at least as slow as generate (input upload + edit) — same ceiling."""
    td = default_registry.get("edit_image")
    assert td is not None
    assert td.timeout is not None
    assert td.timeout >= 60.0


def test_default_registry_now_includes_fetch_webpage():
    names = set(default_registry.names())
    assert "fetch_webpage" in names


def test_executor_wraps_arbitrary_provider_exception():
    """Tools that call provider SDKs can raise provider-specific errors
    (openai.APIError, anthropic.APIError, httpx.HTTPError, etc.) which aren't
    subclasses of ValueError/TypeError. The executor must still surface them
    as {ok: False, ...} so the LLM can recover — not propagate and abort the
    whole agent loop."""

    class FakeProviderError(Exception):
        """Stand-in for openai.APIError / anthropic.APIError etc. — not a
        subclass of any stdlib exception category the old allowlist knew
        about."""

    registry = ToolRegistry()

    def provider_call(ctx):
        raise FakeProviderError("rate limit: try again in 42s")

    registry.register(
        ToolDef(
            name="llm_thing",
            description="",
            parameters={"type": "object", "properties": {}},
            fn=provider_call,
        )
    )
    executor = ToolExecutor(_ctx(), registry)
    result = executor.execute(ToolCall(id="1", name="llm_thing", arguments={}))
    assert result["ok"] is False
    assert "FakeProviderError" in result["error"]
    assert "rate limit" in result["error"]


def test_executor_close_is_idempotent():
    """close() is called by the owning agent at end-of-request — invoking it
    twice (e.g. agent.run() running then the caller also calling close) must
    not raise."""
    registry = ToolRegistry()
    executor = ToolExecutor(_ctx(), registry)
    executor.close()
    executor.close()
    assert executor._closed is True
