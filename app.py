"""AWS Lambda entrypoint for the Slack mention bot.

This module exists only to satisfy `serverless.yml`'s
`handler: app.lambda_handler` contract. All routing, worker
processing, and event handling lives in `src.router` and the
`src.handlers` package; this file dispatches to them.

Architecture
============

This Lambda serves multiple distinct Slack apps. Each app's secrets
(`signing_secret` + `bot_token`) live in SSM Parameter Store under
`{SSM_PARAMS_PREFIX}/{api_app_id}/...` and are looked up per request via
`CredentialsStore`. DynamoDB row `app:{api_app_id}` records when each
app was first seen and last seen.

Receiver path (Slack → API Gateway → Lambda):
  1. lambda_handler short-circuits Slack retries (X-Slack-Retry-Num).
  2. router._route_request parses the body to extract `api_app_id`.
     URL-verification handshakes (no api_app_id) echo the challenge
     directly without signature verification — the body has no
     actionable payload.
  3. Looks up the app's secrets in SSM. Missing → log + HTTP 200
     (so Slack doesn't retry an unrecoverable misconfig).
  4. Dispatches to a per-app cached Bolt App that verifies the
     signature with that app's signing_secret and routes to the event
     handlers (`_on_mention`, `_on_message`, `_on_reaction_added`).
  5. Each handler ack()s, then fires a fire-and-forget self-invoke
     with `_worker=True` plus `api_app_id` so the worker can fetch
     its own bot token from SSM (we don't ship secrets through the
     async invoke payload — Lambda invoke payloads can show up in
     CloudTrail).

Worker path (Lambda async self-invoke):
  1. `_worker=True` skips Bolt; router._process_worker re-fetches the
     bot_token from SSM keyed on the carried `api_app_id`.
  2. Branches on event type: `reaction_added` →
     `handlers.reactions._process_reaction`; otherwise →
     `handlers.message._process`.
  3. Same dedup row absorbs Slack's retry burst on the receiver side
     AND Lambda async's built-in 2x retry on worker failure — all
     paths converge on the same `dedup:{client_msg_id}` key (or
     `dedup:reaction:{event_ts}:{reactor}` for reactions).
"""
from __future__ import annotations

from src import router


def lambda_handler(event, context):  # noqa: ANN001
    """Lambda entrypoint (`serverless.yml: handler: app.lambda_handler`).

    The function name and module location are part of the deployment
    contract — do not rename, do not move out of `app.py`. The actual
    routing logic lives in `src.router`; this wrapper just dispatches.
    """
    # Worker path: a Lambda async self-invoke with `_worker=True` skips
    # Slack signature verification entirely. The only way to land here
    # with this flag is via `router._enqueue_worker`, which is only
    # callable from inside a successfully verified receiver invocation.
    if isinstance(event, dict) and event.get("_worker"):
        router._process_worker(event)
        return {"statusCode": 200, "body": ""}

    # Receiver path: Slack HTTP event via API Gateway.
    # Short-circuit Slack retries without re-dispatching the worker.
    headers = event.get("headers") or {}
    normalized = {k.lower(): v for k, v in headers.items()}
    if normalized.get("x-slack-retry-num"):
        return {"statusCode": 200, "body": ""}
    return router._route_request(event, context)
