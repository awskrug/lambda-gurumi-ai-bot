"""Worker handler for `reaction_added` events.

Dispatch table driven: `REACTION_HANDLERS` maps reaction names to
handler functions. Adding a new reaction = a new `_handle_reaction_*`
function plus one dict entry. The Bolt receiver
(`router._on_reaction_added`) reads the same dict to pre-filter
unregistered reactions before they cost a Lambda async invoke.

Handler signature: `(event, client, api_app_id) -> None`.
The dispatcher (`_process_reaction`) handles `request_id`, the
`item.type == "message"` filter, and per-event dedup before calling,
so each handler focuses on policy + action.
"""
from __future__ import annotations

import uuid
from typing import Any

from slack_sdk import WebClient

from src import runtime
from src.app_metadata import ALLOWED_USER_IDS_ATTR
from src.llms import get_llm
from src.logging_utils import log_event, set_request_id


def _process_reaction(event: dict, client: WebClient, api_app_id: str) -> None:
    """Worker entrypoint for reaction_added events.

    Common pre-flight: validates the payload shape, applies per-event
    dedup, then dispatches to the per-reaction handler from
    REACTION_HANDLERS. Reaction-specific logic (target validation,
    authorization, the actual action) lives in the handler — keeping the
    dispatcher minimal so adding a new reaction is a one-line dict entry.
    """
    set_request_id(str(uuid.uuid4()))

    item = event.get("item") or {}
    if item.get("type") != "message":
        return
    reaction = event.get("reaction") or ""
    handler = REACTION_HANDLERS.get(reaction)
    if handler is None:
        # Defense-in-depth: receiver pre-filters by the same dict, but
        # a malformed/forged payload reaching the worker is silently
        # dropped here.
        return

    channel = item.get("channel")
    message_ts = item.get("ts")
    reactor = event.get("user", "")
    if not channel or not message_ts or not reactor:
        return

    # Common dedup: Slack and Lambda may both re-deliver the same
    # reaction event. event_ts is unique per event firing. Two-stage
    # check mirrors the message path — `done:` is the long-lived marker
    # so a worker that crashed mid-handler can be retried by Lambda
    # async without being silently blocked.
    dedup = runtime._get_dedup()
    dedup_key = f"reaction:{event.get('event_ts') or message_ts}:{reactor}"
    full_key = f"dedup:{dedup_key}"
    try:
        if dedup.is_done(full_key):
            log_event(runtime.logger, "dedup.skip", key=dedup_key, reason="already_done")
            return
        if not dedup.reserve(full_key, user=reactor or "system"):
            log_event(runtime.logger, "dedup.skip", key=dedup_key, reason="in_flight")
            return
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("dedup unavailable, proceeding without it: %s", exc)

    handler(event, client, api_app_id)
    try:
        dedup.mark_done(full_key, user=reactor or "system")
    except Exception as exc:  # noqa: BLE001
        runtime.logger.debug("dedup.mark_done failed: %s", exc)


