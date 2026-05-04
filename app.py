"""AWS Lambda entrypoint for the Slack mention bot.

Multi-tenant routing
====================

This Lambda serves multiple distinct Slack apps. Each app's secrets
(`signing_secret` + `bot_token`) live in SSM Parameter Store under
`{SSM_PARAMS_PREFIX}/{api_app_id}/...` and are looked up per request via
`CredentialsStore` (with a 5-min TTL cache to absorb the per-request SSM
cost on warm containers). DynamoDB row `app:{api_app_id}` records when
each app was first seen and last seen — populated lazily on the first
event we successfully process from that app, so a registry of installed
apps materializes automatically with no separate registration flow.

Receiver path (Slack → API Gateway → Lambda):
  1. lambda_handler short-circuits Slack retries (X-Slack-Retry-Num).
  2. Parses the request body to extract `api_app_id`. If the body is a
     URL-verification handshake (no api_app_id), echo the challenge
     directly without signature verification — the body carries no
     actionable payload, so allowing this without a known signing_secret
     is the cleanest way to break the chicken-and-egg.
  3. Looks up the app's secrets in SSM. Missing → structured warn log
     + HTTP 200 (so Slack doesn't retry an unrecoverable misconfig).
  4. Dispatches to a per-app cached Bolt App that verifies the signature
     with that app's signing_secret and routes to the event handlers.
  5. Each handler ack()s, then fires a fire-and-forget self-invoke with
     `_worker=True` plus `api_app_id` so the worker can fetch its own
     bot token from SSM (we don't ship secrets through the async invoke
     payload — Lambda invoke payloads can show up in CloudTrail).

Worker path (Lambda async self-invoke):
  1. `_worker=True` skips Bolt; we re-fetch `bot_token` from SSM keyed
     on the carried `api_app_id` and run the full agent.
  2. Same dedup row absorbs Slack's retry burst on the receiver side
     AND Lambda async's built-in 2x retry on worker failure — all paths
     converge on the same `dedup:{client_msg_id}` key.
"""
from __future__ import annotations

import base64
import json
import os
import re
import uuid

import boto3
from slack_bolt import App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk import WebClient

from src.agent import SlackMentionAgent
from src.app_metadata import (
    ALLOWED_CHANNEL_IDS_ATTR,
    ALLOWED_USER_IDS_ATTR,
    AppMetadataStore,
)
from src.config import Settings
from src.credentials import CredentialsStore
from src.dedup import ConversationStore, DedupStore
from src.llms import get_llm
from src.logging_utils import get_logger, log_event, set_request_id
from src.slack_helpers import (
    MessageFormatter,
    StreamingMessage,
    channel_allowed,
    sanitize_error,
    set_thread_status,
    user_name_cache,
)


from src.tools import ToolContext, default_registry

settings = Settings.from_env()
logger = get_logger("app")

_llm = None
_dedup: DedupStore | None = None
_conversations: ConversationStore | None = None
_credentials: CredentialsStore | None = None
_app_metadata: AppMetadataStore | None = None
# api_app_id -> ((signing_secret, bot_token), App). The secret tuple is
# the cache *value* not the *key* so we can detect rotation: when
# CredentialsStore returns a different tuple after its TTL refreshes, we
# rebuild the App so signature verification uses the new secret without
# requiring a container restart.
_bolt_apps: dict[str, tuple[tuple[str, str], App]] = {}
_lambda_client = None


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
    return LABELS.get(settings.response_language, LABELS["en"])


def _get_llm():
    global _llm
    if _llm is None:
        _llm = get_llm(
            provider=settings.llm_provider,
            model=settings.llm_model,
            image_provider=settings.image_provider,
            image_model=settings.image_model,
            region=settings.aws_region,
            api_keys={"xai": settings.xai_api_key},
        )
    return _llm


def _get_dedup() -> DedupStore:
    global _dedup
    if _dedup is None:
        _dedup = DedupStore(table_name=settings.dynamodb_table_name, region=settings.aws_region)
    return _dedup


def _get_conversations() -> ConversationStore:
    global _conversations
    if _conversations is None:
        _conversations = ConversationStore(table_name=settings.dynamodb_table_name, region=settings.aws_region)
    return _conversations


def _get_credentials() -> CredentialsStore:
    global _credentials
    if _credentials is None:
        _credentials = CredentialsStore(
            region=settings.aws_region,
            prefix=settings.ssm_params_prefix,
            ttl_seconds=settings.ssm_cache_ttl_seconds,
        )
    return _credentials


