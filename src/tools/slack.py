"""Slack-centric tools: read images/documents attached to the triggering
mention, fetch the current thread's history."""
from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from slack_sdk.errors import SlackApiError

from src.slack_helpers import user_name_cache
from src.tools.registry import ToolContext, default_registry, tool
# Reuse SSRF primitives from web — _NoRedirectHandler keeps a 3xx from
# leaking the bot's Authorization header to a non-Slack host, and
# _read_body_capped enforces the per-tool size cap on streamed reads.
from src.tools.web import _NoRedirectHandler, _read_body_capped

logger = logging.getLogger(__name__)

# Per-message text cap for fetch_thread_history. Long messages can blow
# up the agent's context window; truncating here keeps a 50-message
# history bounded for the next LLM hop. Hard-coded — operators have no
# practical reason to tune it, and a separate setting would just be one
# more knob to misconfigure.
_HISTORY_TEXT_CHARS = 2000
# Aggregate cap across all returned message texts. Even with the
# per-message cap above, a 50-message history can still flood the agent
# context (50 × 2000 = 100k chars). Stop accumulating once we cross
# this budget; the oldest messages are kept (Slack returns oldest-first)
# and the trailing ones get a clear truncation notice.
_HISTORY_TOTAL_CHARS = 30000

SLACK_FILE_HOSTS = {"files.slack.com", "files-edge.slack.com", "files-pri.slack.com"}
# Profile avatars come from Slack's CDN (custom uploads) or Gravatar
# (default fallback). Both are public — sending the bot Authorization
# header to these hosts is unnecessary and could leak the token, so the
# fetch helper below skips it for these hosts.
SLACK_PROFILE_IMAGE_HOSTS = {
    "avatars.slack-edge.com",
    "a.slack-edge.com",
    "secure.gravatar.com",
}
SLACK_IMAGE_HOSTS = SLACK_FILE_HOSTS | SLACK_PROFILE_IMAGE_HOSTS
DOC_TEXT_PREFIX = "text/"
DOC_PDF_MIME = "application/pdf"
_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")


def _http_get(req: urllib.request.Request, timeout: int = 15):
    """Open `req` with redirects refused.

    All Slack file fetches go through here so a 3xx cannot follow off-host
    and carry the bot's Authorization header to a non-Slack target. This
    is also the single name tests patch — keeps the patch surface tiny
    instead of digging into `build_opener` plumbing.
    """
    opener = urllib.request.build_opener(_NoRedirectHandler())
    return opener.open(req, timeout=timeout)


@tool(
    default_registry,
    name="read_attached_images",
    description=(
        "Read image files and return textual descriptions. By default reads "
        "images attached to the current Slack mention. Pass `urls` to also "
        "read images referenced from thread history (url_private_download "
        "returned by fetch_thread_history) or profile images (image_url "
        "returned by fetch_user_profile)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Additional image URLs to describe. Must be either a "
                    "Slack file URL (files*.slack.com) or a Slack profile "
                    "image URL (avatars.slack-edge.com, a.slack-edge.com, "
                    "secure.gravatar.com)."
                ),
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
    # spin up worker threads, so an invalid host surfaces as a synchronous
    # ValueError rather than a thread-pool future exception.
    for url, _, _ in candidates:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in SLACK_IMAGE_HOSTS:
            raise ValueError("invalid Slack file download URL")

    if not candidates:
        return []

    max_bytes = ctx.settings.max_image_bytes

    def _fetch(url: str, mime_hint: str, name: str) -> dict[str, str] | None:
        # Profile-image hosts are public CDN; sending Authorization there
        # is unnecessary and could leak the bot token. Only files*.slack.com
        # requires auth (private-by-default).
        headers: dict[str, str] = {}
        if urllib.parse.urlparse(url).hostname in SLACK_FILE_HOSTS:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        # _http_get refuses 3xx so the Authorization header above cannot
        # follow a redirect off-host and leak the bot token.
        with _http_get(req, timeout=15) as response:  # noqa: S310 (host allowlisted; redirects disabled)
            data = _read_body_capped(response, max_bytes)
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


@tool(
    default_registry,
    name="fetch_user_profile",
    description=(
        "Look up a Slack user's profile and return their display name, real "
        "name, and profile image URL. Accepts either a Slack user ID (U…/W…), "
        "a mention like <@U12345>, or a display name (resolved against names "
        "already seen in this session — call fetch_thread_history first if a "
        "name lookup fails). The returned image_url can be passed via the "
        "`urls` parameter of edit_image (to restyle the avatar) or "
        "read_attached_images (to describe it)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "user": {
                "type": "string",
                "description": "Slack user ID (U…/W…), <@U…> mention, or display name.",
            },
        },
        "required": ["user"],
    },
    # Single users.info call; 15s matches the Slack-API timeout used elsewhere.
    timeout=15.0,
)
def fetch_user_profile(ctx: ToolContext, user: str) -> dict[str, str]:
    user_id = _resolve_user_id(user)
    if not user_id:
        # Cache-miss recovery: try warming the display-name cache from
        # the current thread once, then re-resolve. Covers the "LLM
        # called fetch_user_profile before fetch_thread_history" flow
        # that the description discourages but doesn't enforce —
        # without this, the user-visible failure is just an opaque
        # "could not resolve user".
        if _warm_cache_from_thread(ctx):
            user_id = _resolve_user_id(user)
    if not user_id:
        raise ValueError(
            f"could not resolve user {user!r}. Pass a user ID (U…/W…), "
            "a <@U…> mention, or call fetch_thread_history first so the "
            "display name is in cache."
        )
    try:
        info = ctx.slack_client.users_info(user=user_id)
    except SlackApiError as exc:
        raise ValueError(f"users.info failed for {user_id}: {_slack_error(exc)}") from exc
    user_obj = info.get("user") or {}
    profile = user_obj.get("profile") or {}
    real_name = user_obj.get("real_name") or profile.get("real_name") or ""
    display_name = profile.get("display_name") or real_name or user_id
    # Prefer the largest available avatar so edit_image has good resolution.
    # `image_original` is only present for users with custom uploads;
    # default-avatar users have only image_24..image_512.
    image_url = (
        profile.get("image_original")
        or profile.get("image_1024")
        or profile.get("image_512")
        or profile.get("image_192")
        or profile.get("image_72")
        or ""
    )
    # Cache the resolved display name so subsequent fetch_thread_history
    # calls don't re-resolve via another users.info round trip.
    user_name_cache.set(user_id, display_name)
    return {
        "user_id": user_id,
        "display_name": display_name,
        "real_name": real_name,
        "image_url": image_url,
    }


