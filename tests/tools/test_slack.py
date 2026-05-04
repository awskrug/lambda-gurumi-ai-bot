"""Tests for src.tools.slack."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.tools._helpers import _ctx, _settings
from src.tools.registry import ToolContext, ToolExecutor
from src.tools.slack import (
    fetch_thread_history,
    read_attached_document,
    read_attached_images,
)


# --------------------------------------------------------------------------- #
# read_attached_images SSRF guard
# --------------------------------------------------------------------------- #


def test_read_attached_images_rejects_non_slack_host():
    event = {"files": [{"mimetype": "image/png", "url_private_download": "https://evil.example.com/x.png"}]}
    with pytest.raises(ValueError):
        read_attached_images(_ctx(event=event), limit=1)


def test_read_attached_images_rejects_http_scheme():
    event = {"files": [{"mimetype": "image/png", "url_private_download": "http://files.slack.com/x.png"}]}
    with pytest.raises(ValueError):
        read_attached_images(_ctx(event=event), limit=1)


def test_read_attached_images_accepts_slack_host_variants():
    event = {
        "files": [
            {"mimetype": "image/png", "url_private_download": "https://files-pri.slack.com/x.png", "name": "a"},
        ]
    }
    llm = MagicMock()
    llm.describe_image.return_value = "a cat"
    ctx = _ctx(event=event, llm=llm)
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        opener.return_value.__enter__.return_value.read.return_value = b"fake"
        result = read_attached_images(ctx, limit=1)
    assert result == [{"name": "a", "summary": "a cat"}]


def test_read_attached_images_skips_non_image_mimetypes():
    event = {"files": [{"mimetype": "application/pdf", "url_private_download": "https://files.slack.com/x.pdf"}]}
    assert read_attached_images(_ctx(event=event), limit=1) == []


# --------------------------------------------------------------------------- #
# fetch_thread_history
# --------------------------------------------------------------------------- #


def test_fetch_thread_history_resolves_user_files_and_reactions():
    """History should carry display names, file metadata, and reactions so the
    LLM can answer things like "누가 좋아요 눌렀어?" or "아까 그 이미지 분석해줘"."""
    from src.slack_helpers import user_name_cache

    # Reset the module-level cache so prior tests don't leak.
    user_name_cache._cache.clear()

    client = MagicMock()
    client.conversations_replies.return_value = {
        "messages": [
            {
                "user": "U1",
                "text": "look at this",
                "ts": "1713.1",
                "files": [
                    {
                        "name": "cat.png",
                        "mimetype": "image/png",
                        "url_private_download": "https://files.slack.com/x/cat.png",
                        "permalink": "https://slack/p1",
                        "title": "cute",
                    }
                ],
            },
            {
                "user": "U2",
                "text": "nice!",
                "ts": "1713.2",
                "reactions": [
                    {"name": "thumbsup", "count": 2, "users": ["U1", "U3"]},
                ],
            },
        ]
    }

    def _users_info(user):
        return {"user": {"profile": {"display_name": f"name-{user}"}}}

    client.users_info.side_effect = _users_info

    out = fetch_thread_history(_ctx(slack_client=client), limit=5)
    assert len(out) == 2
    first, second = out
    assert first["user"] == "name-U1"
    assert first["text"] == "look at this"
    assert first["ts"] == "1713.1"
    assert first["files"] == [
        {
            "name": "cat.png",
            "mimetype": "image/png",
            "url_private_download": "https://files.slack.com/x/cat.png",
            "permalink": "https://slack/p1",
            "title": "cute",
        }
    ]
    assert first["reactions"] == []

    assert second["user"] == "name-U2"
    assert second["files"] == []
    assert second["reactions"] == [
        {"emoji": "thumbsup", "count": 2, "users": ["name-U1", "name-U3"]}
    ]


def test_fetch_thread_history_resolves_users_concurrently():
    """fetch_thread_history must prefetch user names in parallel via
    UserNameCache.warm. Without this, a thread of N unique users + R
    reacters becomes N+R serial users_info calls and blows the tool
    timeout on cold caches."""
    import threading
    import time as _time

    from src.slack_helpers import user_name_cache

    user_name_cache._cache.clear()

    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def _slow_users_info(user):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        _time.sleep(0.2)
        with lock:
            in_flight -= 1
        return {"user": {"profile": {"display_name": f"name-{user}"}}}

    client = MagicMock()
    client.users_info.side_effect = _slow_users_info
    client.conversations_replies.return_value = {
        "messages": [
            {"user": f"U{i}", "text": "hi", "ts": f"100.{i}"}
            for i in range(4)
        ]
    }

    started = _time.monotonic()
    out = fetch_thread_history(_ctx(slack_client=client), limit=10)
    elapsed = _time.monotonic() - started

    assert len(out) == 4
    assert peak >= 2, f"expected concurrent users_info, peak={peak}"
    assert elapsed < 0.6, f"expected parallel ~0.2s, took {elapsed:.2f}s"
    assert {item["user"] for item in out} == {f"name-U{i}" for i in range(4)}


def test_read_attached_images_accepts_extra_urls():
    """Images referenced from fetch_thread_history (url_private_download) must
    be loadable via read_attached_images(urls=[...])."""
    ctx = _ctx()
    ctx.llm.describe_image.return_value = "a cat history"
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        opener.return_value.__enter__.return_value.read.return_value = b"fake-bytes"
        out = read_attached_images(
            ctx,
            limit=5,
            urls=["https://files.slack.com/x/cat.png"],
        )
    assert out == [{"name": "cat.png", "summary": "a cat history"}]


def test_read_attached_images_urls_reject_non_slack_host():
    ctx = _ctx()
    with pytest.raises(ValueError):
        read_attached_images(ctx, urls=["https://evil.example.com/cat.png"])


def test_read_attached_images_uses_per_app_token_from_slack_client():
    """Regression: the Authorization header must carry the per-app bot_token
    that the worker resolved from SSM (and put on the WebClient passed into
    ToolContext) — NOT `settings.slack_bot_token`, which is empty in the
    Lambda runtime under the multi-tenant config."""
    import dataclasses

    slack_client = MagicMock()
    slack_client.token = "xoxb-per-app-from-ssm"
    # Mirror the runtime: settings.slack_bot_token is empty.
    ctx = ToolContext(
        slack_client=slack_client,
        channel="C1",
        thread_ts="ts1",
        event={},
        settings=dataclasses.replace(_settings(), slack_bot_token=""),
        llm=MagicMock(describe_image=MagicMock(return_value="x")),
    )

    captured: dict[str, str] = {}

    def _capture_request(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = b"img-bytes"
        return cm

    with patch("src.tools.slack.urllib.request.urlopen", side_effect=_capture_request):
        read_attached_images(ctx, urls=["https://files.slack.com/x/cat.png"])

    assert captured["auth"] == "Bearer xoxb-per-app-from-ssm"


def test_read_attached_document_uses_per_app_token_from_slack_client():
    """Regression: same contract as the images path — token comes from the
    WebClient on ToolContext, not from settings."""
    import dataclasses

    slack_client = MagicMock()
    slack_client.token = "xoxb-per-app-from-ssm"
    ctx = ToolContext(
        slack_client=slack_client,
        channel="C1",
        thread_ts="ts1",
        event={
            "files": [
                {
                    "mimetype": "text/plain",
                    "url_private_download": "https://files.slack.com/x/doc.txt",
                    "name": "doc.txt",
                }
            ]
        },
        settings=dataclasses.replace(_settings(), slack_bot_token=""),
        llm=MagicMock(),
    )

    captured: dict[str, str] = {}

    def _capture_request(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        cm = MagicMock()
        resp = cm.__enter__.return_value
        resp.headers.get.return_value = "text/plain"
        resp.read.side_effect = [b"hello world", b""]  # body chunk, then EOF
        return cm

    with patch("src.tools.slack.urllib.request.urlopen", side_effect=_capture_request):
        read_attached_document(ctx, limit=1)

    assert captured["auth"] == "Bearer xoxb-per-app-from-ssm"


def test_read_attached_images_runs_describes_in_parallel():
    """The describe step is the slow one (LLM call). Three images that each
    take 0.3s to describe should finish in well under the serial 0.9s if the
    pool is actually parallel. Guards against accidentally re-serializing the
    fetch loop (the original bug)."""
    import threading
    import time as _time

    event = {
        "files": [
            {
                "mimetype": "image/png",
                "url_private_download": f"https://files.slack.com/img{i}.png",
                "name": f"img{i}.png",
            }
            for i in range(3)
        ]
    }
    ctx = _ctx(event=event)

    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def _slow_describe(_data, _mime):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        _time.sleep(0.3)
        with lock:
            in_flight -= 1
        return "described"

    ctx.llm.describe_image.side_effect = _slow_describe

    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        opener.return_value.__enter__.return_value.read.return_value = b"x"
        started = _time.monotonic()
        out = read_attached_images(ctx, limit=3)
        elapsed = _time.monotonic() - started

    assert len(out) == 3
    assert peak >= 2, f"expected concurrent describes, peak={peak}"
    assert elapsed < 0.7, f"expected parallel ~0.3s, took {elapsed:.2f}s"


def test_read_attached_images_preserves_order():
    """Output order must match candidate order (event files first, then urls),
    independent of which describe call finishes first."""
    import threading
    import time as _time

    event = {
        "files": [
            {
                "mimetype": "image/png",
                "url_private_download": "https://files.slack.com/event.png",
                "name": "event.png",
            }
        ]
    }
    ctx = _ctx(event=event)

    delays = {b"event": 0.2, b"extra": 0.0}
    barrier = threading.Event()

    def _describe(data, _mime):
        # Force the extra image to finish first so we can verify ordering
        # comes from the candidate index, not completion order.
        _time.sleep(delays.get(data, 0.0))
        if data == b"extra":
            barrier.set()
        return f"sum-{data.decode()}"

    ctx.llm.describe_image.side_effect = _describe

    def _open(req, timeout=15):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else req
        body = b"event" if "event.png" in url else b"extra"
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = body
        return cm

    with patch("src.tools.slack.urllib.request.urlopen", side_effect=_open):
        out = read_attached_images(
            ctx,
            limit=3,
            urls=["https://files.slack.com/extra.png"],
        )

    assert [item["name"] for item in out] == ["event.png", "extra.png"]


def test_read_attached_images_respects_total_limit_across_event_and_urls():
    event = {
        "files": [
            {
                "mimetype": "image/png",
                "url_private_download": "https://files.slack.com/e1.png",
                "name": "e1.png",
            }
        ]
    }
    ctx = _ctx(event=event)
    ctx.llm.describe_image.return_value = "desc"
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        opener.return_value.__enter__.return_value.read.return_value = b"x"
        out = read_attached_images(
            ctx,
            limit=2,
            urls=[
                "https://files.slack.com/u1.png",
                "https://files.slack.com/u2.png",  # should be skipped (limit=2)
            ],
        )
    assert len(out) == 2
    assert {item["name"] for item in out} == {"e1.png", "u1.png"}


# --------------------------------------------------------------------------- #
# read_attached_document
# --------------------------------------------------------------------------- #


def test_read_attached_document_runs_in_parallel():
    """Multiple text documents should download concurrently. The serial
    implementation took ~3 * download_time; parallel should finish in
    roughly download_time. Guards against re-serializing the loop."""
    import threading
    import time as _time

    event = {
        "files": [
            {
                "mimetype": "text/plain",
                "url_private_download": f"https://files.slack.com/d{i}.txt",
                "name": f"d{i}.txt",
            }
            for i in range(3)
        ]
    }
    ctx = _ctx(event=event)

    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def _slow_open(_req, timeout=15):  # noqa: ARG001
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        _time.sleep(0.3)
        with lock:
            in_flight -= 1
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = b"hello"
        cm.__enter__.return_value.headers = {"Content-Length": "5", "Content-Type": "text/plain"}
        return cm

    with patch("src.tools.slack.urllib.request.urlopen", side_effect=_slow_open):
        started = _time.monotonic()
        out = read_attached_document(ctx, limit=3)
        elapsed = _time.monotonic() - started

    assert len(out) == 3
    assert peak >= 2, f"expected concurrent fetches, peak={peak}"
    assert elapsed < 0.7, f"expected parallel ~0.3s, took {elapsed:.2f}s"


def test_read_attached_document_preserves_order_under_parallel_completion():
    """Output order must follow candidate order, not completion order."""
    import time as _time

    event = {
        "files": [
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/slow.txt",
                "name": "slow.txt",
            },
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/fast.txt",
                "name": "fast.txt",
            },
        ]
    }
    ctx = _ctx(event=event)

    def _open(req, timeout=15):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else req
        delay = 0.2 if "slow.txt" in url else 0.0
        _time.sleep(delay)
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = b"x"
        cm.__enter__.return_value.headers = {"Content-Length": "1", "Content-Type": "text/plain"}
        return cm

    with patch("src.tools.slack.urllib.request.urlopen", side_effect=_open):
        out = read_attached_document(ctx, limit=2)

    assert [item["name"] for item in out] == ["slow.txt", "fast.txt"]


def test_read_attached_document_text_file():
    event = {
        "files": [
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/notes.txt",
                "name": "notes.txt",
            }
        ]
    }
    ctx = _ctx(event=event)
    body = b"Hello\n  world.\nLine 3."
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        resp = opener.return_value.__enter__.return_value
        resp.read.return_value = body
        resp.headers = {"Content-Length": str(len(body))}
        out = read_attached_document(ctx, limit=1)
    assert len(out) == 1
    entry = out[0]
    assert entry["name"] == "notes.txt"
    assert entry["mimetype"] == "text/plain"
    assert entry["truncated"] is False
    assert "Hello" in entry["text"]
    assert entry["chars"] == len(entry["text"])
    assert entry["pages"] == 0  # text files report 0 pages


def _build_pdf_bytes(pages_text: list[str]) -> bytes:
    """Build a minimal PDF (one page per string) using reportlab. Test-only."""
    from io import BytesIO
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.pagesizes import letter

    buf = BytesIO()
    canvas = Canvas(buf, pagesize=letter)
    for text in pages_text:
        canvas.drawString(72, 720, text)
        canvas.showPage()
    canvas.save()
    return buf.getvalue()


def _mock_pdf_response(opener, body: bytes, headers=None):
    """Wire the urlopen mock to stream `body` in chunks through `_fetch_slack_file`."""
    resp = opener.return_value.__enter__.return_value
    buf = {"pos": 0}

    def _chunked(n=-1):
        if n == -1:
            remaining = body[buf["pos"]:]
            buf["pos"] = len(body)
            return remaining
        chunk = body[buf["pos"]:buf["pos"] + n]
        buf["pos"] += len(chunk)
        return chunk

    resp.read.side_effect = _chunked
    resp.headers = dict(headers or {"Content-Length": str(len(body)), "Content-Type": "application/pdf"})


def test_read_attached_document_pdf_happy_path():
    pdf = _build_pdf_bytes(["Hello PDF page one.", "Page two here."])
    event = {
        "files": [
            {
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/report.pdf",
                "name": "report.pdf",
            }
        ]
    }
    ctx = _ctx(event=event)
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        _mock_pdf_response(opener, pdf)
        out = read_attached_document(ctx, limit=1)
    assert len(out) == 1
    entry = out[0]
    assert entry["name"] == "report.pdf"
    assert entry["pages"] == 2
    assert entry["truncated"] is False
    assert entry["chars"] > 0


def test_read_attached_document_pdf_truncation():
    pdf = _build_pdf_bytes(["A" * 500])
    event = {
        "files": [
            {
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/big.pdf",
                "name": "big.pdf",
            }
        ]
    }
    ctx = _ctx(
        event=event,
    )
    ctx = ToolContext(
        slack_client=ctx.slack_client,
        channel=ctx.channel,
        thread_ts=ctx.thread_ts,
        event=ctx.event,
        settings=_settings(max_doc_chars=50),
        llm=ctx.llm,
    )
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        _mock_pdf_response(opener, pdf)
        out = read_attached_document(ctx, limit=1)
    assert out[0]["truncated"] is True
    assert out[0]["chars"] == 50


def test_read_attached_document_page_cap():
    pdf = _build_pdf_bytes(["p1", "p2", "p3"])
    event = {
        "files": [
            {
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/pages.pdf",
                "name": "pages.pdf",
            }
        ]
    }
    ctx = ToolContext(
        slack_client=MagicMock(),
        channel="C1",
        thread_ts="ts1",
        event=event,
        settings=_settings(max_doc_pages=2),
        llm=MagicMock(),
    )
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        _mock_pdf_response(opener, pdf)
        out = read_attached_document(ctx, limit=1)
    assert "error" in out[0]
    assert "MAX_DOC_PAGES" in out[0]["error"]


def test_read_attached_document_size_cap_via_content_length():
    event = {
        "files": [
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/huge.txt",
                "name": "huge.txt",
            }
        ]
    }
    ctx = ToolContext(
        slack_client=MagicMock(),
        channel="C1",
        thread_ts="ts1",
        event=event,
        settings=_settings(max_doc_bytes=100),  # tiny cap
        llm=MagicMock(),
    )
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        resp = opener.return_value.__enter__.return_value
        resp.headers = {"Content-Length": "200"}  # > cap
        resp.read.return_value = b"x" * 10  # should never be read past cap
        out = read_attached_document(ctx, limit=1)
    assert "error" in out[0]
    assert "MAX_DOC_BYTES" in out[0]["error"]


def test_read_attached_document_size_cap_via_streamed_read():
    event = {
        "files": [
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/nohead.txt",
                "name": "nohead.txt",
            }
        ]
    }
    ctx = ToolContext(
        slack_client=MagicMock(),
        channel="C1",
        thread_ts="ts1",
        event=event,
        settings=_settings(max_doc_bytes=100),
        llm=MagicMock(),
    )
    body = b"y" * 200
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        resp = opener.return_value.__enter__.return_value
        resp.headers = {}  # no Content-Length
        buf = {"pos": 0}

        def _chunked(n=-1):
            if n == -1:
                remaining = body[buf["pos"]:]
                buf["pos"] = len(body)
                return remaining
            chunk = body[buf["pos"]:buf["pos"] + n]
            buf["pos"] += len(chunk)
            return chunk

        resp.read.side_effect = _chunked
        out = read_attached_document(ctx, limit=1)
    assert "error" in out[0]
    assert "MAX_DOC_BYTES" in out[0]["error"]


def test_read_attached_document_rejects_non_slack_host():
    ctx = _ctx()
    out = read_attached_document(
        ctx, urls=["https://evil.example.com/foo.pdf"], limit=1
    )
    assert len(out) == 1
    assert "error" in out[0]
    assert "invalid" in out[0]["error"].lower()


def test_read_attached_document_skips_encrypted_pdf():
    from io import BytesIO
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    # NOTE: pypdf>=4.0 uses keyword-only user_password. If requirements.txt's
    # upper pin is ever relaxed past 6.0, verify this signature still holds.
    writer.encrypt(user_password="secret")
    buf = BytesIO()
    writer.write(buf)
    encrypted_pdf = buf.getvalue()

    event = {
        "files": [
            {
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/enc.pdf",
                "name": "enc.pdf",
            }
        ]
    }
    ctx = _ctx(event=event)
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        _mock_pdf_response(opener, encrypted_pdf)
        out = read_attached_document(ctx, limit=1)
    assert "error" in out[0]
    assert "encrypted" in out[0]["error"]


def test_read_attached_document_skips_image_mime():
    event = {
        "files": [
            {
                "mimetype": "image/png",
                "url_private_download": "https://files.slack.com/a.png",
                "name": "a.png",
            }
        ]
    }
    ctx = _ctx(event=event)
    # urlopen should NOT be called — image MIMEs are filtered before fetch
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        out = read_attached_document(ctx, limit=1)
    opener.assert_not_called()
    assert out == []


def test_read_attached_document_http_error_returns_per_item():
    import urllib.error

    event = {
        "files": [
            {
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/missing.pdf",
                "name": "missing.pdf",
            }
        ]
    }
    ctx = _ctx(event=event)
    with patch("src.tools.slack.urllib.request.urlopen") as opener:
        opener.side_effect = urllib.error.HTTPError(
            url="https://files.slack.com/missing.pdf",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )
        out = read_attached_document(ctx, limit=1)
    assert len(out) == 1
    assert "error" in out[0]
    assert "404" in out[0]["error"]
