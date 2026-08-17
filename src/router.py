"""Receiver / worker routing layer.

The Lambda runs in two modes from the same `lambda_handler` entrypoint:

- **Receiver path** (Slack → API Gateway → Lambda): parse the inbound
  body to identify the target Slack app, resolve its secrets from SSM,
  hand off to a per-app cached Bolt App that verifies the signature and
  routes to the registered event handlers (`_on_mention`,
  `_on_message`, `_on_reaction_added`). Each handler `ack()`s, then
  fires a fire-and-forget self-invoke (`_enqueue_worker`) and returns.

- **Worker path** (Lambda async self-invoke with `_worker=True` in the
  payload): re-fetches the bot token from SSM keyed on the carried
  `api_app_id` and dispatches to the appropriate handler module
  (`handlers.message` or `handlers.reactions`).

Splitting receiver and worker out of the Lambda entrypoint keeps the
HTTP path tight (a few hundred ms to ack Slack) while letting the
worker run with the full Lambda timeout budget.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse

from slack_bolt import App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk import WebClient

from src import runtime
from src.handlers import commands, message, reactions
from src.logging_utils import log_event


# Slash command names handled by `@app.command(...)` registrations below.
# Kept in sync with `src.handlers.commands._COMMAND_TO_IMAGE` (the worker
# checks again, so an entry here without a worker mapping is just a
# no-op).
_SLASH_COMMANDS: tuple[str, ...] = ("/img-gpt", "/img-xai")


def _enqueue_worker(
    event: dict,
    is_dm: bool,
    api_app_id: str,
    client: WebClient | None = None,
) -> None:
    """Fire-and-forget async self-invoke of the worker path.

    `api_app_id` is carried through the payload so the worker can fetch
    its own bot token from SSM. We deliberately do NOT pass tokens
    through the payload — Lambda invoke payloads can be visible in
    CloudTrail and downstream tooling, so secrets stay in SSM.

    `client` is the per-app WebClient Bolt injects into receiver
    handlers. It's used only on the invoke-failure path to post a
    short user-visible notice; the worker path mints its own.
    """
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    inline_payload = {"slack_event": event, "is_dm": is_dm, "api_app_id": api_app_id}
    if not function_name:
        # Local dev / tests: no Lambda runtime, run inline. Receivers in
        # this mode are unit tests with no API Gateway timeout to worry
        # about.
        _process_worker(inline_payload)
        return
    payload = json.dumps(
        {"_worker": True, **inline_payload}, ensure_ascii=False
    ).encode("utf-8")
    try:
        runtime._get_lambda_client().invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=payload,
        )
    except Exception:
        # Async invoke failed (IAM, throttling, network). DO NOT run
        # the worker inline — receivers have a ~30s API Gateway window
        # and Slack expects ack within 3s, so an inline agent run would
        # blow both budgets and trigger a retry storm. Post a brief
        # user-visible notice (best-effort) and drop the request; the
        # user can resend.
        runtime.logger.exception("async worker invoke failed; dropping after notice")
        _notify_invoke_failure(client, event)


def _notify_invoke_failure(client: WebClient | None, event: dict) -> None:
    """Best-effort 'try again shortly' notice on async-invoke failure.

    Silent if no client (e.g. reaction events have no natural reply
    surface) or if posting itself fails. We do not raise — the receiver
    must still return cleanly so API Gateway gets its 200.
    """
    if client is None:
        return
    channel = event.get("channel")
    if not channel:
        return
    thread_ts = event.get("thread_ts") or event.get("ts")
    try:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="일시 오류로 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        )
    except Exception:
        runtime.logger.warning("invoke-failure notification post failed", exc_info=True)


def _enqueue_command_worker(
    body: dict,
    api_app_id: str,
) -> None:
    """Fire-and-forget async self-invoke for a slash command.

    Mirrors `_enqueue_worker` for events, but the worker-side dispatch
    branches on `kind=command` instead of an `event["type"]`. The Bolt
    `body` dict for a slash command carries Slack's full form payload —
    we extract only the fields the worker actually needs so the async
    invoke payload stays small and ships zero secrets.
    """
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    command_payload = {
        "command": body.get("command", ""),
        "text": body.get("text", ""),
        "channel_id": body.get("channel_id", ""),
        "user_id": body.get("user_id", ""),
        "trigger_id": body.get("trigger_id", ""),
        "response_url": body.get("response_url", ""),
        "team_id": body.get("team_id", ""),
    }
    inline_payload = {
        "kind": "command",
        "command_payload": command_payload,
        "api_app_id": api_app_id,
    }
    if not function_name:
        _process_worker(inline_payload)
        return
    payload = json.dumps(
        {"_worker": True, **inline_payload}, ensure_ascii=False
    ).encode("utf-8")
    try:
        runtime._get_lambda_client().invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=payload,
        )
    except Exception:
        # Same trade-off as `_enqueue_worker`: do NOT run inline — the
        # receiver budget cannot absorb a 60–180s image generation.
        # Push an ephemeral notice through `response_url` instead so the
        # user sees a clear failure (slash commands have no thread to
        # post into without an explicit channel reply).
        runtime.logger.exception("async command invoke failed; dropping after notice")
        _notify_command_invoke_failure(command_payload.get("response_url") or "")


def _notify_command_invoke_failure(response_url: str) -> None:
    """Best-effort 'try again shortly' notice for slash-command invoke failure.

    We avoid posting to the channel here — a public bot message that
    says "I failed" is noisier than an ephemeral reply only the invoking
    user sees. If `response_url` is unavailable, drop silently.
    """
    if not response_url:
        return
    body = json.dumps(
        {
            "response_type": "ephemeral",
            "text": "일시 오류로 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        }
    ).encode("utf-8")
    import urllib.request  # noqa: PLC0415  (local import — used only on the failure path)

    req = urllib.request.Request(
        response_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (Slack-issued URL)
            resp.read()
    except Exception:
        runtime.logger.warning("command invoke-failure notification failed", exc_info=True)


def _process_worker(payload: dict) -> None:
    """Worker path: full agent run.

    Re-fetches the bot token from SSM keyed on `api_app_id` carried in
    the payload. Bolt's injected WebClient is gone by this point — it
    lived in the receiver process — so we mint a fresh one.
    """
    kind = payload.get("kind")
    api_app_id = payload.get("api_app_id") or ""
    slack_event = payload.get("slack_event") or {}
    is_dm = bool(payload.get("is_dm"))
    # reaction_added carries the channel inside `item`, not at the top
    # level the way message/app_mention events do.
    channel = slack_event.get("channel") or (slack_event.get("item") or {}).get("channel")

    if not api_app_id:
        log_event(runtime.logger, "worker.no_app_id", channel=channel, kind=kind)
        return
    creds = runtime._get_credentials().get(api_app_id)
    if creds is None:
        log_event(
            runtime.logger,
            "worker.unknown_app",
            api_app_id=api_app_id,
            channel=channel,
            kind=kind,
            note=f"missing SSM SecureString at {runtime.settings.ssm_params_prefix}/{api_app_id}/{{signing_secret,bot_token}}",
        )
        return

    client = WebClient(token=creds.bot_token)

    if kind == "command":
        commands._process_command(
            payload.get("command_payload") or {},
            client,
            api_app_id=api_app_id,
        )
        return

    if slack_event.get("type") == "reaction_added":
        reactions._process_reaction(slack_event, client, api_app_id=api_app_id)
        return

    def _say(text: str, thread_ts: str | None = None) -> None:
        kwargs: dict = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        client.chat_postMessage(**kwargs)

    message._process(slack_event, client, _say, is_dm=is_dm, api_app_id=api_app_id)


def _get_bolt_app(api_app_id: str, signing_secret: str, bot_token: str) -> App:
    """Per-app Bolt App, cached on warm containers.

    Cache key is `api_app_id`; the (signing_secret, bot_token) tuple is
    stored alongside so we can detect rotation: when CredentialsStore
    returns a different tuple after its TTL refreshes, we rebuild the App
    so signature verification uses the new secret.
    """
    cached = runtime._bolt_apps.get(api_app_id)
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
    def _on_mention(event, body, ack, client):  # noqa: ANN001
        ack()
        _enqueue_worker(
            event,
            is_dm=False,
            api_app_id=(body or {}).get("api_app_id", ""),
            client=client,
        )

    @app.event("message")
    def _on_message(event, body, ack, client):  # noqa: ANN001
        ack()
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        _enqueue_worker(
            event,
            is_dm=True,
            api_app_id=(body or {}).get("api_app_id", ""),
            client=client,
        )

    @app.event("reaction_added")
    def _on_reaction_added(event, body, ack):  # noqa: ANN001
        ack()
        # Pre-filter at the receiver so unrelated reactions don't cost a
        # Lambda async self-invoke. Reads the same REACTION_HANDLERS dict
        # the worker dispatches on — adding a new reaction in one place
        # automatically opens the receiver filter for it. The worker
        # re-checks on its side (defense-in-depth).
        if event.get("reaction") not in reactions.REACTION_HANDLERS:
            return
        if (event.get("item") or {}).get("type") != "message":
            return
        # No `client=...` here: a reaction event has no natural reply
        # surface to post a "try again" notice into. Drop on invoke
        # failure (logged) and let the user re-react.
        _enqueue_worker(event, is_dm=False, api_app_id=(body or {}).get("api_app_id", ""))

    # Slash command handlers — one decorator per command name. Bolt
    # dispatches based on the `command` form field; `body` is the full
    # parsed form payload (already signature-verified by Bolt).
    def _make_command_handler(_name: str):
        def _on_command(ack, body):  # noqa: ANN001
            ack()
            _enqueue_command_worker(body or {}, api_app_id=(body or {}).get("api_app_id", ""))
        return _on_command

    for _cmd in _SLASH_COMMANDS:
        app.command(_cmd)(_make_command_handler(_cmd))

    runtime._bolt_apps[api_app_id] = ((signing_secret, bot_token), app)
    return app


def _parse_request_body(event: dict) -> dict | None:
    """Decode an API Gateway proxy event body into a JSON dict.

    Returns None for non-JSON bodies. Slash commands arrive as
    application/x-www-form-urlencoded and are routed via
    `_route_command` (path-based dispatch in `_route_request`). Handles
    base64 transport that API Gateway uses for binary content types.
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


