from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from src.slack_helpers import (
    MessageFormatter,
    UserNameCache,
    channel_allowed,
    sanitize_error,
)


def test_split_short_message_is_single_chunk():
    chunks = MessageFormatter.split_message("hello", max_len=100)
    assert chunks == ["hello"]


def test_split_by_paragraph():
    para1 = "A" * 1500
    para2 = "B" * 1500
    text = f"{para1}\n\n{para2}"
    chunks = MessageFormatter.split_message(text, max_len=2000)
    assert len(chunks) == 2
    assert all(len(c) <= 2000 for c in chunks)


def test_split_by_sentence_when_paragraph_too_long():
    sent = "Sentence with some length. " * 100  # long paragraph without \n\n
    chunks = MessageFormatter.split_message(sent, max_len=300)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)


def test_split_keeps_small_code_blocks_intact():
    """Code blocks that fit within max_len should not be split."""
    body = "text before\n\n```\nprint('x')\n```\n\ntext after"
    chunks = MessageFormatter.split_message(body, max_len=2000)
    # Entire content fits; must be a single chunk.
    assert len(chunks) == 1
    assert chunks[0].count("```") == 2


def test_split_code_block_longer_than_max_len_still_respects_limit():
    """When a code block exceeds max_len, the splitter cuts inside the
    block, closes the current chunk with ``` and reopens the next one
    with ```. Every chunk must still fit max_len AND be self-balanced."""
    code = "```\n" + ("def x():\n    return 1\n" * 100) + "```"
    chunks = MessageFormatter.split_message(code, max_len=500)
    assert all(len(c) <= 500 for c in chunks)
    # Each chunk closes its own code block — no chunk leaks an unclosed
    # fence into the user's thread view.
    assert all(c.count("```") % 2 == 0 for c in chunks)


def test_split_empty_string():
    assert MessageFormatter.split_message("", max_len=100) == [""]


def test_split_cuts_at_paragraph_boundary_not_mid_word():
    """The greedy \\n\\n cut must keep words and sentences whole rather
    than slicing in the middle of a paragraph."""
    para1 = "First paragraph. " * 10  # ~170 chars
    para2 = "Second paragraph. " * 10  # ~180 chars
    para3 = "Third paragraph. " * 10  # ~170 chars
    text = f"{para1}\n\n{para2}\n\n{para3}"
    chunks = MessageFormatter.split_message(text, max_len=200)
    # Every chunk should end at a paragraph or sentence boundary, never
    # in the middle of a word like "para" + "graph".
    for chunk in chunks:
        assert not chunk.endswith("para"), f"mid-word cut: {chunk!r}"
        # Trailing chars should be one of: full word, period+space, or end-of-text
        assert chunk[-1] in ".!? \n" or chunk == chunks[-1]


def test_split_pushes_code_block_to_next_chunk_when_possible():
    """If the cut would land inside a code block but a \\n\\n exists
    right before the block starts, push the whole block to the next
    chunk so the block stays intact."""
    text = (
        "intro paragraph\n\n"
        "another intro paragraph\n\n"
        "```\n"
        "def example():\n"
        "    return 42\n"
        "```\n\n"
        "epilogue"
    )
    # Force a cut that initially lands inside the code block.
    chunks = MessageFormatter.split_message(text, max_len=60)
    # The code block must appear whole in some chunk — never split
    # across two chunks when it could have been pushed.
    block = "```\ndef example():\n    return 42\n```"
    assert any(block in c for c in chunks), f"block was split: {chunks}"
    # All chunks fit and are self-balanced.
    assert all(len(c) <= 60 for c in chunks)
    assert all(c.count("```") % 2 == 0 for c in chunks)


def test_split_inside_code_block_uses_inner_paragraph_break():
    """When the block itself is too large, cut at the last \\n\\n
    inside the block and re-fence both sides."""
    inner_para1 = "a " * 50  # ~100 chars
    inner_para2 = "b " * 50  # ~100 chars
    inner_para3 = "c " * 50  # ~100 chars
    text = f"```\n{inner_para1}\n\n{inner_para2}\n\n{inner_para3}\n```"
    chunks = MessageFormatter.split_message(text, max_len=150)
    # Length budget respected on every chunk.
    assert all(len(c) <= 150 for c in chunks), [len(c) for c in chunks]
    # Every chunk is self-balanced: it opens with ``` and closes with ```
    # so the user never sees an unclosed code block.
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, f"unbalanced fence: {chunk!r}"
        assert chunk.startswith("```")
        assert chunk.rstrip().endswith("```")


