"""Worker handler for message events (`app_mention` + DM `message`).

Drives the full agent loop: dedup, allowlist + per-app override
resolution, throttle check, streaming setup, agent run, history
persist. Reached from `router._process_worker` after secrets are
resolved and the WebClient is minted.

All references to module-level state go through `runtime.X` so test
monkeypatch on `src.runtime` is honored.
"""
from __future__ import annotations

import re
import uuid

from src import runtime
from src.agent import SlackMentionAgent
from src.app_metadata import (
    ALLOWED_CHANNEL_IDS_ATTR,
    ALLOWED_USER_IDS_ATTR,
    PERSONA_MESSAGE_ATTR,
)
from src.logging_utils import log_event, set_request_id
from src.slack_helpers import (
    MessageFormatter,
    StreamingMessage,
    channel_allowed,
    sanitize_error,
    set_thread_status,
    user_name_cache,
)
from src.tools import ToolContext, default_registry


LABELS = {
    "ko": {
        "generated_image": "생성된 이미지",
        "error_prefix": "요청 처리 중 오류가 발생했습니다",
        "throttled": "잠시 후 다시 시도해주세요. 처리 중인 요청이 많습니다.",
        "thinking": "생각 중... ",
        "max_steps": "답변 정리 중... ",
        "using_tools": "도구 사용 중: {tools}",
        "tool_ok": "도구 완료: {tool}",
        "tool_failed": "도구 실패: {tool}",
        "composing": "답변 작성 중...",
    },
    "en": {
        "generated_image": "Generated image",
        "error_prefix": "An error occurred while processing your request",
        "throttled": "Too many in-flight requests. Please try again shortly.",
        "thinking": "Thinking... ",
        "max_steps": "Finalizing... ",
        "using_tools": "Running tools: {tools}",
        "tool_ok": "Finished: {tool}",
        "tool_failed": "Failed: {tool}",
        "composing": "Composing the answer...",
    },
}


def _labels() -> dict[str, str]:
    return LABELS.get(runtime.settings.response_language, LABELS["en"])


# Matches Slack user mentions so we can (a) extract referenced user_ids
# for cache pre-warm and (b) selectively strip just the bot's own mention.
# Other users' mentions stay intact so the LLM can pass `<@U…>` straight
# to `fetch_user_profile`, whose `_resolve_user_id` parses the syntax.
_USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