def _parse_command_app_id(event: dict) -> str | None:
    """Extract `api_app_id` from a slash-command form body.

    We do NOT mutate `event["body"]` — Bolt still needs the raw bytes
    for signature verification downstream. Only `api_app_id` is pulled
    out so the receiver can pick the right SSM credentials.
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
        parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
    except ValueError:
        return None
    vals = parsed.get("api_app_id") or []
    return vals[0] if vals else None


def _request_path(event: dict) -> str:
    """Best-effort path string for routing the request.

    API Gateway proxy events carry both `path` (the actual request
    path) and `resource` (the route template). Either is enough to tell
    `/slack/events` from `/slack/command`.
    """
    path = event.get("path") or event.get("resource") or ""
    return path if isinstance(path, str) else ""


def _route_request(event: dict, context) -> dict:  # noqa: ANN001
    """Receiver path entry — identifies the target app and dispatches."""
    if _request_path(event).endswith("/slack/command"):
        return _route_command(event, context)

    parsed = _parse_request_body(event)
    if parsed is None:
        log_event(runtime.logger, "request.unparseable_body")
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
        log_event(runtime.logger, "request.no_app_id", body_type=parsed.get("type"))
        return {"statusCode": 200, "body": ""}

    creds = runtime._get_credentials().get(api_app_id)
    if creds is None:
        log_event(
            runtime.logger,
            "request.unknown_app",
            api_app_id=api_app_id,
            note=f"missing SSM SecureString at {runtime.settings.ssm_params_prefix}/{api_app_id}/{{signing_secret,bot_token}}",
        )
        return {"statusCode": 200, "body": ""}

    bolt_app = _get_bolt_app(api_app_id, creds.signing_secret, creds.bot_token)
    return SlackRequestHandler(bolt_app).handle(event, context)


def _route_command(event: dict, context) -> dict:  # noqa: ANN001
    """Receiver path for `/slack/command`.

    Slash commands arrive as `application/x-www-form-urlencoded`. We
    parse just enough of the body to recover `api_app_id`, then hand the
    raw event to the per-app Bolt App, which verifies the signature with
    that app's `signing_secret` and dispatches to the registered
    `@app.command(...)` handler.
    """
    api_app_id = _parse_command_app_id(event)
    if not api_app_id:
        log_event(runtime.logger, "command.no_app_id")
        return {"statusCode": 200, "body": ""}

    creds = runtime._get_credentials().get(api_app_id)
    if creds is None:
        log_event(
            runtime.logger,
            "command.unknown_app",
            api_app_id=api_app_id,
            note=f"missing SSM SecureString at {runtime.settings.ssm_params_prefix}/{api_app_id}/{{signing_secret,bot_token}}",
        )
        return {"statusCode": 200, "body": ""}

    bolt_app = _get_bolt_app(api_app_id, creds.signing_secret, creds.bot_token)
    return SlackRequestHandler(bolt_app).handle(event, context)
