"""Tests for src.tools.web."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.tools._helpers import _ctx, _settings, _streamed_read
from src.config import Settings
from src.tools.registry import ToolContext, ToolExecutor
from src.tools.web import (
    _HtmlTextExtractor,
    _NoRedirectHandler,
    _extract_markdown_links,
    _filter_links,
    _jina_fetch,
    _parse_jina_response,
    _raw_fetch,
    _validate_public_https_url,
    fetch_webpage,
)


def _public_dns(monkeypatch):
    """Route all src.tools.web.socket.getaddrinfo lookups to a public IP."""

    def _public(host, port, family=0, type=0, *args, **kwargs):
        return [(None, None, None, "", ("93.184.216.34", port))]

    monkeypatch.setattr("src.tools.web.socket.getaddrinfo", _public)


# --------------------------------------------------------------------------- #
# fetch_webpage — SSRF guard
# --------------------------------------------------------------------------- #


def test_validate_public_https_url_rejects_http_scheme():
    with pytest.raises(ValueError, match="https"):
        _validate_public_https_url("http://example.com/")


def test_validate_public_https_url_rejects_ip_literal_v4():
    with pytest.raises(ValueError, match="IP literals"):
        _validate_public_https_url("https://127.0.0.1/")


def test_validate_public_https_url_rejects_ip_literal_v6():
    with pytest.raises(ValueError, match="IP literals"):
        _validate_public_https_url("https://[::1]/")


def test_validate_public_https_url_rejects_private_dns(monkeypatch):
    def fake_getaddrinfo(host, port, family=0, type=0, *args, **kwargs):
        # Simulate DNS pointing at RFC1918 space.
        return [(None, None, None, "", ("10.0.0.1", port))]

    monkeypatch.setattr("src.tools.web.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="non-public"):
        _validate_public_https_url("https://internal.corp.example/")


def test_validate_public_https_url_rejects_metadata_host(monkeypatch):
    def fake_getaddrinfo(host, port, family=0, type=0, *args, **kwargs):
        return [(None, None, None, "", ("169.254.169.254", port))]

    monkeypatch.setattr("src.tools.web.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="non-public"):
        _validate_public_https_url("https://cloud.metadata.example/")


def test_validate_public_https_url_accepts_public_host(monkeypatch):
    def fake_getaddrinfo(host, port, family=0, type=0, *args, **kwargs):
        return [(None, None, None, "", ("93.184.216.34", port))]  # example.com

    monkeypatch.setattr("src.tools.web.socket.getaddrinfo", fake_getaddrinfo)
    scheme, host = _validate_public_https_url("https://example.com/path")
    assert scheme == "https"
    assert host == "example.com"


def test_validate_public_https_url_dns_failure(monkeypatch):
    import socket as _socket

    def fake_getaddrinfo(host, port, family=0, type=0, *args, **kwargs):
        raise _socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr("src.tools.web.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="DNS resolution failed"):
        _validate_public_https_url("https://nonexistent.invalid.example/")


def test_no_redirect_handler_raises_on_302():
    import urllib.error
    import urllib.request

    handler = _NoRedirectHandler()
    req = urllib.request.Request("https://example.com/")
    with pytest.raises(urllib.error.HTTPError, match="redirects not allowed"):
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/")


# --------------------------------------------------------------------------- #
# fetch_webpage — HTML parser + Jina response parser
# --------------------------------------------------------------------------- #


def test_html_text_extractor_basic():
    html = (
        "<html><head><title>  Hello </title></head>"
        "<body>"
        "<script>alert('x')</script>"
        "<style>.a{}</style>"
        "<h1>Heading</h1>"
        "<p>Para one.</p>"
        "<p>Para two.</p>"
        "<a href='https://a.example/1'>Link A</a>"
        "<a href='/relative'>Rel</a>"
        "<a href='#frag'>Frag</a>"
        "<a href='mailto:x@y'>Mail</a>"
        "</body></html>"
    )
    x = _HtmlTextExtractor("https://base.example/page")
    x.feed(html)
    assert x.title() == "Hello"
    text = x.text()
    assert "Heading" in text
    assert "Para one." in text
    assert "Para two." in text
    assert "alert" not in text
    assert ".a{}" not in text
    assert ("Link A", "https://a.example/1") in x.links
    # Relative links resolved against base
    assert ("Rel", "https://base.example/relative") in x.links
    # mailto / fragment retained raw — filtering happens in _filter_links
    assert any(url.startswith("mailto:") for _, url in x.links)


def test_filter_links_drops_non_https_and_dedups():
    raw = [
        ("A", "https://a.example/1"),
        ("A dup", "https://a.example/1#top"),  # dedups by fragment-stripped url
        ("B", "https://b.example/"),
        ("Self", "https://base.example/page"),  # self-ref dropped
        ("Mail", "mailto:x@y"),
        ("JS", "javascript:void(0)"),
        ("HTTP", "http://insecure.example/"),
    ]
    out = _filter_links(raw, base_url="https://base.example/page", limit=10)
    urls = [item["url"] for item in out]
    assert urls == ["https://a.example/1", "https://b.example/"]
    assert out[0]["title"] == "A"


def test_filter_links_respects_limit():
    raw = [(f"T{i}", f"https://x.example/{i}") for i in range(5)]
    out = _filter_links(raw, base_url="https://base.example/", limit=3)
    assert len(out) == 3
    assert [item["url"] for item in out] == [
        "https://x.example/0",
        "https://x.example/1",
        "https://x.example/2",
    ]


def test_extract_markdown_links_parses_inline_markdown():
    md = (
        "Title\n\nSome prose with [Google](https://google.com/about) "
        "and [Self](https://base.example/page) and [Same](https://google.com/about?ref=x).\n"
        "[Another](https://example.org/)"
    )
    out = _extract_markdown_links(md, base_url="https://base.example/page", limit=10)
    urls = [item["url"] for item in out]
    assert "https://google.com/about" in urls
    assert "https://example.org/" in urls
    assert "https://base.example/page" not in urls  # self-ref dropped


def test_parse_jina_response_strips_header():
    payload = (
        "Title: Example Page\n"
        "URL Source: https://example.com/\n"
        "Markdown Content:\n"
        "# Heading\n\nBody text.\n"
    )
    title, body = _parse_jina_response(payload)
    assert title == "Example Page"
    assert body.startswith("# Heading")
    assert "URL Source" not in body


def test_parse_jina_response_no_header():
    payload = "just raw markdown\n\nwith no prefix"
    title, body = _parse_jina_response(payload)
    assert title == ""
    assert body == payload


def test_html_text_extractor_empty():
    x = _HtmlTextExtractor("https://base.example/")
    x.feed("")
    assert x.title() == ""
    assert x.text() == ""
    assert x.links == []


def test_filter_links_host_case_normalization():
    out = _filter_links(
        [
            ("A", "https://Example.COM/path"),
            ("B", "https://example.com/path"),   # dup by host case
            ("Self", "https://BASE.EXAMPLE/page"),  # self-ref by host case
        ],
        base_url="https://base.example/page",
        limit=10,
    )
    urls = [item["url"] for item in out]
    assert urls == ["https://Example.COM/path"]  # first-seen wins, case preserved in output


def test_extract_markdown_links_skips_images():
    md = (
        "![logo](https://img.example/logo.png) "
        "see [Docs](https://docs.example/) for more"
    )
    out = _extract_markdown_links(md, base_url="https://base.example/", limit=10)
    urls = [item["url"] for item in out]
    assert "https://img.example/logo.png" not in urls
    assert "https://docs.example/" in urls


def test_extract_markdown_links_preserves_paren_in_url():
    md = "see [Wiki](https://en.wikipedia.org/wiki/Foo_(bar)) for context"
    out = _extract_markdown_links(md, base_url="https://base.example/", limit=10)
    urls = [item["url"] for item in out]
    assert "https://en.wikipedia.org/wiki/Foo_(bar)" in urls


def test_parse_jina_response_inline_markdown_content():
    payload = "Title: T\nURL Source: https://example.com/\nMarkdown Content: # Heading\n\nBody here."
    title, body = _parse_jina_response(payload)
    assert title == "T"
    assert body.startswith("# Heading")
    assert "Body here." in body


# --------------------------------------------------------------------------- #
# fetch_webpage — fetch helpers
# --------------------------------------------------------------------------- #


def test_jina_fetch_returns_body_under_cap():
    payload = b"Title: x\nURL Source: https://example.com/\n\nBody here."
    with patch("src.tools.web.urllib.request.urlopen") as opener:
        resp = opener.return_value.__enter__.return_value
        resp.headers = {"Content-Length": str(len(payload)), "Content-Type": "text/markdown"}
        resp.read.side_effect = _streamed_read(payload)
        text = _jina_fetch("https://r.jina.ai", "https://example.com/", max_bytes=1024)
    assert "Body here." in text


def test_jina_fetch_content_length_over_cap():
    with patch("src.tools.web.urllib.request.urlopen") as opener:
        resp = opener.return_value.__enter__.return_value
        resp.headers = {"Content-Length": "999999"}
        resp.read.side_effect = _streamed_read(b"x" * 10)
        with pytest.raises(ValueError, match="MAX_WEB_BYTES"):
            _jina_fetch("https://r.jina.ai", "https://example.com/", max_bytes=1024)


def test_jina_fetch_streamed_over_cap():
    body = b"x" * 2000
    with patch("src.tools.web.urllib.request.urlopen") as opener:
        resp = opener.return_value.__enter__.return_value
        resp.headers = {}  # no Content-Length
        resp.read.side_effect = _streamed_read(body)
        with pytest.raises(ValueError, match="MAX_WEB_BYTES"):
            _jina_fetch("https://r.jina.ai", "https://example.com/", max_bytes=1024)


def test_raw_fetch_returns_body_under_cap(monkeypatch):
    body = b"<html><title>Hi</title><body>hello</body></html>"

    # _raw_fetch builds its own opener (with _NoRedirectHandler) and calls opener.open.
    # Patch urllib.request.build_opener so we intercept that call.
    fake_opener = MagicMock()
    cm = fake_opener.open.return_value.__enter__.return_value
    cm.headers = {"Content-Length": str(len(body)), "Content-Type": "text/html"}
    cm.read.side_effect = _streamed_read(body)
    monkeypatch.setattr("src.tools.web.urllib.request.build_opener", lambda *_: fake_opener)

    html = _raw_fetch("https://example.com/", max_bytes=1024)
    assert "hello" in html


def test_raw_fetch_streamed_over_cap(monkeypatch):
    body = b"a" * 4096
    fake_opener = MagicMock()
    cm = fake_opener.open.return_value.__enter__.return_value
    cm.headers = {}
    cm.read.side_effect = _streamed_read(body)
    monkeypatch.setattr("src.tools.web.urllib.request.build_opener", lambda *_: fake_opener)
    with pytest.raises(ValueError, match="MAX_WEB_BYTES"):
        _raw_fetch("https://example.com/", max_bytes=1024)


# --------------------------------------------------------------------------- #
# fetch_webpage — end-to-end via tool function
# --------------------------------------------------------------------------- #


def test_fetch_webpage_rejects_http_via_tool(monkeypatch):
    ctx = _ctx()
    with pytest.raises(ValueError, match="https"):
        fetch_webpage(ctx, url="http://example.com/")


def test_fetch_webpage_jina_happy_path(monkeypatch):
    _public_dns(monkeypatch)
    jina_body = (
        b"Title: Example Page\n"
        b"URL Source: https://example.com/\n"
        b"Markdown Content:\n"
        b"# Hello\n\nSee [Docs](https://docs.example.com/) and [Blog](https://blog.example.com/).\n"
    )
    ctx = _ctx()
    with patch("src.tools.web.urllib.request.urlopen") as opener:
        resp = opener.return_value.__enter__.return_value
        resp.headers = {"Content-Length": str(len(jina_body))}
        resp.read.side_effect = _streamed_read(jina_body)
        out = fetch_webpage(ctx, url="https://example.com/")
    assert out["source"] == "jina"
    assert out["title"] == "Example Page"
    assert "Hello" in out["content"]
    urls = [link["url"] for link in out["links"]]
    assert "https://docs.example.com/" in urls
    assert "https://blog.example.com/" in urls
    assert out["truncated"] is False
    assert out["chars"] == len(out["content"])


def test_fetch_webpage_falls_back_to_raw_on_jina_5xx(monkeypatch):
    import urllib.error

    _public_dns(monkeypatch)
    html_body = (
        b"<html><head><title>Raw Title</title></head>"
        b"<body><p>Raw body.</p>"
        b"<a href='https://docs.example.com/'>Docs</a></body></html>"
    )

    def jina_fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://r.jina.ai/https://example.com/", 500, "boom", {}, None
        )

    fake_opener = MagicMock()
    cm = fake_opener.open.return_value.__enter__.return_value
    cm.headers = {"Content-Length": str(len(html_body))}
    cm.read.side_effect = _streamed_read(html_body)

    monkeypatch.setattr("src.tools.web.urllib.request.urlopen", jina_fail)
    monkeypatch.setattr("src.tools.web.urllib.request.build_opener", lambda *_: fake_opener)

    ctx = _ctx()
    out = fetch_webpage(ctx, url="https://example.com/")
    assert out["source"] == "raw"
    assert out["title"] == "Raw Title"
    assert "Raw body." in out["content"]
    assert any(link["url"] == "https://docs.example.com/" for link in out["links"])


def test_fetch_webpage_jina_body_over_cap_falls_back_to_raw(monkeypatch):
    """Jina oversize → fall through to raw fetch instead of raising."""
    _public_dns(monkeypatch)
    settings = Settings(
        slack_bot_token="xoxb-test",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        image_provider="openai",
        image_model="gpt-image-1",
        image_model_gpt="gpt-image-1",
        image_model_xai="grok-imagine-image",
        agent_max_steps=3,
        response_language="ko",
        dynamodb_table_name="t",
        aws_region="us-east-1",
        max_web_bytes=128,
        max_web_chars=8000,
        max_web_links=20,
        jina_reader_base="https://r.jina.ai",
    )
    ctx = ToolContext(
        slack_client=MagicMock(),
        channel="C1",
        thread_ts="ts1",
        event={},
        settings=settings,
        llm=MagicMock(),
    )

    huge_jina = b"x" * 4096
    raw_html = b"<html><body><p>raw small body</p></body></html>"

    fake_opener = MagicMock()
    cm = fake_opener.open.return_value.__enter__.return_value
    cm.headers = {"Content-Length": str(len(raw_html))}
    cm.read.side_effect = _streamed_read(raw_html)

    with patch("src.tools.web.urllib.request.urlopen") as jina_open:
        jresp = jina_open.return_value.__enter__.return_value
        jresp.headers = {}  # no Content-Length → streamed-read path
        jresp.read.side_effect = _streamed_read(huge_jina)
        monkeypatch_build = patch("src.tools.web.urllib.request.build_opener", lambda *_: fake_opener)
        with monkeypatch_build:
            out = fetch_webpage(ctx, url="https://example.com/")
    assert out["source"] == "raw"
    assert "raw small body" in out["content"]


def test_fetch_webpage_max_chars_truncates(monkeypatch):
    _public_dns(monkeypatch)
    long_body = b"Title: T\nURL Source: https://example.com/\nMarkdown Content:\n" + (b"A" * 500)
    ctx = _ctx()
    with patch("src.tools.web.urllib.request.urlopen") as opener:
        resp = opener.return_value.__enter__.return_value
        resp.headers = {"Content-Length": str(len(long_body))}
        resp.read.side_effect = _streamed_read(long_body)
        out = fetch_webpage(ctx, url="https://example.com/", max_chars=200)
    assert out["truncated"] is True
    assert out["chars"] == 200
    assert len(out["content"]) == 200


def test_fetch_webpage_both_paths_fail_raises(monkeypatch):
    import urllib.error

    _public_dns(monkeypatch)

    def jina_500(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://r.jina.ai/...", 500, "jina boom", {}, None
        )

    def raw_503(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://example.com/", 503, "raw boom", {}, None
        )

    fake_opener = MagicMock()
    fake_opener.open.side_effect = raw_503

    monkeypatch.setattr("src.tools.web.urllib.request.urlopen", jina_500)
    monkeypatch.setattr(
        "src.tools.web.urllib.request.build_opener", lambda *_: fake_opener
    )

    ctx = _ctx()
    with pytest.raises(ValueError, match=r"jina=.*raw="):
        fetch_webpage(ctx, url="https://example.com/")


def test_fetch_webpage_max_links_dedup(monkeypatch):
    _public_dns(monkeypatch)
    link_section = (
        "[a](https://a.example/)"
        "[b](https://b.example/)"
        "[c](https://c.example/)"
        "[d](https://d.example/)"
        "[dup-a](https://a.example/)"
        "[e](https://e.example/)"
        "[dup-b](https://b.example/)"
        "[f](https://f.example/)"
    )
    payload = (
        "Title: T\nURL Source: https://example.com/\nMarkdown Content:\n" + link_section
    ).encode()
    ctx = _ctx()
    with patch("src.tools.web.urllib.request.urlopen") as opener:
        resp = opener.return_value.__enter__.return_value
        resp.headers = {"Content-Length": str(len(payload))}
        resp.read.side_effect = _streamed_read(payload)
        out = fetch_webpage(ctx, url="https://example.com/", max_links=3)
    urls = [link["url"] for link in out["links"]]
    assert urls == [
        "https://a.example/",
        "https://b.example/",
        "https://c.example/",
    ]