def _strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove only `<@BOT_USER_ID>` (and its `|alias` form). Other user
    mentions stay in place so the LLM sees them as `<@U…>` and
    `_resolve_user_id` can parse them in `fetch_user_profile`."""
    if not text or not bot_user_id:
        return text
    pattern = re.compile(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>")
    return pattern.sub("", text).strip()


def _process(event: dict, client, say, is_dm: bool, api_app_id: str = "") -> None:  # noqa: ANN001
    set_request_id(str(uuid.uuid4()))
    labels = _labels()
    raw_text = event.get("text", "")
    # Strip ONLY the bot's own mention. Other `<@U…>` mentions stay so
    # the LLM can pass them straight into fetch_user_profile (which
    # parses mention syntax).
    bot_user_id = runtime._get_bot_user_id(client, api_app_id) if api_app_id else ""
    text = _strip_bot_mention(raw_text, bot_user_id).strip() if bot_user_id else raw_text.strip()
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    user = event.get("user", "")

    # Drop empty mentions (bare "@bot" with no prompt) BEFORE reserving a
    # dedup slot. Otherwise every empty ping burns a 1h TTL row on the
    # dedup table and, for no-op messages, shows up as a dedup.skip on any
    # Slack retry even though there was never anything to do.
    if not text:
        return

    dedup = runtime._get_dedup()
    dedup_key = event.get("client_msg_id") or f"{channel}:{event.get('ts')}"
    full_key = f"dedup:{dedup_key}"
    # Two-stage check: a `done:` marker means an earlier attempt already
    # finished successfully — short-circuit so retries don't re-run the
    # agent. The `dedup:` reservation only blocks parallel/in-flight
    # duplicates within a short TTL window; if the first worker died,
    # that row TTL'd out and the retry needs to be allowed through.
    try:
        if dedup.is_done(full_key):
            log_event(runtime.logger, "dedup.skip", key=dedup_key, reason="already_done")
            return
        if not dedup.reserve(full_key, user=user or "system"):
            log_event(runtime.logger, "dedup.skip", key=dedup_key, reason="in_flight")
            return
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("dedup unavailable, proceeding without it: %s", exc)

    # Record this app's metadata only after dedup passes — Slack retries
    # and our own re-deliveries shouldn't bump last_seen_at, and known-bad
    # messages (empty text) shouldn't appear in the registry as activity.
    # `record(...)` returns the full row (ALL_NEW), which carries any per-app
    # ACL overrides the operator set via the CLI — so we resolve effective
    # allowlists in the same DynamoDB roundtrip we already needed for the
    # write, no extra GetItem.
    app_row: dict | None = None
    if api_app_id:
        try:
            app_row = runtime._get_app_metadata().record(api_app_id, team_id=event.get("team"))
        except Exception as exc:  # noqa: BLE001
            runtime.logger.warning("app metadata record failed: %s", exc)

    # Per-app override resolution (applies to both the list ACLs and the
    # string persona):
    #   - attribute ABSENT in row    → use the global env var
    #   - attribute PRESENT in row   → use per-app value, IGNORE the global
    # Empty list / empty string is preserved as a meaningful PRESENT value:
    # `[]` means "this app explicitly allows all" (overrides a non-empty
    # global allowlist), and `""` means "this app has no persona" (overrides
    # a non-empty global PERSONA_MESSAGE). DynamoDB distinguishes these from
    # the attribute being absent, so we mirror that distinction here.
    def _effective(attr, fallback):
        if app_row is None or attr not in app_row:
            return fallback
        return app_row[attr]

    effective_channels = list(_effective(ALLOWED_CHANNEL_IDS_ATTR, runtime.settings.allowed_channel_ids))
    effective_users = list(_effective(ALLOWED_USER_IDS_ATTR, runtime.settings.allowed_user_ids))
    effective_persona = _effective(PERSONA_MESSAGE_ATTR, runtime.settings.persona_message)

    # Channel allowlist applies to public/private channels only. DMs use
    # per-channel IDs (D-prefix) that aren't normally enrolled in the
    # allowlist — enforcing there would lock out every user's direct-message
    # path the moment an operator sets ALLOWED_CHANNEL_IDS. Slack's own
    # workspace install permission already gates who can open the DM.
    # Both check AND `{}` substitution use the EFFECTIVE list so the
    # message points at a per-app channel when overridden.
    if not is_dm and not channel_allowed(channel, effective_channels):
        msg = runtime.settings.allowed_channel_message or ""
        if msg and "{}" in msg and effective_channels:
            msg = msg.replace("{}", f"<#{effective_channels[0]}>")
        if msg:
            say(text=msg, thread_ts=thread_ts)
        log_event(runtime.logger, "channel.blocked", channel=channel, api_app_id=api_app_id)
        return

    # User allowlist applies to channels AND DMs. Unlike the channel allowlist
    # (which exempts DMs because DM channel IDs are D-prefixed and wouldn't
    # be enrolled), restricting *who* can talk to the bot is meaningful in
    # both directions — arguably more so in DMs, where there's no channel-
    # level gate at all. Operator opts in via ALLOWED_USER_IDS (or per-app
    # override); empty list means everyone is allowed.
    if effective_users and user not in effective_users:
        # Silent drop — we deliberately do NOT post a "you're not allowed"
        # message back to the user. Surfacing the bot's existence to
        # outsiders has no upside, and the per-app+global ALLOWED_USER_IDS
        # is meant to be a quiet gate, not a public denial. The block is
        # only visible via the user.blocked log event for operators.
        log_event(runtime.logger, "user.blocked", user=user, channel=channel, api_app_id=api_app_id)
        return

    try:
        active = dedup.count_user_active(user)
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("throttle count unavailable: %s", exc)
        active = 0
    if active >= runtime.settings.max_throttle_count:
        say(text=labels["throttled"], thread_ts=thread_ts)
        log_event(runtime.logger, "throttle.limit", user=user, active=active)
        return

    # Show a typing-style status indicator while the bot is "working" with
    # nothing to reply yet. We intentionally do NOT post a placeholder
    # chat.postMessage up front: that would render as a separate UI element
    # alongside the status line (a duplicate-message look on AI workspaces).
    # The placeholder is posted lazily in _on_stream_wrapped once the first
    # real content delta arrives. Slack auto-clears the status when the bot
    # posts in the thread; we also explicitly clear it after we finalize.
    set_thread_status(client, channel, thread_ts, labels["thinking"] + runtime.settings.bot_cursor)

    stream_msg = StreamingMessage(
        client=client,
        channel=channel,
        thread_ts=thread_ts,
        placeholder=runtime.settings.bot_cursor,
        min_interval=0.6,
        max_len=runtime.settings.max_len_slack,
    )

    def _on_stream_wrapped(delta: str) -> None:
        """Defer placeholder posting until the first real content arrives."""
        if not delta:
            return
        if stream_msg.ts is None:
            try:
                stream_msg.start()
            except Exception as exc:  # noqa: BLE001
                runtime.logger.warning("deferred streaming message start failed: %s", exc)
                return
        stream_msg.append(delta)

    # Pre-warm display names for every user mentioned in the message so
    # `fetch_user_profile` resolves on the first attempt even when the
    # LLM passes a free-text name. Cheap: parallel `users.info` via
    # `UserNameCache.warm`; cached across the warm container so a thread
    # that re-mentions the same user pays once.
    mentioned_ids = {
        m
        for m in _USER_MENTION_RE.findall(raw_text)
        if m and m != bot_user_id
    }
    if mentioned_ids:
        try:
            user_name_cache.warm(client, mentioned_ids)
        except Exception as exc:  # noqa: BLE001
            runtime.logger.debug("mention pre-warm failed: %s", exc)

    history_store = runtime._get_conversations()
    history = history_store.get(thread_ts)

    # User-scoped persistent memory — auto-loaded once per agent run so
    # `remember`/`forget` writes from this turn don't appear in the same
    # turn's prompt (would tempt the LLM into a self-confirming loop).
    # The next turn picks up the change.
    user_memory: list[dict] = []
    if user:
        try:
            user_memory = runtime._get_memory().get(user)
        except Exception as exc:  # noqa: BLE001
            runtime.logger.warning("memory load failed: %s", exc)

    llm = runtime._get_llm()
    context = ToolContext(
        slack_client=client,
        channel=channel,
        thread_ts=thread_ts,
        event=event,
        settings=runtime.settings,
        llm=llm,
        user_id=user,
    )

    def _on_step(step_num: int, phase: str, detail: dict) -> None:
        # While no message is posted yet, use assistant_threads.setStatus.
        # Once the stream has started (stream_msg.ts is set), the bot message
        # is already visible — skip status updates to avoid re-triggering the
        # duplicate-UI problem.
        if stream_msg.ts is not None:
            return
        if phase == "tool_use":
            tools = ", ".join(detail.get("tools") or [])
            status = labels["using_tools"].format(tools=tools)
        elif phase == "tool_result":
            key = "tool_ok" if detail.get("ok") else "tool_failed"
            status = labels[key].format(tool=detail.get("tool") or "")
        elif phase == "compose":
            status = labels["max_steps"] if detail.get("max_steps_hit") else labels["composing"]
        else:
            return
        set_thread_status(client, channel, thread_ts, status + " " + runtime.settings.bot_cursor)

    agent = SlackMentionAgent(
        llm=llm,
        context=context,
        registry=default_registry,
        max_steps=runtime.settings.agent_max_steps,
        response_language=runtime.settings.response_language,
        # SYSTEM_MESSAGE is global only — it's a security/policy field that
        # stays consistent across the deployment. PERSONA_MESSAGE has a
        # per-app override (resolved above) since it's just answer style.
        system_message=runtime.settings.system_message,
        persona_message=effective_persona,
        history=history,
        user_memory=user_memory,
        on_stream=_on_stream_wrapped,
        on_step=_on_step,
        max_output_tokens=runtime.settings.max_output_tokens,
    )

    user_name = user_name_cache.get(client, user) if user else ""
    log_event(runtime.logger, "agent.start", user=user_name or user, channel=channel, is_dm=is_dm, api_app_id=api_app_id)

    try:
        result = agent.run(text)
    except Exception as exc:  # noqa: BLE001
        # Sanitize before logging — provider SDK tracebacks can carry
        # `Authorization: Bearer ...` headers that would otherwise land in
        # CloudWatch verbatim. Full traceback is preserved at DEBUG only.
        log_event(
            runtime.logger,
            "agent.failure",
            error_class=exc.__class__.__name__,
            reason=sanitize_error(exc),
        )
        runtime.logger.debug("agent failure traceback", exc_info=True)
        error_text = f"{labels['error_prefix']}: {sanitize_error(exc)}"
        if stream_msg.ts is not None:
            stream_msg.stop(error_text)
        else:
            say(text=error_text, thread_ts=thread_ts)
        set_thread_status(client, channel, thread_ts, "")
        return

    final_text = result.text or "(응답을 생성하지 못했습니다)"
    # StreamingMessage.stop() handles split + follow-up postMessage internally,
    # AND skips any prefix already sealed into earlier rolled ts'es by the
    # size-overflow roll path. Pass full final_text so the slice can match.
    # When no placeholder exists (no stream deltas ever arrived), post the
    # chunks as fresh thread messages.
    if stream_msg.ts is not None:
        stream_msg.stop(final_text)
    else:
        chunks = MessageFormatter.split_message(final_text, max_len=runtime.settings.max_len_slack)
        for chunk in chunks:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk)
    # Explicitly clear the typing-style status indicator. Slack usually
    # auto-clears it when the bot posts a reply, but an explicit clear
    # ensures there's no stale line left over from the last on_step update.
    set_thread_status(client, channel, thread_ts, "")
    # NOTE: do not post `result.image_url` as a separate text message —
    # the image is already uploaded inline to the thread by the
    # generate_image tool, and the LLM's reply is instructed to omit
    # the permalink. A trailing "생성된 이미지: <url>" line would just
    # duplicate what the user already sees.

    new_history = [
        *history,
        {"role": "user", "content": text},
        {"role": "assistant", "content": final_text},
    ]
    try:
        history_store.put(
            thread_ts,
            user=user or "unknown",
            messages=new_history,
            max_chars=runtime.settings.max_history_chars,
        )
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("conversation persist failed: %s", exc)

    # Mark the long-lived completion marker AFTER the response is delivered
    # and history is persisted. A retry that loses the race against the
    # short-TTL `dedup:` row will see `done:` and short-circuit cleanly.
    try:
        dedup.mark_done(full_key, user=user or "system")
    except Exception as exc:  # noqa: BLE001
        # Non-fatal: at worst a future retry re-runs the agent. The dedup
        # store itself logs its own warning; this catch handles a totally
        # absent dedup (in degraded mode above we may have skipped reserve).
        runtime.logger.debug("dedup.mark_done failed: %s", exc)

    log_event(
        runtime.logger,
        "agent.done",
        steps=result.steps,
        tool_calls=result.tool_calls_count,
        tokens_in=result.token_usage.get("input", 0),
        tokens_out=result.token_usage.get("output", 0),
    )
