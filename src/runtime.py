"""Process-wide singletons + lazy accessors.

Lambda's warm-container model means module-level state survives across
invocations. Heavy clients (Bedrock, DynamoDB, SSM, Lambda) and the
per-app Bolt App cache live here so every request reuses them. Cold
start pays the construction cost once; warm requests pay nothing.

`auth.test` results are cached the same way (`_bot_user_ids`) — bot
user_id changes only on app reinstall, which always involves a new
container anyway.

All callers should reach this module via `from src import runtime` and
call `runtime.X()` rather than `from src.runtime import X`. The
late-binding pattern is what makes test monkeypatch work — `setattr`
on this module is then visible to every consumer.
"""
from __future__ import annotations

import boto3
from slack_bolt import App
from slack_sdk import WebClient

from src.app_metadata import AppMetadataStore
from src.config import Settings
from src.credentials import CredentialsStore
from src.dedup import ConversationStore, DedupStore
from src.llms import get_llm
from src.logging_utils import get_logger
from src.memory import MemoryStore

settings = Settings.from_env()
# logger name "app" is preserved across the runtime/router/handlers
# split so existing CloudWatch Insights queries (`logger="app"`) keep
# matching every emitted record.
logger = get_logger("app")

_llm = None
_dedup: DedupStore | None = None
_conversations: ConversationStore | None = None
_credentials: CredentialsStore | None = None
_app_metadata: AppMetadataStore | None = None
_memory: MemoryStore | None = None
# api_app_id -> ((signing_secret, bot_token), App). The secret tuple is
# the cache *value* not the *key* so we can detect rotation: when
# CredentialsStore returns a different tuple after its TTL refreshes, we
# rebuild the App so signature verification uses the new secret without
# requiring a container restart.
_bolt_apps: dict[str, tuple[tuple[str, str], App]] = {}
# api_app_id -> bot user_id, populated lazily via auth.test on the worker
# path. Used by the reaction-delete flow to confirm the message being
# reacted to was authored by THIS bot before calling chat.delete (which
# would fail anyway, but the structured log + early-out is cleaner).
_bot_user_ids: dict[str, str] = {}
_lambda_client = None


def _get_llm():
    global _llm
    if _llm is None:
        _llm = get_llm(
            provider=settings.llm_provider,
            model=settings.llm_model,
            image_provider=settings.image_provider,
            image_model=settings.image_model,
            region=settings.aws_region,
            api_keys={"xai": settings.xai_api_key, "upstage": settings.upstage_api_key},
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


def _get_memory() -> MemoryStore:
    global _memory
    if _memory is None:
        _memory = MemoryStore(
            table_name=settings.dynamodb_table_name,
            region=settings.aws_region,
        )
    return _memory


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=settings.aws_region)
    return _lambda_client


def _get_bot_user_id(client: WebClient, api_app_id: str) -> str:
    """Cached `auth.test().user_id` per app_id.

    Used by the reaction handlers to verify the reacted-to message was
    authored by THIS bot. Only successful lookups are cached — a failed
    `auth.test` is NOT poisoned into the cache so a transient outage
    can recover on the next call.
    """
    cached = _bot_user_ids.get(api_app_id)
    if cached:
        return cached
    try:
        resp = client.auth_test()
    except Exception as exc:  # noqa: BLE001
        logger.warning("auth.test failed for %s: %s", api_app_id, exc)
        return ""
    bot_user_id = (resp.get("user_id") if hasattr(resp, "get") else "") or ""
    if bot_user_id:
        _bot_user_ids[api_app_id] = bot_user_id
    return bot_user_id
