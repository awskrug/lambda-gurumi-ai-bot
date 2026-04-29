"""Slack-facing helpers: message splitting, status indicator, user name cache, allowlist."""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable

from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


CODE_FENCE = "```"
PARAGRAPH_SEP = "\n\n"
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _slack_error_code(exc: SlackApiError) -> str:
    """Extract the `error` field from a SlackApiError's response payload.

    SlackApiError.response is normally a SlackResponse mapping; access via
    `.get("error")` works on both dict-like and SlackResponse objects.
    Falls back to empty string if anything is unexpected.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        return response.get("error", "") or ""
    except (AttributeError, TypeError):
        return ""


class MessageFormatter:
    """Split a long message into Slack-safe chunks.

    Strategy (greedy, paragraph-first):
      1. Cut at the last \\n\\n that fits inside max_len. This keeps
         sentences whole and avoids the "mid-word" splits that happen
         with a hard slice.
      2. If that cut lands inside a ``` code block, push the whole
         block to the next chunk by re-cutting at the \\n\\n right
         before the block opens.
      3. If the block itself is larger than max_len, cut at the last
         \\n\\n inside the block; if the block uses only single \\n
         between lines (the common case), cut at the last \\n instead.
         Close the current chunk with \\n``` and reopen the next chunk
         with ```\\n.
      4. When no \\n\\n is available within max_len at all, fall back
         to a sentence boundary (.!? + whitespace), then to a single
         \\n, then hard-slice.
    """

    @staticmethod
    def split_message(text: str, max_len: int = 2000) -> list[str]:
        if not text:
            return [""]
        if len(text) <= max_len:
            return [text]

        chunks: list[str] = []
        remaining = text
        fence_suffix = "\n" + CODE_FENCE
        fence_prefix = CODE_FENCE + "\n"

        while len(remaining) > max_len:
            # 1. Greedy paragraph-boundary cut within max_len.
            cut = remaining.rfind(PARAGRAPH_SEP, 0, max_len)
            if cut > 0:
                first = remaining[:cut]
                tail_start = cut + len(PARAGRAPH_SEP)
            else:
                first, tail_start = MessageFormatter._fallback_cut(remaining, max_len)

            # 2. Code-fence balancing: an odd ``` count means the cut
            #    landed inside a code block.
            if first.count(CODE_FENCE) % 2 == 1:
                last_fence = first.rfind(CODE_FENCE)
                # Try pushing the whole block to the next chunk by
                # cutting at the \n\n right before the block opens.
                block_start_cut = first.rfind(PARAGRAPH_SEP, 0, last_fence)
                if block_start_cut > 0:
                    first = remaining[:block_start_cut]
                    tail_start = block_start_cut + len(PARAGRAPH_SEP)
                else:
                    # Block won't fit anywhere whole. Cut inside the block,
                    # leaving room for the closing fence so the chunk still
                    # respects max_len. The cut must land after the opening
                    # fence and its newline so we always make progress.
                    min_cut = last_fence + len(CODE_FENCE) + 1
                    inner_budget = max_len - len(fence_suffix)
                    inner_cut = remaining.rfind(PARAGRAPH_SEP, min_cut, inner_budget)
                    if inner_cut > 0:
                        first = remaining[:inner_cut]
                        tail_start = inner_cut + len(PARAGRAPH_SEP)
                    else:
                        # Code blocks usually have only single \n between
                        # lines, not \n\n. Cut at a line boundary so a token
                        # doesn't get sliced.
                        line_cut = remaining.rfind("\n", min_cut, inner_budget)
                        if line_cut > 0:
                            first = remaining[:line_cut]
                            tail_start = line_cut + 1
                        else:
                            first = remaining[:inner_budget]
                            tail_start = inner_budget
                    chunks.append(first + fence_suffix)
                    remaining = fence_prefix + remaining[tail_start:]
                    continue

            chunks.append(first)
            remaining = remaining[tail_start:]

        if remaining:
            chunks.append(remaining)

        return chunks

    @staticmethod
    def _fallback_cut(text: str, max_len: int) -> tuple[str, int]:
        """Choose a cut when no \\n\\n boundary exists within max_len.

        Tries (in order): sentence boundary (.!? + whitespace), single
        \\n, then hard slice. The single-\\n fallback handles content
        like bullet lists or code blocks that only use one newline
        between lines.
        """
        last_match = None
        for match in SENTENCE_SPLIT_RE.finditer(text):
            if match.start() >= max_len:
                break
            last_match = match
        if last_match is not None:
            return text[: last_match.start()], last_match.end()

        line_cut = text.rfind("\n", 0, max_len)
        if line_cut > 0:
            return text[:line_cut], line_cut + 1

        return text[:max_len], max_len


