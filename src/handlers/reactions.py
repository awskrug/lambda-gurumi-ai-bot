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


def _lookup_reacted_message(
    client: WebClient,
    channel: str,
    message_ts: str,
    api_app_id: str,
) -> tuple[dict | None, str, list[dict]]:
    """Resolve a reacted-to message and the thread it lives in.

    `conversations.replies(ts=root_ts)` returns the full thread
    oldest-first. Keyed by a *reply* ts, Slack returns only that single
    reply (carrying a `thread_ts` that points at the root) — not the
    whole thread. So when the reacted message is a thread reply, a second
    `replies` call keyed by its `thread_ts` recovers the root and the
    full thread. `thread[0]` is then the thread root and its `user` the
    original asker.

    `conversations.history(latest=ts, limit=1)` is the fallback for when
    the primary `replies` call fails (missing scope, transient outage).
    The history result is accepted only when the returned ts equals
    `message_ts` exactly — otherwise Slack returns the nearest top-level
    message at-or-before that ts, which can be the root of an unrelated
    thread.

    Returns:
      target: the reacted message dict, or None if not found.
      parent_ts: thread root ts (= `message_ts` for a top-level message
        or for the "not found" case).
      thread: full thread oldest-first; `[target]` when reached via the
        history fallback; `[]` otherwise.
    """
    target: dict | None = None
    parent_ts = message_ts
    thread: list[dict] = []

    try:
        replies = client.conversations_replies(
            channel=channel,
            ts=message_ts,
            limit=200,
        )
        reply_msgs = (replies.get("messages") if hasattr(replies, "get") else []) or []
        if reply_msgs:
            thread = reply_msgs
            target = next(
                (m for m in reply_msgs if m.get("ts") == message_ts),
                None,
            )
            parent_ts = reply_msgs[0].get("ts") or message_ts
    except Exception as exc:  # noqa: BLE001
        log_event(
            runtime.logger,
            "reaction.lookup.replies_failed",
            channel=channel,
            ts=message_ts,
            error_class=exc.__class__.__name__,
            error_message=str(exc)[:200],
            api_app_id=api_app_id,
        )

    if target is None:
        try:
            hist = client.conversations_history(
                channel=channel,
                latest=message_ts,
                inclusive=True,
                limit=1,
            )
            hist_msgs = (hist.get("messages") if hasattr(hist, "get") else []) or []
            if hist_msgs and hist_msgs[0].get("ts") == message_ts:
                target = hist_msgs[0]
                parent_ts = target.get("thread_ts") or target.get("ts") or message_ts
                thread = [target]
        except Exception as exc:  # noqa: BLE001
            log_event(
                runtime.logger,
                "reaction.lookup.history_failed",
                channel=channel,
                ts=message_ts,
                error_class=exc.__class__.__name__,
                error_message=str(exc)[:200],
                api_app_id=api_app_id,
            )

    # The reacted message is a thread reply when its `thread_ts` differs
    # from its own ts. The primary `replies` call keyed by that reply ts
    # returned only the reply, so `thread[0]`/`parent_ts` still point at
    # the reply, not the root. Re-fetch keyed by the reply's `thread_ts`
    # to recover the root (and thus the original asker) and the full
    # thread.
    root_ts = (target.get("thread_ts") if target else "") or ""
    if root_ts and root_ts != message_ts:
        try:
            root_replies = client.conversations_replies(
                channel=channel,
                ts=root_ts,
                limit=200,
            )
            root_msgs = (root_replies.get("messages") if hasattr(root_replies, "get") else []) or []
            if root_msgs:
                thread = root_msgs
                parent_ts = root_msgs[0].get("ts") or root_ts
                target = next(
                    (m for m in root_msgs if m.get("ts") == message_ts),
                    target,
                )
        except Exception as exc:  # noqa: BLE001
            log_event(
                runtime.logger,
                "reaction.lookup.root_replies_failed",
                channel=channel,
                ts=root_ts,
                error_class=exc.__class__.__name__,
                error_message=str(exc)[:200],
                api_app_id=api_app_id,
            )

    return target, parent_ts, thread


def _handle_reaction_x_delete(event: dict, client: WebClient, api_app_id: str) -> None:
    """`:x:` → delete the bot-authored message it was attached to.

    Authorization: the reactor must be either (a) the original asker —
    the user who started the thread the bot replied in — or (b) a user
    listed in the effective ALLOWED_USER_IDS for this app (per-app
    override > global env var). The target message must be one this bot
    itself authored.
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
    # item_user present and disagreeing → not our message; chat.delete
    # would 403. When absent, proceed and let chat.delete itself enforce.
    if item_user and item_user != bot_user_id:
        log_event(
            runtime.logger,
            "reaction.skip_not_bot_message",
            item_user=item_user,
            channel=channel,
            ts=message_ts,
        )
        return

    # ALLOWED_USER_IDS resolution: attribute absent → global env var;
    # attribute present → use as-is (including `[]` as an explicit empty
    # override, in which case the original-asker check is the only path).
    app_row: dict | None = None
    try:
        app_row = runtime._get_app_metadata().record(api_app_id, team_id=event.get("team"))
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("app metadata record failed: %s", exc)

    if app_row is not None and ALLOWED_USER_IDS_ATTR in app_row:
        effective_users = list(app_row[ALLOWED_USER_IDS_ATTR])
    else:
        effective_users = list(runtime.settings.allowed_user_ids)

    # The bot posts inside a thread (`handlers.message._process` sets
    # `thread_ts=event.thread_ts or event.ts`), so the bot message is
    # either a thread reply or a thread root that has replies. Either
    # way, `thread[0]` from the common lookup is the thread root and
    # its `user` is the original asker.
    _target, parent_ts, thread = _lookup_reacted_message(
        client, channel, message_ts, api_app_id
    )
    original_asker = ""
    if thread and parent_ts != message_ts:
        original_asker = thread[0].get("user", "") or ""

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
    attached to the original message. Errors surface as an ephemeral to
    the reactor — reactions have no `response_url`, and pushing failures
    into the channel as a bot post would pollute the conversation.
    """
    item = event.get("item") or {}
    channel = item.get("channel")
    message_ts = item.get("ts")
    reactor = event.get("user", "")
    reaction = event.get("reaction", "")

    spec = _REACTION_TO_IMAGE.get(reaction)
    if spec is None:
        # Defense-in-depth: REACTION_HANDLERS pre-filter normally blocks
        # this, but a forged payload reaching the worker is dropped here.
        return
    image_provider, model_attr = spec
    settings = runtime.settings
    image_model = getattr(settings, model_attr)

    target, parent_ts, _thread = _lookup_reacted_message(
        client, channel, message_ts, api_app_id
    )
    if target is None:
        log_event(
            runtime.logger,
            "reaction.image.message_not_found",
            channel=channel,
            ts=message_ts,
            api_app_id=api_app_id,
        )
        _notify_reactor(
            client, channel, reactor, "메시지를 읽을 수 없습니다.", thread_ts=message_ts
        )
        return

    prompt = (target.get("text") or "").strip()
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
        # Build the LLM explicitly (not via the runtime singleton) so
        # one deployment can serve multiple image providers — same trick
        # the slash-command worker uses.
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
            initial_comment=f":{reaction}: {prompt[:200]}",
        )
    except Exception as exc:  # noqa: BLE001
        # error_message at INFO so CloudWatch retains the provider's
        # BadRequest detail even with DEBUG traceback off; capped to
        # avoid bloating log rows.
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
