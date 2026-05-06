"""Tests for src.tools.image."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.tools._helpers import _ctx
from src.tools.image import edit_image, generate_image


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

    with patch("src.tools.image.urllib.request.urlopen", return_value=_ok_response(b"raw-png")):
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

    with patch("src.tools.image.urllib.request.urlopen", side_effect=_spy_urlopen):
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

    with patch("src.tools.image.urllib.request.urlopen", return_value=_ok_response(b"jpg-bytes", mime="image/jpeg")):
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

    with patch("src.tools.image.urllib.request.urlopen", return_value=_ok_response(b"x")):
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

    with patch("src.tools.image.urllib.request.urlopen", side_effect=_spy):
        edit_image(ctx, prompt="x")

    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lower["authorization"] == "Bearer xoxb-bot-token-here"