def test_split_inside_code_block_falls_back_to_single_newline():
    """Real code blocks rarely contain \\n\\n — they use single \\n
    between lines. When the block exceeds max_len, the splitter must
    cut at a line boundary (\\n) so identifiers and tokens stay whole
    instead of getting hard-sliced mid-word."""
    code = "```\n" + "\n".join(f"def function_number_{i}(): return {i}" for i in range(20)) + "\n```"
    chunks = MessageFormatter.split_message(code, max_len=150)
    assert all(len(c) <= 150 for c in chunks), [len(c) for c in chunks]
    # Every chunk balanced: both ``` are inside the chunk.
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, f"unbalanced fence: {chunk!r}"
        assert chunk.startswith("```")
        assert chunk.rstrip().endswith("```")
    # No identifier got cut in half. Every "function_number_<N>" that
    # appears must be complete (each cut should have landed on \n,
    # never inside the identifier).
    import re

    full_text = "\n".join(chunks)
    # Each occurrence of "function_number_" must be followed by a digit
    # and then "(" — otherwise it was sliced.
    bad = re.findall(r"function_number_(?!\d+\()[^\n]{0,5}", full_text)
    assert not bad, f"identifier sliced: {bad}"


def test_split_falls_back_to_single_newline_for_lists():
    """Lists or other line-based content without \\n\\n or sentence
    punctuation should still cut at \\n rather than hard-slicing."""
    text = "\n".join(f"- item number {i} with some descriptive text" for i in range(20))
    chunks = MessageFormatter.split_message(text, max_len=150)
    assert all(len(c) <= 150 for c in chunks), [len(c) for c in chunks]
    # Every chunk should start and end at a list-item boundary, not
    # mid-word. We verify by ensuring no chunk ends with a partial
    # "item number <N>" pattern.
    for chunk in chunks[:-1]:
        # Trailing content must be a complete item line, not split.
        last_line = chunk.rstrip("\n").rsplit("\n", 1)[-1]
        assert last_line.startswith("- item number "), f"mid-word cut: {last_line!r}"


def test_user_name_cache_uses_display_name():
    cache = UserNameCache._default()
    client = MagicMock()
    client.users_info.return_value = {"user": {"profile": {"display_name": "Alice"}}}
    assert cache.get(client, "U1") == "Alice"
    # second call is cached
    assert cache.get(client, "U1") == "Alice"
    client.users_info.assert_called_once()


def test_user_name_cache_falls_back_to_user_id_on_error():
    cache = UserNameCache._default()
    client = MagicMock()
    client.users_info.side_effect = SlackApiError("fail", {})
    assert cache.get(client, "U2") == "U2"


def test_user_name_cache_warm_resolves_misses_in_parallel():
    """warm() must hit users_info once per uncached id and run them
    concurrently — the original serial pattern blew the tool timeout
    when many new users appeared in a thread."""
    import threading
    import time as _time

    cache = UserNameCache._default()
    client = MagicMock()

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

    client.users_info.side_effect = _slow_users_info

    started = _time.monotonic()
    cache.warm(client, ["U1", "U2", "U3", "U4"])
    elapsed = _time.monotonic() - started

    assert peak >= 2, f"expected concurrent users_info, peak={peak}"
    assert elapsed < 0.6, f"expected parallel ~0.2s, took {elapsed:.2f}s"
    assert cache.get(client, "U1") == "name-U1"
    # warm populated the cache, so get() does not trigger a second API call
    assert client.users_info.call_count == 4


def test_user_name_cache_warm_skips_cached_and_empty_ids():
    cache = UserNameCache._default()
    client = MagicMock()
    client.users_info.return_value = {"user": {"profile": {"display_name": "Alice"}}}
    cache.get(client, "U1")  # prime cache
    client.users_info.reset_mock()

    cache.warm(client, ["U1", "", None, "U1"])  # all already-cached or skippable

    client.users_info.assert_not_called()


def test_channel_allowed_no_allowlist():
    assert channel_allowed("C1", []) is True


def test_channel_allowed_allowlist_match():
    assert channel_allowed("C1", ["C1", "C2"]) is True


def test_channel_allowed_allowlist_miss():
    assert channel_allowed("C9", ["C1", "C2"]) is False


