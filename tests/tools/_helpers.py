"""Shared fixtures for tests/tools/ — _settings, _ctx, _streamed_read."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.config import Settings
from src.tools.registry import ToolContext


def _settings(**overrides) -> Settings:
    base = {
        "slack_bot_token": "xoxb-test",
        "llm_provider": "openai",
        "llm_model": "gpt-4o-mini",
        "image_provider": "openai",
        "image_model": "gpt-image-1",
        "agent_max_steps": 3,
        "response_language": "ko",
        "dynamodb_table_name": "t",
        "aws_region": "us-east-1",
    }
    base.update(overrides)
    return Settings(**base)


def _ctx(event=None, slack_client=None, llm=None):
    return ToolContext(
        slack_client=slack_client or MagicMock(),
        channel="C1",
        thread_ts="ts1",
        event=event or {},
        settings=_settings(),
        llm=llm or MagicMock(),
    )


def _streamed_read(body: bytes):
    """Build a urlopen-mock read side_effect that serves `body` in chunks."""
    buf = {"pos": 0}

    def _chunked(n=-1):
        if n == -1:
            remaining = body[buf["pos"]:]
            buf["pos"] = len(body)
            return remaining
        chunk = body[buf["pos"]:buf["pos"] + n]
        buf["pos"] += len(chunk)
        return chunk

    return _chunked
