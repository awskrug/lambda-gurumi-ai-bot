"""Image generation + editing tools.

`generate_image` calls the configured image provider with a prompt only.
`edit_image` additionally supplies one or more input images sourced from
the triggering Slack mention's attachments or from explicit URLs (e.g.
`url_private_download` values returned by `fetch_thread_history`).
`attach_image_from_url` downloads a public web image into the Slack
thread so it becomes a Slack-hosted file the other tools can reuse.

All tools upload the result to the current thread and return the
permalink.
"""
from __future__ import annotations

import logging
import socket
import urllib.error
import urllib.parse
import urllib.request

from src.tools.registry import ToolContext, default_registry, tool
from src.tools.slack import SLACK_FILE_HOSTS, SLACK_IMAGE_HOSTS
# SSRF guard primitives live in src.tools.web — sibling-module import is
# fine within the tools package and avoids duplicating the validation
# logic. The `_`-prefixed names are stable per CLAUDE.md ("load-bearing").
from src.tools.web import (
    _NoRedirectHandler,
    _read_body_capped,
    _validate_public_https_url,
)

logger = logging.getLogger(__name__)


@tool(
    default_registry,
    name="generate_image",
    description="Generate an image from a prompt and upload it to the Slack thread. Returns the permalink.",
    parameters={
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
    },
    timeout=240.0,  # gpt-image-2 / titan / stability can take 60–180s; Lambda caps at 300s, leaves ~60s for compose + upload
)
def generate_image(ctx: ToolContext, prompt: str) -> dict[str, str]:
    image_bytes = ctx.llm.generate_image(prompt)
    return _upload_to_thread(ctx, image_bytes, "generated.png")


@tool(
    default_registry,
    name="attach_image_from_url",
    description=(
        "Download a public web image and attach it to the Slack thread. "
        "Use this to bring an external image (e.g. a result from "
        "search_images) into the conversation — the upload makes it "
        "Slack-hosted so subsequent tools (edit_image, read_attached_images) "
        "can reference it by its files.slack.com URL. Returns the Slack "
        "permalink and url_private_download. Rejects non-https URLs, "
        "non-image content types, and oversize payloads (cap from "
        "MAX_IMAGE_BYTES)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute https URL of the image to attach.",
            },
            "title": {
                "type": "string",
                "description": "Optional Slack file title shown above the upload.",
            },
        },
        "required": ["url"],
    },
    # External fetch (12s) + Slack files_upload_v2 (multi-second). 30s
    # matches the budget of fetch_webpage so concurrent calls share a
    # similar deadline.
    timeout=30.0,
)
def attach_image_from_url(
    ctx: ToolContext,
    url: str,
    title: str | None = None,
) -> dict[str, str]:
    body, mime, filename = _fetch_external_image(url, ctx.settings.max_image_bytes)
    upload = ctx.slack_client.files_upload_v2(
        channel=ctx.channel,
        thread_ts=ctx.thread_ts,
        title=title or filename,
        filename=filename,
        file=body,
    )
    file_info = upload.get("file", {}) if isinstance(upload, dict) else {}
    return {
        "permalink": file_info.get("permalink", ""),
        "url_private_download": file_info.get("url_private_download", "")
        or file_info.get("url_private", ""),
        "title": file_info.get("title", title or filename),
        "mimetype": file_info.get("mimetype", mime),
    }


@tool(
    default_registry,
    name="edit_image",
    description=(
        "Edit existing image(s) with a text prompt and upload the result to "
        "the Slack thread. By default uses images attached to the current "
        "Slack mention. To edit images from earlier in the thread, first "
        "call fetch_thread_history, then pass the desired "
        "`files[*].url_private_download` values via `urls`. To edit a "
        "user's profile image, first call fetch_user_profile and pass the "
        "returned `image_url` via `urls`. To edit a web-search result, "
        "first call attach_image_from_url to bring it into Slack, then "
        "pass the returned url_private_download via `urls`. URLs must be "
        "on files*.slack.com or a Slack profile image host "
        "(avatars.slack-edge.com, a.slack-edge.com, secure.gravatar.com). "
        "Use this — not generate_image — whenever the user wants to "
        "transform, restyle, or modify an existing image. Not supported "
        "when IMAGE_PROVIDER=bedrock; the tool returns an error in that "
        "case so you can fall back to a text reply."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Editing instructions describing the desired change.",
            },
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional input image URLs. Must be on files*.slack.com "
                    "or a Slack profile image host (avatars.slack-edge.com, "
                    "a.slack-edge.com, secure.gravatar.com). If omitted, "
                    "uses images attached to the current Slack mention."
                ),
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
        },
        "required": ["prompt"],
    },
    # Edit can take longer than generate (input upload + edit). Keep the
    # same 240s ceiling — Lambda has 300s total, leaving ~60s for compose.
    timeout=240.0,
)
def edit_image(
    ctx: ToolContext,
    prompt: str,
    urls: list[str] | None = None,
    limit: int = 2,
) -> dict[str, str]:
    images = _collect_input_images(ctx, urls=urls, limit=limit)
    if not images:
        raise ValueError(
            "no input image found — attach an image to the message, or pass "
            "`urls` from fetch_thread_history."
        )
    image_bytes = ctx.llm.edit_image(prompt, images)
    return _upload_to_thread(ctx, image_bytes, "edited.png")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _upload_to_thread(ctx: ToolContext, image_bytes: bytes, filename: str) -> dict[str, str]:
    upload = ctx.slack_client.files_upload_v2(
        channel=ctx.channel,
        thread_ts=ctx.thread_ts,
        title=ctx.settings.image_model,
        filename=filename,
        file=image_bytes,
    )
    file_info = upload.get("file", {})
    return {
        "permalink": file_info.get("permalink", ""),
        "title": file_info.get("title", filename),
    }