def test_sanitize_error_redacts_tokens():
    class FakeErr(Exception):
        pass

    exc = FakeErr("failed with token xoxb-12345-67890 for /path/to/file.py boom")
    out = sanitize_error(exc)
    assert "xoxb-12345" not in out
    assert "redacted-slack-token" in out
    assert "[path]" in out


def test_sanitize_error_redacts_openai_key():
    exc = ValueError("Bad request using sk-proj-abcdefghij1234567890xyz")
    out = sanitize_error(exc)
    assert "sk-proj" not in out


def test_sanitize_error_truncates_long():
    exc = ValueError("x" * 1000)
    out = sanitize_error(exc)
    assert len(out) <= 300


def test_sanitize_error_redacts_anthropic_key():
    exc = ValueError("failed: sk-ant-api03-abc123xyz456_-_deadbeef")
    out = sanitize_error(exc)
    assert "sk-ant-api03" not in out
    assert "redacted-anthropic-key" in out
    # Must not also match the generic openai key pattern (order matters).
    assert "redacted-openai-key" not in out


def test_sanitize_error_redacts_xai_key():
    exc = ValueError("grok call failed with xai-abcdef1234567890xyz")
    out = sanitize_error(exc)
    assert "xai-abcdef" not in out
    assert "redacted-xai-key" in out


def test_sanitize_error_redacts_tavily_key():
    exc = ValueError("tavily error: tvly-abcdefghij1234567890")
    out = sanitize_error(exc)
    assert "tvly-abcdef" not in out
    assert "redacted-tavily-key" in out


def test_sanitize_error_redacts_aws_access_key():
    exc = ValueError("Boto3 failure for AKIAIOSFODNN7EXAMPLE in region")
    out = sanitize_error(exc)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "redacted-aws-key" in out


def test_sanitize_error_redacts_aws_session_key():
    exc = ValueError("temp creds ASIA123456789ABCDEF0 expired")
    out = sanitize_error(exc)
    assert "ASIA123456789ABCDEF0" not in out
    assert "redacted-aws-key" in out


# --------------------------------------------------------------------------- #
# StreamingMessage
# --------------------------------------------------------------------------- #

from src.slack_helpers import CODE_FENCE, StreamingMessage


def _slack_client_native_stream():
    """Client whose api_call responds to chat.startStream/appendStream/stopStream."""
    client = MagicMock()

    def api_call(method, params=None, **_):
        if method == "chat.startStream":
            return {"ok": True, "channel": params["channel"], "ts": "1234.5678"}
        if method == "chat.appendStream":
            return {"ok": True, "channel": params["channel"], "ts": params["ts"]}
        if method == "chat.stopStream":
            return {"ok": True, "channel": params["channel"], "ts": params["ts"]}
        raise AssertionError(f"unexpected api_call: {method}")

    client.api_call.side_effect = api_call
    return client


def test_streaming_message_native_start_uses_api_call():
    client = _slack_client_native_stream()
    sm = StreamingMessage(client=client, channel="C1", thread_ts="ts1", placeholder=":robot:", enable_native=True)
    sm.start()
    assert sm.ts == "1234.5678"
    assert sm._native is True
    client.api_call.assert_called_once()
    assert client.api_call.call_args.args[0] == "chat.startStream"


def test_streaming_message_fallback_when_native_fails():
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    client.chat_postMessage.return_value = {"ok": True, "ts": "fallback-ts"}
    sm = StreamingMessage(client=client, channel="C1", thread_ts="ts1")
    sm.start()
    assert sm.ts == "fallback-ts"
    assert sm._native is False
    client.chat_postMessage.assert_called_once()


def test_streaming_message_append_throttles():
    client = _slack_client_native_stream()
    sm = StreamingMessage(client=client, channel="C1", thread_ts="ts1", min_interval=10.0, enable_native=True)
    sm.start()
    # First append should flush (last_flush=0 -> elapsed > interval)
    sm.append("hello ")
    # Second append within interval: should buffer, not flush
    sm.append("world")
    # Count of appendStream calls should be <= 1 within this tight window
    append_calls = [c for c in client.api_call.call_args_list if c.args[0] == "chat.appendStream"]
    assert len(append_calls) <= 1


def test_streaming_message_stop_finalizes_native():
    client = _slack_client_native_stream()
    sm = StreamingMessage(client=client, channel="C1", thread_ts="ts1", enable_native=True)
    sm.start()
    sm.stop("final answer")
    stop_calls = [c for c in client.api_call.call_args_list if c.args[0] == "chat.stopStream"]
    assert len(stop_calls) == 1
    assert stop_calls[0].kwargs["params"]["markdown_text"] == "final answer"


