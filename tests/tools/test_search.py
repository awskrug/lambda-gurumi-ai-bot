"""Tests for src.tools.search."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tests.tools._helpers import _ctx, _settings
from src.tools.registry import ToolContext
from src.tools.search import search_images, search_web


# --------------------------------------------------------------------------- #
# search_web
# --------------------------------------------------------------------------- #


def test_search_web_ddg_parses_results():
    ctx = _ctx()
    payload = {
        "AbstractURL": "https://example.com/a",
        "AbstractText": "abstract",
        "RelatedTopics": [{"Text": "t1", "FirstURL": "https://example.com/1"}],
    }
    with patch("src.tools.search.urllib.request.urlopen") as opener:
        opener.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        results = search_web(ctx, query="q", limit=5)
    assert results[0]["url"] == "https://example.com/a"
    assert results[1]["url"] == "https://example.com/1"


def test_search_web_uses_tavily_when_key_set():
    ctx = ToolContext(
        slack_client=MagicMock(),
        channel="C1",
        thread_ts="ts1",
        event={},
        settings=_settings(tavily_api_key="tvly-xyz"),
        llm=MagicMock(),
    )
    payload = {"results": [{"title": "t", "url": "https://x", "content": "c"}]}
    with patch("src.tools.search.urllib.request.urlopen") as opener:
        opener.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        out = search_web(ctx, query="q", limit=5)
    assert out == [{"title": "t", "url": "https://x", "content": "c"}]


# --------------------------------------------------------------------------- #
# search_images
# --------------------------------------------------------------------------- #


def _tavily_ctx():
    return ToolContext(
        slack_client=MagicMock(),
        channel="C1",
        thread_ts="ts1",
        event={},
        settings=_settings(tavily_api_key="tvly-xyz"),
        llm=MagicMock(),
    )


def test_search_images_requires_tavily_key():
    """No DDG fallback for images — fail loudly so the LLM can fall back to text."""
    ctx = _ctx()  # _settings() leaves tavily_api_key=None
    with pytest.raises(ValueError, match="TAVILY_API_KEY"):
        search_images(ctx, query="cat")


def test_search_images_parses_object_shape_with_descriptions():
    """When `include_image_descriptions=true`, Tavily returns
    `images: [{url, description}, ...]`. We pass that through unchanged
    (sans extra fields)."""
    payload = {
        "images": [
            {"url": "https://img1", "description": "a fluffy cat"},
            {"url": "https://img2", "description": "another cat"},
        ],
        "results": [],
    }
    with patch("src.tools.search.urllib.request.urlopen") as opener:
        opener.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        out = search_images(_tavily_ctx(), query="cat", limit=5)
    assert out == [
        {"url": "https://img1", "description": "a fluffy cat"},
        {"url": "https://img2", "description": "another cat"},
    ]


def test_search_images_parses_legacy_string_shape():
    """If a future API version (or older endpoint) returns plain URLs, the
    tool should still produce records — description simply blank."""
    payload = {"images": ["https://img1", "https://img2"], "results": []}
    with patch("src.tools.search.urllib.request.urlopen") as opener:
        opener.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        out = search_images(_tavily_ctx(), query="cat", limit=5)
    assert out == [
        {"url": "https://img1", "description": ""},
        {"url": "https://img2", "description": ""},
    ]


def test_search_images_dedupes_and_caps_at_limit():
    payload = {
        "images": [
            {"url": "https://img1", "description": "a"},
            {"url": "https://img1", "description": "duplicate"},  # dropped
            {"url": "https://img2", "description": "b"},
            {"url": "https://img3", "description": "c"},
            {"url": "https://img4", "description": "d"},  # dropped (limit=3)
        ],
    }
    with patch("src.tools.search.urllib.request.urlopen") as opener:
        opener.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        out = search_images(_tavily_ctx(), query="cat", limit=3)
    assert [item["url"] for item in out] == ["https://img1", "https://img2", "https://img3"]


def test_search_images_sets_include_images_in_request():
    """Regression: the tool MUST pass include_images=true; otherwise Tavily
    returns no images even with a valid API key."""
    captured: dict = {}

    def _spy(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps({"images": []}).encode()
        return cm

    with patch("src.tools.search.urllib.request.urlopen", side_effect=_spy):
        search_images(_tavily_ctx(), query="cat", limit=5)

    assert captured["body"]["include_images"] is True
    assert captured["body"]["include_image_descriptions"] is True
    assert captured["body"]["query"] == "cat"
