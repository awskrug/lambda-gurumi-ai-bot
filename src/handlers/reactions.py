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

from slack_sdk import WebClient

from src import runtime
from src.app_metadata import ALLOWED_USER_IDS_ATTR
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
    # reaction event. event_ts is unique per event firing.
    dedup = runtime._get_dedup()
    dedup_key = f"reaction:{event.get('event_ts') or message_ts}:{reactor}"
    try:
        if not dedup.reserve(f"dedup:{dedup_key}", user=reactor or "system"):
            log_event(runtime.logger, "dedup.skip", key=dedup_key)
            return
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning("dedup unavailable, proceeding without it: %s", exc)

    handler(event, client, api_app_id)


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

    # Find the original asker. The bot always replies inside a thread,
    # so the parent message's user is the asker. conversations.replies
    # accepts any in-thread ts and returns oldest_first — limit=1 yields
    # the parent.
    original_asker = ""
    try:
        resp = client.conversations_replies(channel=channel, ts=message_ts, limit=1)
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


# Reaction → handler dispatch. Add a new entry here (and the matching
# handler function above) to wire up another reaction. The Bolt receiver
# pre-filter (`router._on_reaction_added`) reads the same dict so
# unregistered reactions never burn a Lambda async invoke.
REACTION_HANDLERS: dict[str, "callable"] = {
    "x": _handle_reaction_x_delete,
}
