"""Web search tool (DuckDuckGo Instant Answer + optional Tavily)."""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from src.tools.registry import ToolContext, default_registry, tool
from src.tools.web import _read_body_capped

logger = logging.getLogger(__name__)

DUCKDUCKGO_HOST = "api.duckduckgo.com"
TAVILY_HOST = "api.tavily.com"
# Cap response sizes — search APIs occasionally return very large payloads
# (e.g. Tavily with high max_results and rich content). 2 MiB matches the
# default web fetch cap and is plenty for normal queries.
_SEARCH_RESPONSE_MAX_BYTES = 2 * 1024 * 1024


@tool(
    default_registry,
    name="search_web",
    description="Search the public web for up-to-date information. Uses Tavily if TAVILY_API_KEY is set, otherwise DuckDuckGo Instant Answer.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        "required": ["query"],
    },
)
def search_web(ctx: ToolContext, query: str, limit: int = 5) -> list[dict[str, str]]:
    if ctx.settings.tavily_api_key:
        return _tavily_search(ctx.settings.tavily_api_key, query, limit)
    return _ddg_search(query, limit)


def _ddg_search(query: str, limit: int) -> list[dict[str, str]]:
    params = urllib.parse.urlencode({"q": query, "format": "json", "no_redirect": 1, "no_html": 1})
    url = f"https://{DUCKDUCKGO_HOST}/?{params}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != DUCKDUCKGO_HOST:
        raise ValueError("invalid web search URL")
    with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310
        body = _read_body_capped(response, _SEARCH_RESPONSE_MAX_BYTES)
    payload = json.loads(body.decode("utf-8"))
    # Schema match with the Tavily branch — `content` is empty on DDG since
    # Instant Answer doesn't ship a per-result snippet, but emitting the
    # key anyway lets the LLM treat both branches uniformly.
    results: list[dict[str, str]] = []
    if payload.get("AbstractURL"):
        results.append(
            {
                "title": payload.get("AbstractText", ""),
                "url": payload["AbstractURL"],
                "content": payload.get("AbstractText", ""),
            }
        )
    for item in payload.get("RelatedTopics", []):
        if "Text" in item and "FirstURL" in item:
            results.append(
                {"title": item["Text"], "url": item["FirstURL"], "content": ""}
            )
            if len(results) >= limit:
                break
    return results[:limit]


def _tavily_search(api_key: str, query: str, limit: int) -> list[dict[str, str]]:
    url = f"https://{TAVILY_HOST}/search"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != TAVILY_HOST:
        raise ValueError("invalid Tavily URL")
    body = json.dumps({"api_key": api_key, "query": query, "max_results": limit}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310
        raw = _read_body_capped(response, _SEARCH_RESPONSE_MAX_BYTES)
    payload = json.loads(raw.decode("utf-8"))
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
        for r in payload.get("results", [])[:limit]
    ]


@tool(
    default_registry,
    name="search_images",
    description=(
        "Search the public web for images matching a query and return their "
        "URLs and LLM-generated descriptions. Requires TAVILY_API_KEY (raises "
        "an error if unset — there is no DDG fallback for image search). "
        "Each result is a public web URL — to attach the picked image to the "
        "Slack thread, pass the URL to attach_image_from_url; to edit it, "
        "attach first then call edit_image with the returned Slack URL."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        "required": ["query"],
    },
    timeout=20.0,
)
def search_images(ctx: ToolContext, query: str, limit: int = 5) -> list[dict[str, str]]:
    if not ctx.settings.tavily_api_key:
        raise ValueError(
            "image search requires TAVILY_API_KEY — set the env var or fall "
            "back to a text response describing where the user could look."
        )
    return _tavily_image_search(ctx.settings.tavily_api_key, query, limit)


def _tavily_image_search(api_key: str, query: str, limit: int) -> list[dict[str, str]]:
    """Call Tavily /search with `include_images` + `include_image_descriptions`.

    Tavily's `images` field shape depends on `include_image_descriptions`:
      - false → list[str] (URLs only)
      - true  → list[{url, description}]
    We always request descriptions so the LLM has enough to pick — and
    normalize both shapes here so the tool result is consistent.
    """
    url = f"https://{TAVILY_HOST}/search"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != TAVILY_HOST:
        raise ValueError("invalid Tavily URL")
    body = json.dumps(
        {
            "api_key": api_key,
            "query": query,
            # Tavily's image count is decoupled from `max_results`; we cap on
            # the client side after the response so the LLM doesn't see a
            # 20-item haystack when limit=5 was requested.
            "max_results": max(limit, 5),
            "include_images": True,
            "include_image_descriptions": True,
        }
    ).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310
        raw = _read_body_capped(response, _SEARCH_RESPONSE_MAX_BYTES)
    payload = json.loads(raw.decode("utf-8"))
    raw_images = payload.get("images") or []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_images:
        if isinstance(item, str):
            image_url, description = item, ""
        elif isinstance(item, dict):
            image_url = item.get("url", "") or ""
            description = item.get("description", "") or ""
        else:
            continue
        if not image_url or image_url in seen:
            continue
        seen.add(image_url)
        out.append({"url": image_url, "description": description})
        if len(out) >= limit:
            break
    return out