def _get_app_metadata() -> AppMetadataStore:
    global _app_metadata
    if _app_metadata is None:
        _app_metadata = AppMetadataStore(
            table_name=settings.dynamodb_table_name,
            region=settings.aws_region,
        )
    return _app_metadata


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=settings.aws_region)
    return _lambda_client


def _enqueue_worker(event: dict, is_dm: bool, api_app_id: str) -> None:
    """Fire-and-forget async self-invoke of the worker path.

    `api_app_id` is carried through the payload so the worker can fetch
    its own bot token from SSM. We deliberately do NOT pass tokens
    through the payload — Lambda invoke payloads can be visible in
    CloudTrail and downstream tooling, so secrets stay in SSM.
    """
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    inline_payload = {"slack_event": event, "is_dm": is_dm, "api_app_id": api_app_id}
    if not function_name:
        _process_worker(inline_payload)
        return
    payload = json.dumps(
        {"_worker": True, **inline_payload}, ensure_ascii=False
    ).encode("utf-8")
    try:
        _get_lambda_client().invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=payload,
        )
    except Exception:
        # If async invoke fails (IAM, throttling, network), fall back to
        # inline execution so the user's message isn't dropped — same
        # behavior as before the multi-tenant refactor.
        logger.exception("async worker invoke failed, running inline")
        _process_worker(inline_payload)


def _process_worker(payload: dict) -> None:
    """Worker path: full agent run.

    Re-fetches the bot token from SSM keyed on `api_app_id` carried in
    the payload. Bolt's injected WebClient is gone by this point — it
    lived in the receiver process — so we mint a fresh one.
    """
    slack_event = payload.get("slack_event") or {}
    is_dm = bool(payload.get("is_dm"))
    api_app_id = payload.get("api_app_id") or ""
    channel = slack_event.get("channel")

    if not api_app_id:
        log_event(logger, "worker.no_app_id", channel=channel)
        return
    creds = _get_credentials().get(api_app_id)
    if creds is None:
        log_event(
            logger,
            "worker.unknown_app",
            api_app_id=api_app_id,
            channel=channel,
            note=f"missing SSM SecureString at {settings.ssm_params_prefix}/{api_app_id}/{{signing_secret,bot_token}}",
        )
        return

    client = WebClient(token=creds.bot_token)

    def _say(text: str, thread_ts: str | None = None) -> None:
        kwargs: dict = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        client.chat_postMessage(**kwargs)

    _process(slack_event, client, _say, is_dm=is_dm, api_app_id=api_app_id)


def _get_bolt_app(api_app_id: str, signing_secret: str, bot_token: str) -> App:
    """Per-app Bolt App, cached on warm containers.

    Cache key is `api_app_id`; the (signing_secret, bot_token) tuple is
    stored alongside so we can detect rotation: when CredentialsStore
    returns a different tuple after its TTL refreshes, we rebuild the App
    so signature verification uses the new secret.
    """
    cached = _bolt_apps.get(api_app_id)
    if cached and cached[0] == (signing_secret, bot_token):
        return cached[1]
    app = App(
        token=bot_token,
        signing_secret=signing_secret,
        process_before_response=True,
        # Skip Bolt's auth.test on init — multi-tenant containers would
        # otherwise pay one Slack API roundtrip per app per cold container.
        # Bolt still verifies signatures on each request, which is what we
        # actually need from it here.
        token_verification_enabled=False,
    )

    @app.event("app_mention")
    def _on_mention(event, body, ack):  # noqa: ANN001
        ack()
        _enqueue_worker(event, is_dm=False, api_app_id=(body or {}).get("api_app_id", ""))

    @app.event("message")
    def _on_message(event, body, ack):  # noqa: ANN001
        ack()
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        _enqueue_worker(event, is_dm=True, api_app_id=(body or {}).get("api_app_id", ""))

    _bolt_apps[api_app_id] = ((signing_secret, bot_token), app)
    return app


MENTION_RE = re.compile(r"<@[^>]+>")