def _handle_reaction_x_delete(event: dict, client: WebClient, api_app_id: str) -> None:
    """`:x:` → delete the bot-authored message it was attached to.

    Authorization: the reactor must be either (a) the original asker —
    the user whose message started the thread the bot answered in — or
    (b) a user listed in the effective ALLOWED_USER_IDS for this app
    (per-app override > global env var). The target message must be one
    this bot itself authored.
    """
    item = event.get("item") or {}
    channel = item.get("channel")
    message_ts = item.get("ts")
    reactor = event.get("user", "")
    item_user = event.get("item_user", "")

    bot_user_id = runtime._get_bot_user_id(client, api_app_id)
    if not bot_user_id:
        log_event(runtime.logger, "reaction.no_bot_id", api_app_id=api_app_id)
        return
    # When item_user is present and disagrees, the message is not ours —
    # chat.delete would 403. When item_user is missing (some payloads
    # omit it), we proceed and let chat.delete itself enforce.
    if item_user and item_user != bot_user_id:
        log_event(
            runtime.logger,
            "reaction.skip_not_bot_message",
            item_user=item_user,
            channel=channel,
            ts=message_ts,
        )
        return

    # ALLOWED_USER_IDS resolution mirrors the message-path contract:
    # attribute absent → global env var; attribute present → use as-is
    # (including [] for "explicitly allow nobody from this list" — in
    # the reaction context that just means the original-asker check is
    # the only path to authorization for this app).
    app_row: dict | None = None
    try:
        app_row = runtime._get_app_metadata().record(api_app_id, team_id=event.get("team"))
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("app metadata record failed: %s", exc)

    if app_row is not None and ALLOWED_USER_IDS_ATTR in app_row:
        effective_users = list(app_row[ALLOWED_USER_IDS_ATTR])
    else:
        effective_users = list(runtime.settings.allowed_user_ids)

    # Find the original asker. The bot always replies inside a thread —
    # `handlers.message._process` posts with `thread_ts=event.thread_ts
    # or event.ts`, so the bot message either is or sits inside a thread.
    # We can't pass the bot message ts to conversations.replies directly:
    # Slack only treats the parent (thread root) ts as a valid lookup key.
    # Instead:
    #   1. conversations.history(latest=msg_ts, inclusive=True, limit=1)
    #      returns the bot message; we read its `thread_ts` (parent ts).
    #      We deliberately omit `oldest` — passing oldest=latest hits a
    #      Slack quirk where the bracket can return zero messages even
    #      with inclusive=True. `latest+inclusive+limit=1` reliably
    #      returns the message at-or-before that ts (which is the
    #      message itself).
    #   2. conversations.replies(ts=parent_ts, limit=1) returns the
    #      parent message — first (oldest_first) — whose `user` is the
    #      asker.
    original_asker = ""
    parent_ts = ""
    history_messages_count = -1  # -1 = call failed; 0+ = number of messages returned
    try:
        hist = client.conversations_history(
            channel=channel,
            latest=message_ts,
            inclusive=True,
            limit=1,
        )
        hist_messages = (hist.get("messages") if hasattr(hist, "get") else []) or []
        history_messages_count = len(hist_messages)
        if hist_messages:
            bot_msg = hist_messages[0]
            # `thread_ts` is set on thread replies AND on a thread root
            # that has any replies. Either way, lookup the parent.
            parent_ts = bot_msg.get("thread_ts") or bot_msg.get("ts") or ""
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("conversations.history failed: %s", exc)

    if parent_ts and parent_ts != message_ts:
        try:
            resp = client.conversations_replies(channel=channel, ts=parent_ts, limit=1)
            messages = (resp.get("messages") if hasattr(resp, "get") else []) or []
            if messages:
                original_asker = messages[0].get("user", "") or ""
        except Exception as exc:  # noqa: BLE001
            runtime.logger.warning("conversations.replies failed: %s", exc)

    allowed = (
        (original_asker and reactor == original_asker)
        or (reactor in effective_users)
    )
    if not allowed:
        log_event(
            runtime.logger,
            "reaction.unauthorized",
            reactor=reactor,
            channel=channel,
            ts=message_ts,
            api_app_id=api_app_id,
            original_asker=original_asker or "(lookup_failed)",
            parent_ts=parent_ts or "(none)",
            history_messages_count=history_messages_count,
        )
        return

    try:
        client.chat_delete(channel=channel, ts=message_ts)
        log_event(
            runtime.logger,
            "reaction.deleted",
            reactor=reactor,
            channel=channel,
            ts=message_ts,
            api_app_id=api_app_id,
        )
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("chat.delete failed: %s", exc)


