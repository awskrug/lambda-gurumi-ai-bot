"""Tests for src.tools.image."""
from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock, patch

import pytest

from tests.tools._helpers import _ctx, _settings
from src.tools.image import attach_image_from_url, edit_image, generate_image
from src.tools.registry import ToolContext


# --------------------------------------------------------------------------- #
# generate_image
# --------------------------------------------------------------------------- #


def test_generate_image_returns_permalink():
    llm = MagicMock()
    llm.generate_image.return_value = b"imgbytes"
    client = MagicMock()
    client.files_upload_v2.return_value = {"file": {"permalink": "https://slack/abc", "title": "t"}}
    ctx = _ctx(slack_client=client, llm=llm)
    out = generate_image(ctx, prompt="cat")
    assert out == {"permalink": "https://slack/abc", "title": "t"}
    llm.generate_image.assert_called_once_with("cat")
    upload_kwargs = client.files_upload_v2.call_args.kwargs
    assert upload_kwargs["title"] == ctx.settings.image_model


# --------------------------------------------------------------------------- #
# edit_image — input image source
# --------------------------------------------------------------------------- #


def _ok_response(body: bytes = b"\x89PNG-x", mime: str = "image/png"):
    """urlopen() context manager returning Slack image bytes + Content-Type header."""
    headers = {"Content-Type": mime}
    cm = MagicMock()
    cm.__enter__ = lambda self: self
    cm.__exit__ = lambda *a: False
    cm.read.return_value = body
    cm.headers.get = lambda k, default=None: headers.get(k, default)
    return cm


def test_edit_image_uses_attached_files_by_default():
    """When the LLM omits `urls`, edit_image collects images from the
    triggering mention's attachments (mirrors read_attached_images)."""
    llm = MagicMock()
    llm.edit_image.return_value = b"edited"
    client = MagicMock()
    client.token = "xoxb-token"
    client.files_upload_v2.return_value = {"file": {"permalink": "https://slack/p", "title": "t"}}
    event = {
        "files": [
            {
                "mimetype": "image/png",
                "url_private_download": "https://files.slack.com/T1/F1/cat.png",
                "name": "cat.png",
            }
        ]
    }
    ctx = _ctx(event=event, slack_client=client, llm=llm)

    with patch("src.tools.image._http_get", return_value=_ok_response(b"raw-png")):
        out = edit_image(ctx, prompt="make it pencil sketch")

    assert out["permalink"] == "https://slack/p"
    llm.edit_image.assert_called_once()
    args, _ = llm.edit_image.call_args
    assert args[0] == "make it pencil sketch"
    images = args[1]
    assert images == [(b"raw-png", "image/png")]


def test_edit_image_uses_explicit_urls_over_attachments():
    """When the LLM passes `urls` (e.g. from fetch_thread_history) we use
    those, NOT the current message's attachments — caller is being deliberate."""
    llm = MagicMock()
    llm.edit_image.return_value = b"edited"
    client = MagicMock()
    client.token = "xoxb-token"
    client.files_upload_v2.return_value = {"file": {"permalink": "https://slack/p"}}
    event = {
        "files": [
            {"mimetype": "image/png", "url_private_download": "https://files.slack.com/T1/F1/now.png"}
        ]
    }
    ctx = _ctx(event=event, slack_client=client, llm=llm)
    captured: list = []

    def _spy_urlopen(req, timeout=None):
        captured.append(req.full_url)
        return _ok_response(b"old-png", mime="image/png")

    with patch("src.tools.image._http_get", side_effect=_spy_urlopen):
        edit_image(
            ctx,
            prompt="redo",
            urls=["https://files.slack.com/T1/F2/older.png"],
        )

    # Only the explicit URL was fetched — the attached file was NOT mixed in.
    assert captured == ["https://files.slack.com/T1/F2/older.png"]


def test_edit_image_rejects_non_slack_host():
    """SSRF guard: any URL outside files*.slack.com must fail before any IO."""
    llm = MagicMock()
    client = MagicMock()
    client.token = "xoxb-token"
    ctx = _ctx(slack_client=client, llm=llm)

    with pytest.raises(ValueError, match="invalid Slack file URL"):
        edit_image(ctx, prompt="x", urls=["https://evil.example/cat.png"])
    llm.edit_image.assert_not_called()