def _warm_cache_from_thread(ctx: ToolContext) -> bool:
    """Best-effort: pull thread participants and warm the display-name
    cache. Returns True when at least one user_id was resolved (so a
    follow-up `_resolve_user_id` call may succeed); False on any
    failure or when the thread is empty.

    `fetch_user_profile` calls this once when a display-name lookup
    misses the cache, so the LLM doesn't need to call
    `fetch_thread_history` explicitly before referencing a user by
    display name.
    """
    if not getattr(ctx, "thread_ts", None) or not getattr(ctx, "channel", None):
        return False
    try:
        res = ctx.slack_client.conversations_replies(
            channel=ctx.channel, ts=ctx.thread_ts, limit=50
        )
    except SlackApiError as exc:
        logger.debug("cache-warm fetch failed: %s", _slack_error(exc))
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("cache-warm fetch unexpected error: %s", exc)
        return False
    messages = res.get("messages", []) if hasattr(res, "get") else []
    user_ids: set[str] = {
        m.get("user") for m in messages if m.get("user")
    }
    if not user_ids:
        return False
    user_name_cache.warm(ctx.slack_client, user_ids)
    return True


def _resolve_user_id(identifier: str) -> str | None:
    """Resolve a free-form user reference to a Slack user ID.

    Accepts: bare user ID (U…/W…), `<@U12345>` or `<@U12345|name>` mention
    syntax, or a display name (looked up against `user_name_cache`).
    Returns None when no match is found.
    """
    if not identifier:
        return None
    candidate = identifier.strip()
    if candidate.startswith("<@") and candidate.endswith(">"):
        candidate = candidate[2:-1].split("|", 1)[0]
    if _USER_ID_RE.match(candidate):
        return candidate
    return user_name_cache.find_by_name(identifier.strip())


def _slack_error(exc: SlackApiError) -> str:
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    try:
        return response.get("error", "") or str(exc)
    except (AttributeError, TypeError):
        return str(exc)


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
    # _http_get prevents a 3xx from carrying the bot token to an off-host
    # redirect target.
    with _http_get(req, timeout=15) as response:  # noqa: S310 (host allowlisted; redirects disabled)
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
        # otherwise be 50+ serial users.info calls and easily blow the
        # tool timeout.
        # bot_ids (B…) are NOT resolvable via users.info — that endpoint
        # would 404 and pollute the cache. We render bot messages from
        # the inline `username`/`bot_id` fields below.
        user_ids: set[str] = set()
        for item in messages:
            uid = item.get("user")
            if uid:
                user_ids.add(uid)
            for r in item.get("reactions") or []:
                for u in (r.get("users") or []):
                    if u:
                        user_ids.add(u)
        user_name_cache.warm(client, user_ids)

        out: list[dict[str, Any]] = []
        total_text_chars = 0
        for item in messages:
            user_id = item.get("user") or ""
            if user_id:
                author = user_name_cache.get(client, user_id)
            else:
                # Bot message: prefer the human-readable username Slack
                # ships in the event payload; fall back to the bot_id so
                # the LLM has *something* to attribute the message to.
                author = item.get("username") or item.get("bot_id") or ""
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
            text = item.get("text", "")
            if len(text) > _HISTORY_TEXT_CHARS:
                text = text[:_HISTORY_TEXT_CHARS] + "…"
            # Aggregate budget — once exhausted, stop pulling text content
            # but still emit a short stub so the LLM can see the
            # conversation continued (and how many messages were dropped).
            budget_left = _HISTORY_TOTAL_CHARS - total_text_chars
            if budget_left <= 0:
                remaining = len(messages) - len(out)
                out.append(
                    {
                        "user": author,
                        "text": f"[{remaining} more messages truncated]",
                        "ts": item.get("ts", ""),
                        "files": files,
                        "reactions": reactions,
                    }
                )
                break
            if len(text) > budget_left:
                text = text[:budget_left] + "…"
            total_text_chars += len(text)
            out.append(
                {
                    "user": author,
                    "text": text,
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
                continue
            raise
    # Unreachable: every loop iteration either returns or re-raises. The
    # only path that `continue`s is "ratelimited AND not the final attempt",
    # so the final attempt always exits the loop one way or another.
    raise RuntimeError(f"{label}: exhausted retries without raising")