def _handle_reaction_image_gen(event: dict, client: WebClient, api_app_id: str) -> None:
    """`:img-gpt:` / `:img-xai:` → generate an image from the reacted
    message's text and upload it as a thread reply.

    Mirrors the slash-command `/img-gpt` / `/img-xai` provider mapping:
    `img-gpt` → OpenAI + `image_model_gpt`, `img-xai` → xAI +
    `image_model_xai`. Posting with `thread_ts` keeps the result
    attached to the original message instead of dumping a top-level
    file into the channel.

    Errors surface as an ephemeral message to the reactor — reactions
    have no `response_url`, and pushing failures into the channel as a
    bot post would pollute the conversation.
    """
    item = event.get("item") or {}
    channel = item.get("channel")
    message_ts = item.get("ts")
    reactor = event.get("user", "")
    reaction = event.get("reaction", "")

    spec = _REACTION_TO_IMAGE.get(reaction)
    if spec is None:
        # Defensive: REACTION_HANDLERS pre-filter should make this
        # unreachable, but if a forged event slips through we drop it.
        return
    image_provider, model_attr = spec
    settings = runtime.settings
    image_model = getattr(settings, model_attr)

    # Fetch the reacted-to message so we can use its text as the prompt
    # and figure out the right thread to post into.
    #
    # Slack's `conversations.history` only returns top-level channel
    # messages — thread replies are NOT included. So if the reaction
    # lands on a thread reply, `history(latest=reply_ts, inclusive=True,
    # limit=1)` returns the closest top-level message at-or-before that
    # ts, which is normally the thread parent (root). We detect this by
    # comparing returned ts vs. `message_ts`: when they differ, the
    # reaction was on a reply and we must fall back to
    # `conversations.replies(ts=parent_ts)` to recover the reply's text.
    # Otherwise the prompt would be the thread root's text — a bug where
    # reacting to any reply in a thread regenerates from the original
    # question.
    prompt = ""
    parent_ts = message_ts
    try:
        hist = client.conversations_history(
            channel=channel,
            latest=message_ts,
            inclusive=True,
            limit=1,
        )
        messages = (hist.get("messages") if hasattr(hist, "get") else []) or []
        if not messages:
            log_event(
                runtime.logger,
                "reaction.image.history_empty",
                channel=channel,
                ts=message_ts,
                api_app_id=api_app_id,
            )
            _notify_reactor(
                client, channel, reactor, "메시지를 읽을 수 없습니다.", thread_ts=message_ts
            )
            return
        msg = messages[0]
        if msg.get("ts") == message_ts:
            # Top-level message (or thread root) — use its text directly.
            prompt = (msg.get("text") or "").strip()
            # If the reacted message is a thread reply, post into the
            # same thread (Slack rejects nesting threads, so we use the
            # parent ts). If it's a top-level message, the message's
            # own ts becomes the new thread root.
            parent_ts = msg.get("thread_ts") or msg.get("ts") or message_ts
        else:
            # Reaction was on a thread reply — msg is the thread parent.
            # Re-fetch via conversations.replies to find the actual reply.
            parent_ts = msg.get("thread_ts") or msg.get("ts") or message_ts
            replies = client.conversations_replies(channel=channel, ts=parent_ts)
            reply_msgs = (replies.get("messages") if hasattr(replies, "get") else []) or []
            target = next(
                (m for m in reply_msgs if m.get("ts") == message_ts),
                None,
            )
            if target is None:
                log_event(
                    runtime.logger,
                    "reaction.image.reply_not_found",
                    channel=channel,
                    ts=message_ts,
                    parent_ts=parent_ts,
                    replies_count=len(reply_msgs),
                    api_app_id=api_app_id,
                )
                _notify_reactor(
                    client, channel, reactor, "메시지를 읽을 수 없습니다.", thread_ts=parent_ts
                )
                return
            prompt = (target.get("text") or "").strip()
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("conversations lookup failed: %s", exc)
        _notify_reactor(
            client, channel, reactor, "메시지를 읽을 수 없습니다.", thread_ts=message_ts
        )
        return

    if not prompt:
        _notify_reactor(
            client, channel, reactor, "이미지 생성에 쓸 텍스트가 없습니다.", thread_ts=parent_ts
        )
        return

    log_event(
        runtime.logger,
        "reaction.image.start",
        reaction=reaction,
        reactor=reactor,
        channel=channel,
        api_app_id=api_app_id,
        image_provider=image_provider,
        image_model=image_model,
    )

    try:
        # Same explicit-LLM-build trick as the slash command worker —
        # bypass the runtime singleton so a single deployment can serve
        # multiple image providers via these reactions.
        llm = get_llm(
            provider=settings.llm_provider,
            model=settings.llm_model,
            image_provider=image_provider,
            image_model=image_model,
            region=settings.aws_region,
            api_keys={"xai": settings.xai_api_key},
        )
        image_bytes = llm.generate_image(prompt)
        client.files_upload_v2(
            channel=channel,
            thread_ts=parent_ts,
            title=image_model,
            filename="generated.png",
            file=image_bytes,
            initial_comment=f"`:{reaction}:` {prompt[:200]}",
        )
    except Exception as exc:  # noqa: BLE001
        # error_message is included at INFO so CloudWatch retains the
        # provider's BadRequest detail (e.g. "size 1024x1024 not supported
        # for gpt-image-2") even when DEBUG traceback is off. Capped to
        # avoid bloating log rows; the full traceback still goes to DEBUG.
        log_event(
            runtime.logger,
            "reaction.image.failure",
            reaction=reaction,
            error_class=exc.__class__.__name__,
            error_message=str(exc)[:500],
            api_app_id=api_app_id,
        )
        runtime.logger.debug("reaction image traceback", exc_info=True)
        _notify_reactor(
            client, channel, reactor, f"이미지 생성 실패: {exc}", thread_ts=parent_ts
        )
        return

    log_event(
        runtime.logger,
        "reaction.image.done",
        reaction=reaction,
        reactor=reactor,
        channel=channel,
        api_app_id=api_app_id,
    )


def _notify_reactor(
    client: WebClient,
    channel: str,
    user: str,
    text: str,
    thread_ts: str = "",
) -> None:
    """Best-effort ephemeral notice to the user who triggered the reaction.

    Used for input/operational errors that the reactor needs to see but
    that should not pollute the channel. Silent on failure — losing the
    notice is preferable to raising into the dispatcher.

    `thread_ts` keeps the ephemeral attached to the same thread the reactor
    is viewing. Without it, the ephemeral lands at channel level and is
    invisible to a reactor whose UI is open on the thread sidebar — which
    is exactly when image-gen reactions are usually triggered.
    """
    if not channel or not user or not text:
        return
    kwargs: dict[str, Any] = {"channel": channel, "user": user, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    try:
        client.chat_postEphemeral(**kwargs)
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("chat.postEphemeral failed: %s", exc)


# `img-{tag}` reaction → (image_provider, settings-attr-name). Mirrors
# `src.handlers.commands._COMMAND_TO_IMAGE` so both interaction modes
# (slash command, reaction) resolve to the same provider/model pairs.
_REACTION_TO_IMAGE: dict[str, tuple[str, str]] = {
    "img-gpt": ("openai", "image_model_gpt"),
    "img-xai": ("xai", "image_model_xai"),
}


# Reaction → handler dispatch. Add a new entry here (and the matching
# handler function above) to wire up another reaction. The Bolt receiver
# pre-filter (`router._on_reaction_added`) reads the same dict so
# unregistered reactions never burn a Lambda async invoke.
REACTION_HANDLERS: dict[str, "callable"] = {
    "x": _handle_reaction_x_delete,
    "img-gpt": _handle_reaction_image_gen,
    "img-xai": _handle_reaction_image_gen,
}