def test_streaming_message_stop_fallback_uses_chat_update():
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    client.chat_postMessage.return_value = {"ok": True, "ts": "fallback-ts"}
    sm = StreamingMessage(client=client, channel="C1", thread_ts="ts1")
    sm.start()
    sm.stop("done")
    client.chat_update.assert_called_with(channel="C1", ts="fallback-ts", text="done")


def test_streaming_message_stop_is_idempotent():
    client = _slack_client_native_stream()
    sm = StreamingMessage(client=client, channel="C1", thread_ts="ts1", enable_native=True)
    sm.start()
    sm.stop("a")
    sm.stop("b")
    stop_calls = [c for c in client.api_call.call_args_list if c.args[0] == "chat.stopStream"]
    assert len(stop_calls) == 1  # only first stop fires


def test_streaming_message_append_noop_before_start():
    client = MagicMock()
    sm = StreamingMessage(client=client, channel="C1", thread_ts="ts1")
    sm.append("hi")  # should not explode, ts is None
    client.api_call.assert_not_called()


def test_streaming_message_fallback_rolls_when_buffer_exceeds_limit():
    """In fallback mode, when the rolling buffer approaches max_len we must
    finalize the current ts and open a fresh chat_postMessage so nothing gets
    lost behind msg_too_long."""
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    # start -> one placeholder, then every new postMessage returns a new ts
    client.chat_postMessage.side_effect = [
        {"ok": True, "ts": "first"},
        {"ok": True, "ts": "second"},
    ]
    sm = StreamingMessage(client=client, channel="C1", thread_ts="ts1", min_interval=0.0, max_len=50)
    sm.start()
    # Force one flush so we have state; buffer is small.
    sm.append("hi")
    # Feed enough content to exceed max_len on the next flush.
    sm.append("x" * 60)
    # After the roll, ts should have advanced and the buffer should be empty.
    assert sm.ts == "second"
    assert sm._buffer == ""
    # chat_update was used to finalize the first message content before rolling.
    assert client.chat_update.called


def test_streaming_message_rolls_after_consecutive_update_failures():
    """If chat_update keeps failing on the current ts (deleted, permission,
    message-level rate limit), the streamer must roll to a fresh ts instead
    of looping forever. The accumulated buffer must be preserved so nothing
    is lost."""
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    client.chat_postMessage.side_effect = [
        {"ok": True, "ts": "first"},
        {"ok": True, "ts": "rolled"},
    ]
    client.chat_update.side_effect = SlackApiError(
        "no", {"error": "message_not_found"}
    )
    sm = StreamingMessage(
        client=client, channel="C1", thread_ts="ts1", min_interval=0.0, max_len=10_000
    )
    sm.start()
    # Three consecutive append -> flush -> chat_update failures. The third one
    # should trigger a roll.
    sm.append("alpha")
    sm.append("beta")
    sm.append("gamma")
    assert sm.ts == "rolled"
    # Buffer preserved: the deltas never reached Slack on the old ts.
    assert "alpha" in sm._buffer
    assert "beta" in sm._buffer
    assert "gamma" in sm._buffer
    # Failure counter reset after roll so the new ts gets a clean window.
    assert sm._consecutive_update_failures == 0


def test_streaming_message_resets_failure_counter_on_success():
    """A successful chat_update must reset the consecutive-failure counter."""
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    client.chat_postMessage.return_value = {"ok": True, "ts": "ts-a"}
    call_count = {"n": 0}

    def _flaky_update(**_):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise SlackApiError("transient", {"error": "ratelimited"})
        return {"ok": True}

    client.chat_update.side_effect = _flaky_update
    sm = StreamingMessage(
        client=client, channel="C1", thread_ts="ts1", min_interval=0.0, max_len=10_000
    )
    sm.start()
    sm.append("one")   # fails
    sm.append("two")   # succeeds, counter resets
    assert sm._consecutive_update_failures == 0
    # Still on original ts (no roll) because we only saw 1 failure.
    assert sm.ts == "ts-a"


