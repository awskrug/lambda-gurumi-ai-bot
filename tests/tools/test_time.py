"""Tests for src.tools.time."""
from __future__ import annotations

from tests.tools._helpers import _ctx
from src.llms import ToolCall
from src.tools.registry import ToolExecutor
from src.tools import default_registry
from src.tools.time import get_current_time


# --------------------------------------------------------------------------- #
# get_current_time
# --------------------------------------------------------------------------- #


def test_get_current_time_uses_default_timezone():
    ctx = _ctx()  # _settings() default_timezone defaults to Asia/Seoul
    out = get_current_time(ctx)
    assert out["timezone"] == "Asia/Seoul"
    assert out["iso"].endswith("+09:00")
    # Weekday is a full English day name (Monday..Sunday)
    assert out["weekday"] in {
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday",
    }
    assert isinstance(out["unix"], int)


def test_get_current_time_respects_custom_timezone():
    ctx = _ctx()
    out = get_current_time(ctx, timezone="UTC")
    assert out["timezone"] == "UTC"
    assert out["iso"].endswith("+00:00")


def test_get_current_time_invalid_tz_via_executor():
    """Invalid timezone should surface as {ok: False, error: ...} via the
    executor so the LLM can recover."""
    executor = ToolExecutor(_ctx(), default_registry)
    result = executor.execute(
        ToolCall(id="t1", name="get_current_time", arguments={"timezone": "Narnia/Center"})
    )
    assert result["ok"] is False
    assert "unknown timezone" in result["error"]
