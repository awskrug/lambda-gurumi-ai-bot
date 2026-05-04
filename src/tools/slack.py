"""Slack-centric tools: read images/documents attached to the triggering
mention, fetch the current thread's history."""
from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from slack_sdk.errors import SlackApiError

from src.slack_helpers import user_name_cache
from src.tools.registry import ToolContext, default_registry, tool

logger = logging.getLogger(__name__)

SLACK_FILE_HOSTS = {"files.slack.com", "files-edge.slack.com", "files-pri.slack.com"}
DOC_TEXT_PREFIX = "text/"
DOC_PDF_MIME = "application/pdf"


@tool(
    default_registry,
    name="read_attached_images",
    description=(
        "Read image files and return textual descriptions. By default reads "
        "images attached to the current Slack mention. Pass `urls` to also "
        "read images referenced from thread history (e.g. url_private_download "
        "returned by fetch_thread_history)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Additional Slack file URLs to describe (must be on files*.slack.com).",
            },
        },
        "required": [],
    },
    # Each image runs Slack download (15s) + LLM describe (multi-second) in
    # parallel; the 60s ceiling is a generous safety net for limit=10.
    timeout=60.0,
)
def read_attached_images(
    ctx: ToolContext,
    limit: int = 3,
    urls: list[str] | None = None,
) -> list[dict[str, str]]:
    # The token is the per-app bot_token resolved from SSM by the worker
    # entrypoint (`app._process_worker`) and carried on the WebClient that
    # was injected into ToolContext. `ctx.settings.slack_bot_token` is
    # empty in the Lambda runtime — that field is a localtest-only field
    # — so reading it here would send `Authorization: Bearer ` and Slack
    # would 401 every download.
    token = ctx.slack_client.token
    seen: set[str] = set()
    candidates: list[tuple[str, str, str]] = []  # (url, mime_hint, name)

    # 1) Images from the current mention event
    for file_info in (ctx.event.get("files") or [])[:limit]:
        if len(candidates) >= limit:
            break
        mime = str(file_info.get("mimetype", ""))
        if not mime.startswith("image/"):
            continue
        dl = file_info.get("url_private_download") or file_info.get("url_private")
        if not dl or dl in seen:
            continue
        seen.add(dl)
        candidates.append((dl, mime, file_info.get("name", "image")))

    # 2) Extra URLs provided by the caller (typically from fetch_thread_history)
    for extra in (urls or []):
        if len(candidates) >= limit:
            break
        if extra in seen:
            continue
        seen.add(extra)
        candidates.append((extra, "", _filename_from_url(extra)))

    # Pre-flight SSRF check: validate every URL we plan to fetch BEFORE we
    # spin up worker threads, so an invalid host raises synchronously like
    # the previous serial implementation did.
    for url, _, _ in candidates:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in SLACK_FILE_HOSTS:
            raise ValueError("invalid Slack file download URL")

    if not candidates:
        return []

    def _fetch(url: str, mime_hint: str, name: str) -> dict[str, str] | None:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310 (host allowlisted)
            data = response.read()
        mime = mime_hint if mime_hint.startswith("image/") else _guess_image_mime(url)
        if not mime.startswith("image/"):
            return None
        return {"name": name, "summary": ctx.llm.describe_image(data, mime)}

    results: list[dict[str, str] | None] = [None] * len(candidates)
    with ThreadPoolExecutor(max_workers=min(len(candidates), 4)) as pool:
        future_to_idx = {pool.submit(_fetch, *c): i for i, c in enumerate(candidates)}
        for future in as_completed(future_to_idx):
            results[future_to_idx[future]] = future.result()

    return [r for r in results if r is not None]