def test_edit_image_rejects_http_scheme():
    llm = MagicMock()
    client = MagicMock()
    client.token = "xoxb-token"
    ctx = _ctx(slack_client=client, llm=llm)

    with pytest.raises(ValueError, match="invalid Slack file URL"):
        edit_image(ctx, prompt="x", urls=["http://files.slack.com/cat.png"])


def test_edit_image_raises_when_no_input_image():
    """No attached files, no explicit urls → raise so the LLM gets a clear
    error and can ask the user (or fall back to fetch_thread_history)."""
    llm = MagicMock()
    client = MagicMock()
    client.token = "xoxb-token"
    ctx = _ctx(event={}, slack_client=client, llm=llm)

    with pytest.raises(ValueError, match="no input image"):
        edit_image(ctx, prompt="hi")
    llm.edit_image.assert_not_called()


def test_edit_image_skips_non_image_attachments_when_using_event_files():
    """When falling back to event files, non-image mimetypes (PDFs, text)
    must be ignored so we don't hand a PDF to the image edit API."""
    llm = MagicMock()
    llm.edit_image.return_value = b"out"
    client = MagicMock()
    client.token = "xoxb-token"
    client.files_upload_v2.return_value = {"file": {"permalink": "p"}}
    event = {
        "files": [
            {"mimetype": "application/pdf", "url_private_download": "https://files.slack.com/T1/F0/doc.pdf"},
            {"mimetype": "image/jpeg", "url_private_download": "https://files.slack.com/T1/F1/photo.jpg"},
        ]
    }
    ctx = _ctx(event=event, slack_client=client, llm=llm)

    with patch("src.tools.image._http_get", return_value=_ok_response(b"jpg-bytes", mime="image/jpeg")):
        edit_image(ctx, prompt="enhance")

    images = llm.edit_image.call_args.args[1]
    assert images == [(b"jpg-bytes", "image/jpeg")]


def test_edit_image_respects_limit_for_multi_attachment():
    llm = MagicMock()
    llm.edit_image.return_value = b"out"
    client = MagicMock()
    client.token = "xoxb-token"
    client.files_upload_v2.return_value = {"file": {"permalink": "p"}}
    event = {
        "files": [
            {"mimetype": "image/png", "url_private_download": f"https://files.slack.com/T1/F{i}/img.png"}
            for i in range(5)
        ]
    }
    ctx = _ctx(event=event, slack_client=client, llm=llm)

    with patch("src.tools.image._http_get", return_value=_ok_response(b"x")):
        edit_image(ctx, prompt="combine", limit=2)

    images = llm.edit_image.call_args.args[1]
    assert len(images) == 2