def _process(event: dict, client, say, is_dm: bool, api_app_id: str = "") -> None:  # noqa: ANN001
    set_request_id(str(uuid.uuid4()))
    labels = _labels()
    text = MENTION_RE.sub("", event.get("text", "")).strip()
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    user = event.get("user", "")

    # Drop empty mentions (bare "@bot" with no prompt) BEFORE reserving a
    # dedup slot. Otherwise every empty ping burns a 1h TTL row on the
    # dedup table and, for no-op messages, shows up as a dedup.skip on any
    # Slack retry even though there was never anything to do.
    if not text:
        return

    dedup = _get_dedup()
    dedup_key = event.get("client_msg_id") or f"{channel}:{event.get('ts')}"
    try:
        if not dedup.reserve(f"dedup:{dedup_key}", user=user or "system"):
            log_event(logger, "dedup.skip", key=dedup_key)
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning("dedup unavailable, proceeding without it: %s", exc)

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
            app_row = _get_app_metadata().record(api_app_id, team_id=event.get("team"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("app metadata record failed: %s", exc)

    # Three-state ACL resolution per attribute:
    #   - attribute ABSENT in row    → global env var (back-compat)
    #   - attribute PRESENT in row   → per-app value, IGNORE global
    #   - attribute is `[]`          → "this app explicitly allows all" —
    #                                   overrides even a non-empty global
    # The third state matters: an operator may want one app to be carved
    # out as unrestricted even when the deployment-wide default is locked
    # down. DynamoDB distinguishes empty list from missing attribute, so
    # we mirror that distinction here.
    def _effective(attr: str, fallback: list[str]) -> list[str]:
        if app_row is None or attr not in app_row:
            return fallback
        return list(app_row[attr])

    effective_channels = _effective(ALLOWED_CHANNEL_IDS_ATTR, settings.allowed_channel_ids)
    effective_users = _effective(ALLOWED_USER_IDS_ATTR, settings.allowed_user_ids)

    # Channel allowlist applies to public/private channels only. DMs use
    # per-channel IDs (D-prefix) that aren't normally enrolled in the
    # allowlist — enforcing there would lock out every user's direct-message
    # path the moment an operator sets ALLOWED_CHANNEL_IDS. Slack's own
    # workspace install permission already gates who can open the DM.
    # Both check AND `{}` substitution use the EFFECTIVE list so the
    # message points at a per-app channel when overridden.
    if not is_dm and not channel_allowed(channel, effective_channels):
        msg = settings.allowed_channel_message or ""
        if msg and "{}" in msg and effective_channels:
            msg = msg.replace("{}", f"<#{effective_channels[0]}>")
        if msg:
            say(text=msg, thread_ts=thread_ts)
        log_event(logger, "channel.blocked", channel=channel, api_app_id=api_app_id)
        return

    # User allowlist applies to channels AND DMs. Unlike the channel allowlist
    # (which exempts DMs because DM channel IDs are D-prefixed and wouldn't
    # be enrolled), restricting *who* can talk to the bot is meaningful in
    # both directions — arguably more so in DMs, where there's no channel-
    # level gate at all. Operator opts in via ALLOWED_USER_IDS (or per-app
    # override); empty list means everyone is allowed.
    if effective_users and user not in effective_users:
        msg = settings.allowed_user_message or ""
        if msg and "{}" in msg:
            msg = msg.replace("{}", f"<@{effective_users[0]}>")
        if msg:
            say(text=msg, thread_ts=thread_ts)
        log_event(logger, "user.blocked", user=user, channel=channel, api_app_id=api_app_id)
        return

    try:
        active = dedup.count_user_active(user)
    except Exception as exc:  # noqa: BLE001
        logger.warning("throttle count unavailable: %s", exc)
        active = 0
    if active >= settings.max_throttle_count:
        say(text=labels["throttled"], thread_ts=thread_ts)
        log_event(logger, "throttle.limit", user=user, active=active)
        return

    # Show a typing-style status indicator while the bot is "working" with
    # nothing to reply yet. We intentionally do NOT post a placeholder
    # chat.postMessage up front: that would render as a separate UI element
    # alongside the status line (a duplicate-message look on AI workspaces).
    # The placeholder is posted lazily in _on_stream_wrapped once the first
    # real content delta arrives. Slack auto-clears the status when the bot
    # posts in the thread; we also explicitly clear it after we finalize.
    set_thread_status(client, channel, thread_ts, labels["thinking"] + settings.bot_cursor)

    stream_msg = StreamingMessage(
        client=client,
        channel=channel,
        thread_ts=thread_ts,
        placeholder=settings.bot_cursor,
        min_interval=0.6,
        max_len=settings.max_len_slack,
    )

    def _on_stream_wrapped(delta: str) -> None:
        """Defer placeholder posting until the first real content arrives."""
        if not delta:
            return
        if stream_msg.ts is None:
            try:
                stream_msg.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning("deferred streaming message start failed: %s", exc)
                return
        stream_msg.append(delta)

    history_store = _get_conversations()
    history = history_store.get(thread_ts)

    llm = _get_llm()
    context = ToolContext(
        slack_client=client,
        channel=channel,
        thread_ts=thread_ts,
        event=event,
        settings=settings,
        llm=llm,
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
        set_thread_status(client, channel, thread_ts, status + " " + settings.bot_cursor)

    agent = SlackMentionAgent(
        llm=llm,
        context=context,
        registry=default_registry,
        max_steps=settings.agent_max_steps,
        response_language=settings.response_language,
        system_message=settings.system_message,
        persona_message=settings.persona_message,
        history=history,
        on_stream=_on_stream_wrapped,
        on_step=_on_step,
        max_output_tokens=settings.max_output_tokens,
    )

    user_name = user_name_cache.get(client, user) if user else ""
    log_event(logger, "agent.start", user=user_name or user, channel=channel, is_dm=is_dm, api_app_id=api_app_id)

    try:
        result = agent.run(text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent failure")
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
        chunks = MessageFormatter.split_message(final_text, max_len=settings.max_len_slack)
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
            max_chars=settings.max_history_chars,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("conversation persist failed: %s", exc)

    log_event(
        logger,
        "agent.done",
        steps=result.steps,
        tool_calls=result.tool_calls_count,
        tokens_in=result.token_usage.get("input", 0),
        tokens_out=result.token_usage.get("output", 0),
    )


def _parse_request_body(event: dict) -> dict | None:
    """Decode an API Gateway proxy event body into a JSON dict.

    Returns None for non-JSON bodies (e.g. legacy URL-encoded slash
    commands — not used by this bot). Handles base64 transport that API
    Gateway uses for binary content types.
    """
    body = event.get("body") or ""
    if not body:
        return None
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _route_request(event: dict, context) -> dict:  # noqa: ANN001
    """Receiver path entry — identifies the target app and dispatches."""
    parsed = _parse_request_body(event)
    if parsed is None:
        log_event(logger, "request.unparseable_body")
        return {"statusCode": 400, "body": ""}

    # URL verification: Slack pings the endpoint when an operator registers
    # Event Subscriptions. The body has a `challenge` to echo back, and
    # crucially does NOT carry api_app_id — so we have no way to pick the
    # right signing_secret. Echo the challenge directly; it's a setup-time
    # ping with no actionable payload, so skipping signature check here
    # is the cleanest break of the chicken-and-egg.
    if parsed.get("type") == "url_verification":
        return {"statusCode": 200, "body": parsed.get("challenge", "")}

    api_app_id = parsed.get("api_app_id")
    if not api_app_id:
        log_event(logger, "request.no_app_id", body_type=parsed.get("type"))
        return {"statusCode": 200, "body": ""}

    creds = _get_credentials().get(api_app_id)
    if creds is None:
        log_event(
            logger,
            "request.unknown_app",
            api_app_id=api_app_id,
            note=f"missing SSM SecureString at {settings.ssm_params_prefix}/{api_app_id}/{{signing_secret,bot_token}}",
        )
        return {"statusCode": 200, "body": ""}

    bolt_app = _get_bolt_app(api_app_id, creds.signing_secret, creds.bot_token)
    return SlackRequestHandler(bolt_app).handle(event, context)


def lambda_handler(event, context):  # noqa: ANN001
    # Worker path: a Lambda async self-invoke with `_worker=True` skips
    # Slack signature verification entirely. The only way to land here
    # with this flag is via `_enqueue_worker`, which is only callable
    # from inside a successfully verified receiver invocation.
    if isinstance(event, dict) and event.get("_worker"):
        _process_worker(event)
        return {"statusCode": 200, "body": ""}

    # Receiver path: Slack HTTP event via API Gateway.
    # Short-circuit Slack retries without re-dispatching the worker.
    headers = event.get("headers") or {}
    normalized = {k.lower(): v for k, v in headers.items()}
    if normalized.get("x-slack-retry-num"):
        return {"statusCode": 200, "body": ""}
    return _route_request(event, context)