def _guess_image_mime(url: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for ext, mime in (
        (".png", "image/png"),
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
        (".bmp", "image/bmp"),
        (".heic", "image/heic"),
    ):
        if path.endswith(ext):
            return mime
    return "image/png"  # conservative default; describe_image will still attempt


def _filename_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = path.rsplit("/", 1)[-1] if path else "image"
    return name or "image"


def _fetch_slack_file(url: str, token: str, max_bytes: int) -> tuple[bytes, str]:
    """Fetch a Slack file with size guard. Returns (body, mimetype_from_header).

    Raises:
      ValueError: on disallowed host, oversize via Content-Length, or
                  oversize discovered while reading the body.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in SLACK_FILE_HOSTS:
        raise ValueError("invalid Slack file download URL")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310
        content_length = response.headers.get("Content-Length") if response.headers else None
        if content_length and content_length.isdigit() and int(content_length) > max_bytes:
            raise ValueError(f"document exceeds MAX_DOC_BYTES={max_bytes}")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"document exceeds MAX_DOC_BYTES={max_bytes}")
        mime = (response.headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower() if response.headers else ""
    return body, mime


def _parse_pdf(
    data: bytes,
    max_pages: int,
    max_chars: int,
) -> tuple[str, int, bool]:
    """Extract text from a PDF. Raises ValueError for recoverable issues so the
    caller can emit a per-document error entry."""
    from io import BytesIO

    # Deferred import keeps pypdf out of cold-start for requests that never
    # touch this tool.
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError, DependencyError

    try:
        reader = PdfReader(BytesIO(data))
    except PdfReadError as exc:
        raise ValueError(f"PdfReadError: {exc}") from exc
    if reader.is_encrypted:
        raise ValueError("encrypted PDF not supported")
    page_count = len(reader.pages)
    if page_count > max_pages:
        raise ValueError(f"document exceeds MAX_DOC_PAGES={max_pages}")
    pieces: list[str] = []
    total = 0
    truncated = False
    for page in reader.pages:
        try:
            piece = page.extract_text() or ""
        except (PdfReadError, DependencyError) as exc:
            raise ValueError(f"PdfReadError: {exc}") from exc
        pieces.append(piece)
        total += len(piece)
        if total >= max_chars:
            truncated = True
            break
    text = "\n".join(pieces)
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return text, page_count, truncated


def _parse_text(data: bytes, max_chars: int) -> tuple[str, bool]:
    text = data.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return text, truncated


@tool(
    default_registry,
    name="read_attached_document",
    description=(
        "Read PDF or text/* files attached to the current Slack mention "
        "(and optionally extra URLs on files*.slack.com) and return the "
        "extracted text. Images are skipped — use read_attached_images "
        "for those. Returns one entry per document; if a document fails "
        "(encrypted, oversize, corrupt) the entry carries an 'error' key."
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Extra Slack file URLs (must be on files*.slack.com).",
            },
        },
        "required": [],
    },
    # Each document downloads from Slack (15s) and is parsed locally;
    # parallelized below, but the 60s ceiling covers the worst case where
    # max=5 documents all hit the download timeout.
    timeout=60.0,
)
def read_attached_document(
    ctx: ToolContext,
    limit: int = 2,
    urls: list[str] | None = None,
) -> list[dict[str, Any]]:
    # See the matching comment in `read_attached_images` — the token must
    # come from the per-app WebClient, not from `settings.slack_bot_token`
    # (which is empty in the Lambda runtime).
    token = ctx.slack_client.token
    max_bytes = ctx.settings.max_doc_bytes
    max_chars = ctx.settings.max_doc_chars
    seen: set[str] = set()
    candidates: list[tuple[str, str, str]] = []  # (url, mime_hint, name)

    def _is_doc_mime(mime: str) -> bool:
        mime = (mime or "").lower()
        return mime == DOC_PDF_MIME or mime.startswith(DOC_TEXT_PREFIX)

    for file_info in (ctx.event.get("files") or [])[:limit]:
        if len(candidates) >= limit:
            break
        mime = str(file_info.get("mimetype", ""))
        if not _is_doc_mime(mime):
            continue
        dl = file_info.get("url_private_download") or file_info.get("url_private")
        if not dl or dl in seen:
            continue
        seen.add(dl)
        candidates.append((dl, mime, file_info.get("name", "document")))

    for extra in (urls or []):
        if len(candidates) >= limit:
            break
        if extra in seen:
            continue
        seen.add(extra)
        candidates.append((extra, "", _filename_from_url(extra)))

    if not candidates:
        return []

    def _process_one(url: str, file_mime_hint: str, name: str) -> dict[str, Any] | None:
        try:
            body, header_mime = _fetch_slack_file(url, token, max_bytes)
        except ValueError as exc:
            return {"name": name, "error": str(exc)}
        except urllib.error.HTTPError as exc:
            return {"name": name, "error": f"HTTPError: {exc.code}"}
        mime = (header_mime or file_mime_hint or "").lower()
        if mime == DOC_PDF_MIME:
            try:
                text, pages, truncated = _parse_pdf(
                    body, ctx.settings.max_doc_pages, max_chars
                )
            except ValueError as exc:
                return {"name": name, "error": str(exc)}
            return {
                "name": name,
                "mimetype": DOC_PDF_MIME,
                "pages": pages,
                "chars": len(text),
                "truncated": truncated,
                "text": text,
            }
        if mime.startswith(DOC_TEXT_PREFIX):
            text, truncated = _parse_text(body, max_chars)
            return {
                "name": name,
                "mimetype": mime,
                "pages": 0,
                "chars": len(text),
                "truncated": truncated,
                "text": text,
            }
        # non-doc mime: silently skip (images handled by read_attached_images)
        return None

    results: list[dict[str, Any] | None] = [None] * len(candidates)
    with ThreadPoolExecutor(max_workers=min(len(candidates), 4)) as pool:
        future_to_idx = {pool.submit(_process_one, *c): i for i, c in enumerate(candidates)}
        for future in as_completed(future_to_idx):
            results[future_to_idx[future]] = future.result()

    return [r for r in results if r is not None]


@tool(
    default_registry,
    name="fetch_thread_history",
    description=(
        "Fetch recent messages from the current Slack thread for context. "
        "Returns each message's user display name, text, file metadata "
        "(for images include url_private_download so read_attached_images "
        "can describe them), reactions with emoji names and reacting users, "
        "and timestamp."
    ),
    parameters={
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}},
        "required": [],
    },
    # conversations_replies + up to ~limit users_info lookups (parallelized
    # via UserNameCache.warm). 30s leaves headroom for retry backoff.
    timeout=30.0,
)
def fetch_thread_history(ctx: ToolContext, limit: int = 20) -> list[dict[str, Any]]:
    def _map(res: dict[str, Any]) -> list[dict[str, Any]]:
        client = ctx.slack_client
        messages = res.get("messages", [])

        # Resolve every author/reacter we'll need in parallel before the
        # rendering loop. With a cold cache and limit=50 this would
        # otherwise be 50+ serial users_info calls (the original timeout
        # bug for read_attached_images, repeating itself here).
        user_ids: set[str] = set()
        for item in messages:
            uid = item.get("user") or item.get("bot_id")
            if uid:
                user_ids.add(uid)
            for r in item.get("reactions") or []:
                for u in (r.get("users") or []):
                    if u:
                        user_ids.add(u)
        user_name_cache.warm(client, user_ids)

        out: list[dict[str, Any]] = []
        for item in messages:
            user_id = item.get("user") or item.get("bot_id") or ""
            files = []
            for f in item.get("files") or []:
                files.append(
                    {
                        "name": f.get("name", ""),
                        "mimetype": f.get("mimetype", ""),
                        "url_private_download": f.get("url_private_download", ""),
                        "permalink": f.get("permalink", ""),
                        "title": f.get("title", ""),
                    }
                )
            reactions = []
            for r in item.get("reactions") or []:
                reacting_users = [user_name_cache.get(client, u) for u in (r.get("users") or [])]
                reactions.append(
                    {
                        "emoji": r.get("name", ""),
                        "count": r.get("count", 0),
                        "users": reacting_users,
                    }
                )
            out.append(
                {
                    "user": user_name_cache.get(client, user_id) if user_id else "",
                    "text": item.get("text", ""),
                    "ts": item.get("ts", ""),
                    "files": files,
                    "reactions": reactions,
                }
            )
        return out

    return _with_slack_retry(
        lambda: ctx.slack_client.conversations_replies(
            channel=ctx.channel, ts=ctx.thread_ts, limit=limit
        ),
        _map,
        label="conversations_replies",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _with_slack_retry(call: Callable[[], Any], map_result: Callable[[Any], Any], label: str, attempts: int = 3) -> Any:
    delay = 1.0
    last: SlackApiError | None = None
    for attempt in range(attempts):
        try:
            return map_result(call())
        except SlackApiError as exc:
            error = (exc.response or {}).get("error") if hasattr(exc, "response") else None
            if error == "ratelimited" and attempt < attempts - 1:
                retry_after = int((exc.response.headers or {}).get("Retry-After", delay)) if hasattr(exc, "response") else delay
                logger.warning("%s rate limited, sleeping %ds", label, retry_after)
                time.sleep(retry_after)
                delay *= 2
                last = exc
                continue
            raise
    if last:
        raise last
    return []