def _collect_input_images(
    ctx: ToolContext,
    urls: list[str] | None,
    limit: int,
) -> list[tuple[bytes, str]]:
    """Resolve input images for edit_image.

    Order of precedence:
      1. Explicit `urls` argument — typically Slack file URLs the LLM
         pulled from `fetch_thread_history`.
      2. Image files attached to the triggering mention (`ctx.event.files`).

    Each URL is validated against the Slack-file host allowlist (SSRF
    guard) and downloaded with the per-app bot token before being handed
    to the provider as `(bytes, mime_type)` tuples.
    """
    token = ctx.slack_client.token
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []  # (url, mime_hint)

    # Explicit URLs win — caller is being deliberate about which image to edit.
    for extra in (urls or []):
        if len(candidates) >= limit:
            break
        if extra in seen:
            continue
        seen.add(extra)
        candidates.append((extra, ""))

    # Fall back to images on the triggering message only when the LLM did
    # not pass any URLs. Mixing both is rarely what the user means.
    if not candidates:
        for file_info in (ctx.event.get("files") or []):
            if len(candidates) >= limit:
                break
            mime = str(file_info.get("mimetype", ""))
            if not mime.startswith("image/"):
                continue
            dl = file_info.get("url_private_download") or file_info.get("url_private")
            if not dl or dl in seen:
                continue
            seen.add(dl)
            candidates.append((dl, mime))

    # Pre-flight SSRF check — fail loud BEFORE any network IO so the LLM
    # gets a clear "bad URL" error instead of a partial fetch.
    for url, _ in candidates:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in SLACK_IMAGE_HOSTS:
            raise ValueError(f"invalid Slack file URL: {url}")

    out: list[tuple[bytes, str]] = []
    for url, mime_hint in candidates:
        body, header_mime = _fetch_slack_image(url, token)
        mime = (
            header_mime if header_mime.startswith("image/")
            else (mime_hint if mime_hint.startswith("image/") else _guess_image_mime(url))
        )
        out.append((body, mime))
    return out


def _fetch_slack_image(url: str, token: str) -> tuple[bytes, str]:
    """Download a Slack-hosted image. Returns (bytes, mime).

    Sends the bot token only for files*.slack.com (private-by-default).
    Profile-image hosts (avatars.slack-edge.com / secure.gravatar.com)
    are public CDN — sending Authorization there is unnecessary and could
    leak the token.

    Caller must validate `url` against SLACK_IMAGE_HOSTS first.
    """
    headers: dict[str, str] = {}
    if urllib.parse.urlparse(url).hostname in SLACK_FILE_HOSTS:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310 (host allowlisted upstream)
        body = response.read()
        header_mime = (
            (response.headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
            if response.headers else ""
        )
    return body, header_mime


def _guess_image_mime(url: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for ext, mime in (
        (".png", "image/png"),
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
    ):
        if path.endswith(ext):
            return mime
    return "image/png"


_IMAGE_FETCH_TIMEOUT = 12  # parallel to web._WEB_FETCH_TIMEOUT — same trade-offs


def _fetch_external_image(url: str, max_bytes: int) -> tuple[bytes, str, str]:
    """Download an image from an arbitrary public https URL.

    Layered defenses (each maps to a real attack class — do not loosen
    without re-reading the comment):

      1. ``_validate_public_https_url`` — DNS resolves to a public unicast
         address. Blocks SSRF to RFC1918 / loopback / metadata services.
      2. ``_NoRedirectHandler`` — a 3xx pointing at a private host would
         silently bypass the pre-flight DNS check, so refuse redirects.
      3. ``_read_body_capped`` — Content-Length and streamed-read size
         caps. Defends against giant payloads burning Lambda time.
      4. Content-Type allowlist — must be ``image/*``. Stops obvious
         content sniffing surprises (HTML, JSON, etc.).

    Returns (body, mime, filename).
    """
    _validate_public_https_url(url)
    opener = urllib.request.build_opener(_NoRedirectHandler())
    req = urllib.request.Request(url, headers={"User-Agent": "lambda-gurumi-bot/1.0"})
    try:
        with opener.open(req, timeout=_IMAGE_FETCH_TIMEOUT) as response:  # noqa: S310 (URL pre-validated, redirects disabled)
            body = _read_body_capped(response, max_bytes)
            header_mime = (
                (response.headers.get("Content-Type", "") or "")
                .split(";", 1)[0]
                .strip()
                .lower()
                if response.headers
                else ""
            )
    except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout) as exc:
        raise ValueError(f"image download failed: {exc.__class__.__name__}: {exc}") from exc
    if not header_mime.startswith("image/"):
        # Header lies cheaply, but if even the header doesn't claim image/*
        # we reject — uploading non-image bytes to Slack would surface as
        # a broken file in the thread.
        raise ValueError(
            f"URL did not return an image (Content-Type={header_mime or 'missing'})"
        )
    filename = _filename_from_url(url, header_mime)
    return body, header_mime, filename


def _filename_from_url(url: str, mime: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = path.rsplit("/", 1)[-1] if path else ""
    name = name.split("?", 1)[0].split("#", 1)[0]
    if name and "." in name:
        return name
    ext = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(mime, "png")
    return f"image.{ext}"