def test_streaming_message_stop_splits_long_final():
    """A final_text longer than max_len must land in multiple messages."""
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    # Return a fresh ts for every postMessage so the test is not sensitive
    # to how many follow-ups the splitter produces.
    client.chat_postMessage.return_value = {"ok": True, "ts": "post-ts"}
    sm = StreamingMessage(client=client, channel="C1", thread_ts="ts1", max_len=100)
    sm.start()
    final = "para one " * 30 + "\n\n" + "para two " * 30 + "\n\n" + "para three " * 30
    sm.stop(final)
    # first chunk went to chat_update on the placeholder ts
    assert client.chat_update.call_args.kwargs["ts"] == "post-ts"
    # at least one follow-up post message was issued (beyond the initial placeholder)
    follow_calls = [c for c in client.chat_postMessage.call_args_list if c.kwargs.get("thread_ts") == "ts1"]
    assert len(follow_calls) >= 1


def test_streaming_message_flush_msg_too_long_spills_via_postmessage_and_drops_placeholder():
    """Regression: chat.update's effective limit on multibyte/markdown content
    is well under its documented 4000-char cap (mrkdwn → section block coerces
    to ~3000). Retrying the same buffer against a fresh ts via roll-with-
    preserve-buffer produces the same failure on every new placeholder, so
    the user ends up seeing N empty :loading: messages plus the spilled
    answer ("double output"). msg_too_long must spill via chat.postMessage
    and chat.delete the placeholder."""
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    client.chat_postMessage.return_value = {"ok": True, "ts": "placeholder-ts"}
    client.chat_update.side_effect = SlackApiError(
        "too long", {"error": "msg_too_long"}
    )
    sm = StreamingMessage(
        client=client, channel="C1", thread_ts="ts1", min_interval=0.0, max_len=10_000
    )
    sm.start()
    sm.append("a" * 1500)

    # Spill path: at least one chat.postMessage with the buffer body went out,
    # the placeholder was deleted, and ts is reset so the next delta lazily
    # opens a fresh placeholder via the deferred-start path.
    spill_calls = [
        c
        for c in client.chat_postMessage.call_args_list
        if c.kwargs.get("thread_ts") == "ts1" and c.kwargs.get("text", "").startswith("a")
    ]
    assert len(spill_calls) >= 1
    client.chat_delete.assert_called_with(channel="C1", ts="placeholder-ts")
    assert sm.ts is None
    assert sm._buffer == ""
    assert sm._consecutive_update_failures == 0


def test_streaming_message_stop_msg_too_long_deletes_placeholder():
    """If the final chat.update fails (e.g. msg_too_long on a multi-paragraph
    answer), the placeholder must be deleted before the fallback chat.postMessage
    so the user doesn't see :loading: alongside the answer."""
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    client.chat_postMessage.return_value = {"ok": True, "ts": "placeholder-ts"}
    client.chat_update.side_effect = SlackApiError(
        "too long", {"error": "msg_too_long"}
    )
    sm = StreamingMessage(client=client, channel="C1", thread_ts="ts1", max_len=10_000)
    sm.start()
    sm.stop("final answer")

    client.chat_delete.assert_called_with(channel="C1", ts="placeholder-ts")
    # Fallback postMessage carries the answer; the start() call also posted a
    # placeholder, so we filter on the actual answer body.
    answer_posts = [
        c
        for c in client.chat_postMessage.call_args_list
        if c.kwargs.get("text") == "final answer"
    ]
    assert len(answer_posts) == 1


def test_streaming_message_stop_skips_already_rolled_prefix():
    """Regression: when streaming rolled to a new ts via the size-overflow
    path, stop(final_text) must skip the prefix already sealed into the
    earlier ts. Otherwise the latest ts gets overwritten with content that
    overlaps the rolled message above it, producing two near-identical
    messages (prefix in ts1, prefix in ts2, then the suffix as ts3)."""
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    client.chat_postMessage.side_effect = [
        {"ok": True, "ts": "ts1"},
        {"ok": True, "ts": "ts2"},
        {"ok": True, "ts": "ts3"},
        {"ok": True, "ts": "ts4"},
    ]
    client.chat_update.return_value = {"ok": True}

    sm = StreamingMessage(
        client=client, channel="C1", thread_ts="thread-1", min_interval=0.0, max_len=50
    )
    sm.start()

    # Stream ~60 chars to trigger one roll-finalize: ts1 sealed, ts2 placeholder.
    prefix = "A" * 60
    sm.append(prefix)
    assert sm.ts == "ts2"

    # Stream the suffix into ts2.
    suffix = "B" * 30
    final_text = sm._finalized_text + suffix
    sm.append(suffix)

    # Final compose call: pass the FULL streamed content. stop() should
    # recognize the rolled prefix and only send the suffix to ts2.
    sm.stop(final_text)

    # ts1 was rolled-finalized via chat_update with the prefix.
    assert any(
        call.kwargs.get("ts") == "ts1" and call.kwargs.get("text") == prefix
        for call in client.chat_update.call_args_list
    )
    # ts2 was finalized via chat_update with ONLY the suffix — never the
    # prefix-overlapping content that caused the duplication bug.
    ts2_finalize_calls = [
        call for call in client.chat_update.call_args_list
        if call.kwargs.get("ts") == "ts2" and not call.kwargs.get("text", "").endswith(":robot_face:")
    ]
    assert ts2_finalize_calls, "expected a final chat_update on ts2"
    for call in ts2_finalize_calls:
        text = call.kwargs.get("text", "")
        assert prefix not in text, f"ts2 finalize must not contain the rolled prefix: {text!r}"