def set_thread_status(client: Any, channel: str, thread_ts: str, status: str) -> None:
    """Set (or clear) the assistant thread's transient status indicator.

    Renders as a typing-like "... is thinking" line in AI-enabled workspaces.
    Pass an empty string to clear it. Swallows API errors when the feature
    is not enabled on the workspace (tier: assistant.threads).
    """
    try:
        client.assistant_threads_setStatus(channel_id=channel, thread_ts=thread_ts, status=status)
    except (SlackApiError, AttributeError, TypeError) as exc:
        logger.debug("assistant_threads_setStatus failed: %s", exc)


# --------------------------------------------------------------------------- #
# Streaming message
# --------------------------------------------------------------------------- #


class StreamingMessage:
    """Stream LLM output into a single Slack message.

    Preferred path uses Slack's native streaming API (chat.startStream /
    appendStream / stopStream, available in AI-enabled workspaces). If those
    calls fail (unsupported, missing scope, etc.) we fall back to a regular
    chat.postMessage + repeated chat.update pattern.

    Appends are throttled by `min_interval` to stay within Slack rate limits
    (chat.appendStream is Tier 4 = 100+/min; chat.update is Tier 3 = 50+/min).
    """

    NATIVE_METHOD = "chat.startStream"
    APPEND_METHOD = "chat.appendStream"
    STOP_METHOD = "chat.stopStream"
    # After this many consecutive chat_update failures on the fallback path,
    # finalize the current ts and open a fresh chat_postMessage. Covers the
    # case where the current ts is unreachable (deleted, rate-limited on a
    # specific msg, etc.) without waiting for the buffer to hit max_len.
    MAX_CONSECUTIVE_UPDATE_FAILURES = 3

    def __init__(
        self,
        client: Any,
        channel: str,
        thread_ts: str,
        placeholder: str = ":robot_face:",
        min_interval: float = 0.6,
        max_len: int = 2000,
        enable_native: bool = False,
    ) -> None:
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.placeholder = placeholder
        self.min_interval = min_interval
        # Soft cap for a single Slack message in fallback streaming mode;
        # when the rolling buffer approaches this size we finalize the
        # current ts and roll to a fresh chat_postMessage.
        self.max_len = max_len
        # Native Slack streaming (chat.startStream/appendStream/stopStream)
        # renders an extra "searching..." status UI beside our message on
        # AI-enabled workspaces, which looks like two replies to the user.
        # Default off — stream into a plain chat.postMessage + chat.update
        # loop so there's exactly one reply ts throughout the session.
        self.enable_native = enable_native
        self.ts: str | None = None
        self._buffer = ""
        self._last_flush = 0.0
        self._native = False  # True once chat.startStream succeeds
        self._stopped = False
        self._consecutive_update_failures = 0
        # Concatenation of every prefix already sealed into earlier ts'es by
        # the size-overflow roll-finalize path. stop() uses this to skip the
        # part of final_text that is already on screen, otherwise the latest
        # ts gets overwritten with a chunk whose content overlaps the rolled
        # message above it ("first two messages are nearly identical").
        self._finalized_text = ""

    # -- start ---------------------------------------------------------- #

    def start(self) -> None:
        """Initialize the streaming message.

        Starts with a plain chat.postMessage so the rest of the lifecycle
        is a single ts under our control. If `enable_native` is set we
        also try the Slack native streaming API, but it's off by default
        because on AI-enabled workspaces it renders an extra "searching"
        status UI alongside our reply that looks like a second message.
        """
        if self.enable_native:
            try:
                res = self.client.api_call(
                    self.NATIVE_METHOD,
                    params={
                        "channel": self.channel,
                        "thread_ts": self.thread_ts,
                        "markdown_text": self.placeholder,
                    },
                )
                if res.get("ok"):
                    self.ts = res.get("ts")
                    self._native = True
                    return
                logger.debug("%s returned not-ok: %s", self.NATIVE_METHOD, res.get("error"))
            except (SlackApiError, AttributeError, TypeError, KeyError) as exc:
                logger.debug("%s failed, falling back to postMessage: %s", self.NATIVE_METHOD, exc)

        # Default path: regular message we'll keep editing with chat.update.
        res = self.client.chat_postMessage(channel=self.channel, thread_ts=self.thread_ts, text=self.placeholder)
        self.ts = res.get("ts") if isinstance(res, dict) else res["ts"]

    # -- append --------------------------------------------------------- #

    def append(self, delta: str) -> None:
        """Accumulate `delta` and flush to Slack if the throttle interval passed."""
        if not delta or self._stopped or not self.ts:
            return
        self._buffer += delta
        now = time.monotonic()
        if now - self._last_flush < self.min_interval:
            return
        self._flush()
        self._last_flush = now

    def _flush(self) -> None:
        if not self._buffer or not self.ts:
            return
        text = self._buffer
        if self._native:
            try:
                self.client.api_call(
                    self.APPEND_METHOD,
                    params={"channel": self.channel, "ts": self.ts, "markdown_text": text},
                )
                self._buffer = ""
                return
            except (SlackApiError, AttributeError, TypeError) as exc:
                logger.debug("%s failed, downgrading to chat.update: %s", self.APPEND_METHOD, exc)
                self._native = False

        # Fallback: chat.update with the full accumulated text plus cursor.
        # When the buffer approaches the per-message limit we finalize this
        # message and roll into a fresh chat_postMessage so nothing gets lost
        # behind a msg_too_long error on the next update.
        display = text + " " + self.placeholder
        if len(display) >= self.max_len:
            # If the rolling buffer ends inside an unclosed code block,
            # close it on this ts and reopen it on the next placeholder
            # so each rolled message renders as a balanced block in
            # Slack instead of leaking an unclosed ``` into thread
            # rendering.
            sealed_text = text
            carry = ""
            if text.count(CODE_FENCE) % 2 == 1:
                sealed_text = text + "\n" + CODE_FENCE
                carry = CODE_FENCE + "\n"
            sealed = False
            try:
                self.client.chat_update(channel=self.channel, ts=self.ts, text=sealed_text)
                sealed = True
            except SlackApiError as exc:
                logger.warning("chat_update (roll-finalize) failed: %s", exc)
            if sealed:
                # Track the raw (un-fenced) text so stop()'s
                # final_text slice still matches the LLM output.
                self._finalized_text += text
            self._roll_to_new_message()
            if sealed and carry:
                self._buffer = carry
            return
        try:
            self.client.chat_update(channel=self.channel, ts=self.ts, text=display)
            self._consecutive_update_failures = 0
        except SlackApiError as exc:
            # msg_too_long is an explicit "this payload exceeds chat.update's
            # rendered limit" signal — Slack's mrkdwn → section-block coercion
            # caps a single block at ~3000 chars and fails well before the
            # documented 4000-char text limit on multibyte/markdown content.
            # Retrying the same buffer against a fresh ts via _roll_to_new_message
            # just produces the same failure on the new placeholder, leaving a
            # trail of empty :loading: messages in the thread. Spill the buffer
            # via chat.postMessage (40k-char limit, no rolling-update history)
            # and drop the current placeholder so the next delta starts fresh.
            if _slack_error_code(exc) == "msg_too_long":
                self._spill_buffer_via_post_message(text)
                return
            self._consecutive_update_failures += 1
            logger.warning(
                "chat_update during stream failed (%d consecutive): %s",
                self._consecutive_update_failures,
                exc,
            )
            # If updates keep failing on this ts (deleted, message-level rate
            # limit, permission change), roll to a fresh message instead of
            # burning cycles on the same broken ts until the buffer hits
            # max_len. The accumulated buffer rides along into the new ts.
            if self._consecutive_update_failures >= self.MAX_CONSECUTIVE_UPDATE_FAILURES:
                self._consecutive_update_failures = 0
                self._roll_to_new_message(preserve_buffer=True)

    def _spill_buffer_via_post_message(self, text: str) -> None:
        """Recover from chat.update msg_too_long by posting the buffered text
        as fresh thread messages and dropping the current placeholder ts.

        chat.postMessage's 40k-char limit is much more permissive than
        chat.update's effective ~3k-char limit on multibyte/markdown content,
        so MessageFormatter.split_message at self.max_len reliably fits.
        Deleting the (still :loading:) placeholder keeps the thread free of
        the empty placeholder + spilled-answer "double output" pattern.
        Setting `self.ts = None` lets the next delta lazy-start a fresh
        placeholder via the same deferred path used at first delta.
        """
        chunks = MessageFormatter.split_message(text, max_len=self.max_len)
        for chunk in chunks:
            try:
                self.client.chat_postMessage(
                    channel=self.channel, thread_ts=self.thread_ts, text=chunk,
                )
            except SlackApiError as exc:
                logger.warning("spill chat_postMessage failed: %s", exc)
        if self.ts:
            try:
                self.client.chat_delete(channel=self.channel, ts=self.ts)
            except SlackApiError as exc:
                logger.debug("placeholder chat_delete after spill failed: %s", exc)
        self._buffer = ""
        self._consecutive_update_failures = 0
        self.ts = None

    def _roll_to_new_message(self, preserve_buffer: bool = False) -> None:
        """Open a fresh placeholder message and reset the buffer. Used when
        the fallback rolling update would overflow the per-message limit,
        or when repeated chat_update failures make the current ts unusable.

        When `preserve_buffer` is True, the accumulated delta text is kept so
        the next flush re-sends it against the new ts. This matters for the
        consecutive-failure path: the deltas never reached Slack on the old
        ts, so we can't just drop them. The size-overflow path passes
        `preserve_buffer=False` because the old ts already received the full
        buffer content via the `text=text` finalize update.
        """
        try:
            res = self.client.chat_postMessage(
                channel=self.channel,
                thread_ts=self.thread_ts,
                text=self.placeholder,
            )
            self.ts = res.get("ts") if isinstance(res, dict) else res["ts"]
            if not preserve_buffer:
                self._buffer = ""
        except SlackApiError as exc:
            logger.warning("roll-to-new-message failed: %s", exc)

    # -- stop ----------------------------------------------------------- #

    def stop(self, final_text: str) -> None:
        """Finalize the message with `final_text`. Safe to call once.

        If `final_text` exceeds the per-message limit we split it with the
        MessageFormatter, put the first chunk into the current ts, and post
        the remaining chunks as additional thread messages. This avoids the
        msg_too_long failures we saw when a long answer was written back
        through a single chat.update.
        """
        if self._stopped or not self.ts:
            return
        self._stopped = True

        # If streaming rolled to a new ts via the size-overflow path, the
        # earlier ts'es already display the prefix portion of the answer.
        # Strip that prefix here so the latest ts gets only the suffix
        # instead of overwriting itself with content already shown above
        # (the "first two messages nearly identical" duplication).
        if self._finalized_text and final_text.startswith(self._finalized_text):
            final_text = final_text[len(self._finalized_text):]

        if self._native:
            # Native streaming: stopStream accepts up to 12k chars, but be
            # conservative and split to self.max_len anyway to keep UX
            # consistent with the fallback path.
            chunks = MessageFormatter.split_message(final_text, max_len=self.max_len)
            try:
                self.client.api_call(
                    self.STOP_METHOD,
                    params={"channel": self.channel, "ts": self.ts, "markdown_text": chunks[0]},
                )
                for extra in chunks[1:]:
                    try:
                        self.client.chat_postMessage(
                            channel=self.channel, thread_ts=self.thread_ts, text=extra,
                        )
                    except SlackApiError as exc:
                        logger.warning("follow-up postMessage failed: %s", exc)
                return
            except (SlackApiError, AttributeError, TypeError) as exc:
                logger.debug("%s failed, finalizing with chat.update: %s", self.STOP_METHOD, exc)
                self._native = False

        # Fallback finalizer: split and roll.
        chunks = MessageFormatter.split_message(final_text, max_len=self.max_len)
        first = chunks[0]
        try:
            self.client.chat_update(channel=self.channel, ts=self.ts, text=first)
        except SlackApiError as exc:
            logger.warning("final chat_update failed (len=%d): %s", len(first), exc)
            # Drop the still-:loading: placeholder before posting the answer
            # via chat.postMessage; otherwise the user sees the placeholder
            # and the spilled answer side-by-side ("double output").
            try:
                self.client.chat_delete(channel=self.channel, ts=self.ts)
            except SlackApiError as exc2:
                logger.debug("placeholder chat_delete after final failure: %s", exc2)
            # Fallback to postMessage so at least the text lands somewhere.
            try:
                self.client.chat_postMessage(
                    channel=self.channel, thread_ts=self.thread_ts, text=first,
                )
            except SlackApiError as exc2:
                logger.warning("final postMessage also failed: %s", exc2)
        for extra in chunks[1:]:
            try:
                self.client.chat_postMessage(
                    channel=self.channel, thread_ts=self.thread_ts, text=extra,
                )
            except SlackApiError as exc:
                logger.warning("follow-up postMessage failed: %s", exc)