def test_edit_image_uses_bot_token_for_download():
    """Image download must carry the bot token (Slack files require auth)."""
    llm = MagicMock()
    llm.edit_image.return_value = b"x"
    client = MagicMock()
    client.token = "xoxb-bot-token-here"
    client.files_upload_v2.return_value = {"file": {"permalink": "p"}}
    event = {
        "files": [
            {"mimetype": "image/png", "url_private_download": "https://files.slack.com/T1/F1/x.png"}
        ]
    }
    ctx = _ctx(event=event, slack_client=client, llm=llm)
    captured: dict = {}

    def _spy(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return _ok_response(b"png")

    with patch("src.tools.image._http_get", side_effect=_spy):
        edit_image(ctx, prompt="x")

    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lower["authorization"] == "Bearer xoxb-bot-token-here"


def test_edit_image_accepts_profile_image_url():
    """Profile image URLs from fetch_user_profile must be accepted via `urls`
    so the bot can edit a user's avatar."""
    llm = MagicMock()
    llm.edit_image.return_value = b"edited"
    client = MagicMock()
    client.token = "xoxb-bot"
    client.files_upload_v2.return_value = {"file": {"permalink": "https://slack/p"}}
    ctx = _ctx(slack_client=client, llm=llm)
    captured: dict = {}

    def _spy(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return _ok_response(b"avatar-bytes", mime="image/png")

    with patch("src.tools.image._http_get", side_effect=_spy):
        edit_image(
            ctx,
            prompt="make it pixel art",
            urls=["https://avatars.slack-edge.com/T1/U1/abc_512.png"],
        )

    assert captured["url"] == "https://avatars.slack-edge.com/T1/U1/abc_512.png"
    # Public CDN — no Authorization (would leak the bot token).
    assert captured["auth"] is None
    images = llm.edit_image.call_args.args[1]
    assert images == [(b"avatar-bytes", "image/png")]


def test_edit_image_accepts_gravatar_host():
    llm = MagicMock()
    llm.edit_image.return_value = b"x"
    client = MagicMock()
    client.token = "xoxb-bot"
    client.files_upload_v2.return_value = {"file": {"permalink": "p"}}
    ctx = _ctx(slack_client=client, llm=llm)

    with patch("src.tools.image._http_get", return_value=_ok_response(b"g", mime="image/png")):
        edit_image(
            ctx,
            prompt="x",
            urls=["https://secure.gravatar.com/avatar/deadbeef.png"],
        )
    llm.edit_image.assert_called_once()


# --------------------------------------------------------------------------- #
# attach_image_from_url
# --------------------------------------------------------------------------- #


def _public_dns(monkeypatch):
    """Make `_validate_public_https_url` resolve any host to a public IP."""
    monkeypatch.setattr(
        "src.tools.web.socket.getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )


def _ext_ctx(max_image_bytes: int = 10 * 1024 * 1024) -> ToolContext:
    client = MagicMock()
    client.token = "xoxb-bot"
    client.files_upload_v2.return_value = {
        "file": {
            "permalink": "https://slack/p",
            "url_private_download": "https://files.slack.com/T1/F1/x.png",
            "title": "title",
            "mimetype": "image/png",
        }
    }
    return ToolContext(
        slack_client=client,
        channel="C1",
        thread_ts="ts1",
        event={},
        settings=dataclasses.replace(_settings(), max_image_bytes=max_image_bytes),
        llm=MagicMock(),
    )


def _ext_response(body: bytes, mime: str = "image/png", content_length: int | None = None) -> MagicMock:
    headers = {"Content-Type": mime}
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    cm = MagicMock()
    cm.__enter__.return_value = cm
    cm.__exit__.return_value = False
    # _read_body_capped calls response.headers.get(...)
    cm.headers = MagicMock()
    cm.headers.get = lambda k, default=None: headers.get(k, default)
    cm.read.side_effect = lambda n=-1: body if n == -1 else body[:n]
    return cm


def test_attach_image_from_url_happy_path(monkeypatch):
    _public_dns(monkeypatch)
    ctx = _ext_ctx()
    # Real PNG magic — _fetch_external_image now sniffs body bytes so a
    # malicious server can't claim image/png while shipping HTML/SVG.
    png_body = b"\x89PNG\r\n\x1a\nrest-of-png"
    with patch(
        "src.tools.image._NoRedirectHandler",
    ), patch(
        "src.tools.image.urllib.request.build_opener"
    ) as build_opener:
        opener = MagicMock()
        opener.open.return_value = _ext_response(png_body, mime="image/png", content_length=len(png_body))
        build_opener.return_value = opener
        out = attach_image_from_url(ctx, url="https://example.com/cat.png")

    assert out == {
        "permalink": "https://slack/p",
        "url_private_download": "https://files.slack.com/T1/F1/x.png",
        "title": "title",
        "mimetype": "image/png",
    }
    upload_kwargs = ctx.slack_client.files_upload_v2.call_args.kwargs
    assert upload_kwargs["channel"] == "C1"
    assert upload_kwargs["thread_ts"] == "ts1"
    assert upload_kwargs["filename"] == "cat.png"
    assert upload_kwargs["file"] == png_body


def test_attach_image_from_url_rejects_http_scheme():
    """SSRF guard: http:// is not allowed even if everything else looks fine."""
    ctx = _ext_ctx()
    with pytest.raises(ValueError, match="https"):
        attach_image_from_url(ctx, url="http://example.com/cat.png")
    ctx.slack_client.files_upload_v2.assert_not_called()


def test_attach_image_from_url_rejects_ip_literal():
    ctx = _ext_ctx()
    with pytest.raises(ValueError, match="IP literals"):
        attach_image_from_url(ctx, url="https://10.0.0.1/x.png")
    ctx.slack_client.files_upload_v2.assert_not_called()


def test_attach_image_from_url_rejects_private_dns(monkeypatch):
    """DNS that resolves to a private/loopback range must fail before any IO."""
    monkeypatch.setattr(
        "src.tools.web.socket.getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("169.254.169.254", 443))],  # AWS metadata
    )
    ctx = _ext_ctx()
    with pytest.raises(ValueError, match="non-public"):
        attach_image_from_url(ctx, url="https://internal.example/cat.png")
    ctx.slack_client.files_upload_v2.assert_not_called()


def test_attach_image_from_url_rejects_non_image_content_type(monkeypatch):
    _public_dns(monkeypatch)
    ctx = _ext_ctx()
    with patch("src.tools.image.urllib.request.build_opener") as build_opener:
        opener = MagicMock()
        opener.open.return_value = _ext_response(b"<html></html>", mime="text/html", content_length=13)
        build_opener.return_value = opener
        with pytest.raises(ValueError, match="did not return an image"):
            attach_image_from_url(ctx, url="https://example.com/page")
    ctx.slack_client.files_upload_v2.assert_not_called()


def test_attach_image_from_url_rejects_oversize_via_content_length(monkeypatch):
    _public_dns(monkeypatch)
    ctx = _ext_ctx(max_image_bytes=1000)
    with patch("src.tools.image.urllib.request.build_opener") as build_opener:
        opener = MagicMock()
        opener.open.return_value = _ext_response(
            b"x" * 10, mime="image/png", content_length=5000
        )
        build_opener.return_value = opener
        with pytest.raises(ValueError, match="MAX_WEB_BYTES"):
            attach_image_from_url(ctx, url="https://example.com/big.png")
    ctx.slack_client.files_upload_v2.assert_not_called()


def test_attach_image_from_url_filename_falls_back_when_url_has_no_extension(monkeypatch):
    _public_dns(monkeypatch)
    ctx = _ext_ctx()
    # Real JPEG SOI magic — body must pass _detect_image_mime sniffing.
    jpg_body = b"\xff\xd8\xff\xe0jpg-payload"
    with patch("src.tools.image.urllib.request.build_opener") as build_opener:
        opener = MagicMock()
        opener.open.return_value = _ext_response(jpg_body, mime="image/jpeg", content_length=len(jpg_body))
        build_opener.return_value = opener
        attach_image_from_url(ctx, url="https://example.com/render?id=42")

    upload_kwargs = ctx.slack_client.files_upload_v2.call_args.kwargs
    # No usable basename → fall back to mime-based name.
    assert upload_kwargs["filename"] == "image.jpg"


def test_attach_image_from_url_uses_no_redirect_handler(monkeypatch):
    """A 3xx redirect to a private host would defeat the SSRF guard. The
    fetch must use _NoRedirectHandler (raises HTTPError on any 3xx)."""
    _public_dns(monkeypatch)
    ctx = _ext_ctx()
    with patch("src.tools.image.urllib.request.build_opener") as build_opener:
        # Return a real opener whose open() raises HTTPError to simulate
        # what _NoRedirectHandler.redirect_request does on a 302.
        import urllib.error

        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            url="https://example.com/cat.png",
            code=302,
            msg="redirects not allowed",
            hdrs=None,
            fp=None,
        )
        build_opener.return_value = opener
        with pytest.raises(ValueError, match="image download failed"):
            attach_image_from_url(ctx, url="https://example.com/cat.png")
    # Verify _NoRedirectHandler was actually wired into the opener (not
    # silently swapped for a permissive handler later).
    handler_arg = build_opener.call_args.args[0]
    from src.tools.web import _NoRedirectHandler
    assert isinstance(handler_arg, _NoRedirectHandler)


def test_image_http_get_uses_slack_redirect_handler():
    """edit_image's Slack fetch path must use _SlackRedirectHandler so
    Slack-internal 302s (signed CDN refresh) are followed. The strict
    _NoRedirectHandler stays reserved for external URL fetches."""
    import urllib.request

    from src.tools import image as _image_mod
    from src.tools.slack import _SlackRedirectHandler

    with patch("src.tools.image.urllib.request.build_opener") as build_opener:
        build_opener.return_value = MagicMock()
        _image_mod._http_get(urllib.request.Request("https://files.slack.com/x.png"))
    handler_arg = build_opener.call_args.args[0]
    assert isinstance(handler_arg, _SlackRedirectHandler)