def test_streaming_message_roll_seals_unclosed_code_block_with_fence():
    """Regression: when the rolling buffer ends inside an open code
    block, the seal must add a closing ``` so the rolled message
    renders as a balanced block in Slack, and the next placeholder
    must reopen the block with ``` so streaming continues inside the
    same fence. Without this, ts1 leaks an unclosed ``` and the user
    sees the code block run past the message boundary."""
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    client.chat_postMessage.side_effect = [
        {"ok": True, "ts": "ts1"},
        {"ok": True, "ts": "ts2"},
    ]
    client.chat_update.return_value = {"ok": True}

    sm = StreamingMessage(
        client=client, channel="C1", thread_ts="thread-1", min_interval=0.0, max_len=50
    )
    sm.start()

    # Stream content that opens but never closes a code block, large
    # enough to trigger a roll on the next flush.
    open_block = "```python\n" + ("x" * 50)
    sm.append(open_block)

    # ts1 was sealed via chat_update with a closing fence appended.
    seal_calls = [
        c for c in client.chat_update.call_args_list
        if c.kwargs.get("ts") == "ts1"
    ]
    assert seal_calls, "expected chat_update on ts1 during roll"
    last_text = seal_calls[-1].kwargs["text"]
    assert last_text.count(CODE_FENCE) % 2 == 0, f"ts1 still unbalanced: {last_text!r}"
    assert last_text.endswith(CODE_FENCE), f"ts1 missing closing fence: {last_text!r}"

    # ts2 took over and its buffer was pre-loaded with ```\n so the
    # next delta continues inside the reopened code block.
    assert sm.ts == "ts2"
    assert sm._buffer.startswith(CODE_FENCE), f"ts2 buffer missing carry-over: {sm._buffer!r}"

    # _finalized_text tracks the raw streamed text only — no extra
    # closing fence — so stop()'s final_text slice still matches.
    assert sm._finalized_text == open_block


def test_streaming_message_roll_no_carry_when_fence_already_balanced():
    """If the rolling buffer ends with the code block already closed,
    no extra fence should be added on either side."""
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    client.chat_postMessage.side_effect = [
        {"ok": True, "ts": "ts1"},
        {"ok": True, "ts": "ts2"},
    ]
    client.chat_update.return_value = {"ok": True}

    sm = StreamingMessage(
        client=client, channel="C1", thread_ts="thread-1", min_interval=0.0, max_len=50
    )
    sm.start()

    closed_block = "```\nshort code\n```\n" + ("y" * 40)
    sm.append(closed_block)

    seal_calls = [
        c for c in client.chat_update.call_args_list
        if c.kwargs.get("ts") == "ts1"
    ]
    assert seal_calls
    last_text = seal_calls[-1].kwargs["text"]
    # Sealed text equals the raw buffer — no synthetic closing fence.
    assert last_text == closed_block
    # ts2 buffer is empty (no carry).
    assert sm._buffer == ""


def test_streaming_message_stop_unchanged_when_no_roll():
    """Without rolling, stop(final_text) should behave exactly as before:
    one chat_update on the placeholder ts with the full text."""
    client = MagicMock()
    client.api_call.side_effect = SlackApiError("no", {"error": "method_deprecated"})
    client.chat_postMessage.return_value = {"ok": True, "ts": "only-ts"}
    client.chat_update.return_value = {"ok": True}

    sm = StreamingMessage(
        client=client, channel="C1", thread_ts="thread-1", min_interval=0.0, max_len=10_000
    )
    sm.start()
    sm.stop("hello world")

    final_calls = [
        call for call in client.chat_update.call_args_list
        if call.kwargs.get("text") == "hello world"
    ]
    assert len(final_calls) == 1