@dataclass
class UserNameCache:
    """Module-level cache keyed by user_id. Survives warm starts.

    Thread-safe: `warm()` resolves cache misses in parallel threads, so
    cache writes go through `_lock`. Reads are lock-free (a `dict.get`
    on an existing key is GIL-atomic in CPython; a worst-case race just
    causes a redundant `users_info` call, never a corrupt cache)."""

    _cache: dict[str, str]
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def _default(cls) -> "UserNameCache":
        return cls(_cache={})

    def get(self, client: Any, user_id: str) -> str:
        if not user_id:
            return ""
        cached = self._cache.get(user_id)
        if cached is not None:
            return cached
        try:
            info = client.users_info(user=user_id)
            profile = (info.get("user") or {}).get("profile") or {}
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or (info.get("user") or {}).get("real_name")
                or user_id
            )
        except SlackApiError as exc:
            logger.debug("users_info failed for %s: %s", user_id, exc)
            name = user_id
        with self._lock:
            self._cache[user_id] = name
        return name

    def warm(self, client: Any, user_ids: Iterable[str]) -> None:
        """Pre-resolve display names for the given user IDs in parallel.

        Used by callers that know they'll need many user names before the
        rendering loop starts (e.g. `fetch_thread_history`). Without this,
        `get()` runs serially inside the loop and 50 cache misses become
        50 sequential `users_info` calls, easily blowing the tool timeout."""
        misses = list({uid for uid in user_ids if uid and uid not in self._cache})
        if not misses:
            return
        with ThreadPoolExecutor(max_workers=min(len(misses), 8)) as pool:
            list(pool.map(lambda uid: self.get(client, uid), misses))


