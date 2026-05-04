"""SSM Parameter Store credential loader for multi-tenant Slack apps.

Per-app secrets live as SecureString parameters at:

    {prefix}/{app_id}/signing_secret
    {prefix}/{app_id}/bot_token

Both must be present for the app to be considered configured. A missing
or partially configured app returns None — callers translate that into a
structured log + HTTP 200 (so Slack doesn't retry a fundamentally
unrecoverable misconfiguration).

A short in-process TTL cache (default 5 min) absorbs the per-request SSM
cost on warm Lambda containers. Negative results (missing app) are
cached for the same TTL so a misconfigured app's burst can't storm SSM.
Secret rotation is reflected within one TTL window without a container
restart.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlackAppCredentials:
    signing_secret: str
    bot_token: str


class CredentialsStore:
    def __init__(
        self,
        region: str,
        prefix: str = "/gurumi-bot/apps",
        ttl_seconds: int = 300,
        client: Any = None,
    ) -> None:
        self.region = region
        # Strip trailing slash so we always assemble {prefix}/{app_id}/...
        self.prefix = prefix.rstrip("/")
        self.ttl_seconds = ttl_seconds
        self._client = client
        # app_id -> (expires_at_epoch, SlackAppCredentials | None)
        self._cache: dict[str, tuple[float, SlackAppCredentials | None]] = {}

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("ssm", region_name=self.region)
        return self._client

    def get(self, app_id: str) -> SlackAppCredentials | None:
        if not app_id:
            return None
        now = time.time()
        cached = self._cache.get(app_id)
        if cached and cached[0] > now:
            return cached[1]

        signing_name = f"{self.prefix}/{app_id}/signing_secret"
        token_name = f"{self.prefix}/{app_id}/bot_token"
        try:
            res = self._get_client().get_parameters(
                Names=[signing_name, token_name],
                WithDecryption=True,
            )
        except ClientError as exc:
            # Don't cache transient errors — a fresh request should retry.
            logger.warning("ssm get_parameters failed for app_id=%s: %s", app_id, exc)
            return None

        params = {p["Name"]: p["Value"] for p in res.get("Parameters", [])}
        signing_secret = params.get(signing_name)
        bot_token = params.get(token_name)
        if not signing_secret or not bot_token:
            self._cache[app_id] = (now + self.ttl_seconds, None)
            return None

        creds = SlackAppCredentials(signing_secret=signing_secret, bot_token=bot_token)
        self._cache[app_id] = (now + self.ttl_seconds, creds)
        return creds

    def invalidate(self, app_id: str) -> None:
        self._cache.pop(app_id, None)