user_name_cache = UserNameCache._default()


def channel_allowed(channel: str, allowed_ids: list[str]) -> bool:
    """Return True if no allowlist configured or channel is listed."""
    if not allowed_ids:
        return True
    return channel in allowed_ids


def sanitize_error(exc: BaseException) -> str:
    """User-facing error text. Strips internal paths/tokens."""
    msg = str(exc) or exc.__class__.__name__
    # Redact tokens. Order matters: more specific patterns (sk-ant, sk-proj)
    # run before the generic sk- pattern so the labels stay accurate.
    msg = re.sub(r"xox[abprs]-[A-Za-z0-9-]+", "[redacted-slack-token]", msg)
    msg = re.sub(r"sk-ant-[A-Za-z0-9\-_]{10,}", "[redacted-anthropic-key]", msg)
    msg = re.sub(r"sk-[A-Za-z0-9\-_]{10,}", "[redacted-openai-key]", msg)
    msg = re.sub(r"xai-[A-Za-z0-9\-_]{10,}", "[redacted-xai-key]", msg)
    msg = re.sub(r"tvly-[A-Za-z0-9\-_]{10,}", "[redacted-tavily-key]", msg)
    # AWS access keys: AKIA (long-term) / ASIA (temporary session).
    msg = re.sub(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "[redacted-aws-key]", msg)
    # Truncate stack-like paths.
    msg = re.sub(r"(/[\w./-]+\.py)", "[path]", msg)
    if len(msg) > 300:
        msg = msg[:297] + "..."
    return msg


