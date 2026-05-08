"""DynamoDB-backed idempotency and thread conversation memory.

Single table, partition key `id`. Three key prefixes share the table:
- `dedup:{client_msg_id}` — short-TTL in-flight reservation. Released
  via natural TTL expiry so a worker that crashed/timed-out *can* be
  retried by Lambda's async retry policy.
- `done:{client_msg_id}` — long-TTL completion marker written after a
  successful agent run. Subsequent retries see this and skip.
- `ctx:{thread_ts}` — conversation history for thread memory.

The two-stage dedup (reserve + mark_done) is what protects against the
silent-failure path where a worker dies and Lambda's automatic retry
would otherwise be blocked by a long-lived `dedup:` row.

A GSI on `user` + `expire_at` enables per-user active-request counting
for throttling.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


@dataclass
class _BaseStore:
    table_name: str
    region: str
    _table: Any = None

    def _get_table(self) -> Any:
        if self._table is None:
            self._table = boto3.resource("dynamodb", region_name=self.region).Table(self.table_name)
        return self._table


class DedupStore(_BaseStore):
    """Atomic reservation for Slack retry deduplication + user throttle count."""

    GSI_NAME = "user-index"

    # In-flight TTL is intentionally short. It only needs to absorb
    # Slack's receiver-side retry burst (≤30s in practice) and prevent
    # two Lambda async invocations from running the same agent in
    # parallel. Once a worker fails and the row TTL expires, Lambda's
    # built-in async retry can re-enter without being silently blocked.
    # The long-term idempotency guarantee comes from `mark_done` below.
    DEFAULT_RESERVE_TTL = 90
    DEFAULT_DONE_TTL = 3600

    def reserve(
        self,
        event_key: str,
        user: str = "system",
        ttl_seconds: int = DEFAULT_RESERVE_TTL,
    ) -> bool:
        """Return True if reservation succeeds, False if already reserved.

        Uses ConditionExpression=attribute_not_exists(id) for atomicity — no
        get-then-put race window.
        """
        table = self._get_table()
        expire_at = int(time.time()) + ttl_seconds
        try:
            table.put_item(
                Item={"id": f"dedup:{event_key}", "user": user, "expire_at": expire_at},
                ConditionExpression="attribute_not_exists(id)",
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            logger.warning("dedup reserve failed: %s", exc)
            raise

    def is_done(self, event_key: str) -> bool:
        """Return True if a successful run was already recorded for this key.

        Read-only — does NOT reserve anything. Callers check this first to
        short-circuit retries that survived past the in-flight TTL window.
        """
        table = self._get_table()
        try:
            res = table.get_item(Key={"id": f"done:{event_key}"})
        except ClientError as exc:
            # Treat DDB outage as "not done" — falling through to reserve()
            # will surface its own error path. Better to maybe-double-process
            # than to silently drop on a transient read failure.
            logger.warning("dedup is_done failed: %s", exc)
            return False
        return res.get("Item") is not None

    def mark_done(
        self,
        event_key: str,
        user: str = "system",
        ttl_seconds: int = DEFAULT_DONE_TTL,
    ) -> None:
        """Write the long-lived completion marker.

        Called after a successful agent run. The short-lived `dedup:` row
        is left to expire on its own — only the `done:` marker survives,
        and that's what subsequent `is_done` checks observe.
        """
        table = self._get_table()
        expire_at = int(time.time()) + ttl_seconds
        try:
            table.put_item(
                Item={"id": f"done:{event_key}", "user": user, "expire_at": expire_at},
            )
        except ClientError as exc:
            # Failing to mark done is non-fatal — at worst a future retry
            # re-runs the agent. We log so operators can spot a chronic
            # outage; we do not raise into the response path.
            logger.warning("dedup mark_done failed: %s", exc)

    def count_user_active(self, user: str) -> int:
        """Number of non-expired reservations for a user (throttle check)."""
        if not user:
            return 0
        table = self._get_table()
        now = int(time.time())
        try:
            res = table.query(
                IndexName=self.GSI_NAME,
                KeyConditionExpression="#u = :u AND expire_at > :now",
                ExpressionAttributeNames={"#u": "user"},
                ExpressionAttributeValues={":u": user, ":now": now},
                Select="COUNT",
            )
            return int(res.get("Count", 0))
        except ClientError as exc:
            logger.warning("count_user_active failed: %s", exc)
            return 0


class ConversationStore(_BaseStore):
    """Thread conversation history with TTL."""

    def get(self, thread_ts: str) -> list[dict[str, Any]]:
        if not thread_ts:
            return []
        table = self._get_table()
        try:
            res = table.get_item(Key={"id": f"ctx:{thread_ts}"})
        except ClientError as exc:
            logger.warning("conversation get failed: %s", exc)
            return []
        item = res.get("Item")
        if not item:
            return []
        raw = item.get("conversation") or "[]"
        try:
            messages = json.loads(raw)
            return messages if isinstance(messages, list) else []
        except json.JSONDecodeError:
            logger.warning("malformed conversation blob for %s", thread_ts)
            return []

    def put(
        self,
        thread_ts: str,
        user: str,
        messages: list[dict[str, Any]],
        ttl_seconds: int = 3600,
        max_chars: int = 4000,
    ) -> None:
        if not thread_ts:
            return
        trimmed = self.truncate_to_chars(messages, max_chars)
        table = self._get_table()
        try:
            table.put_item(
                Item={
                    "id": f"ctx:{thread_ts}",
                    "user": user or "unknown",
                    "expire_at": int(time.time()) + ttl_seconds,
                    "conversation": json.dumps(trimmed, ensure_ascii=False),
                }
            )
        except ClientError as exc:
            logger.warning("conversation put failed: %s", exc)

    # json.dumps with default separators renders a list as `[item, item, ...]`
    # so exact serialized size is:  2 (brackets) + sum(sizes) + 2 * (n - 1) (", ")
    _JSON_ARRAY_BRACKETS = 2
    _JSON_ITEM_SEPARATOR = 2

    @staticmethod
    def truncate_to_chars(messages: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
        """Drop oldest messages until total serialized size <= max_chars.

        Previous implementation was O(n²) — it re-serialized the full kept
        list on every pop. This walks the list once, serializes each message
        individually, then accumulates from the newest end backwards until
        adding the next (older) message would exceed the budget. Matches the
        exact byte count of `json.dumps(kept, ensure_ascii=False)` using the
        default item separator `", "`.
        """
        if not messages:
            return []
        sizes = [len(json.dumps(m, ensure_ascii=False)) for m in messages]
        total = ConversationStore._JSON_ARRAY_BRACKETS
        start = len(messages)  # exclusive; empty kept set serializes to "[]"
        for i in range(len(messages) - 1, -1, -1):
            add_cost = sizes[i] + (ConversationStore._JSON_ITEM_SEPARATOR if start < len(messages) else 0)
            if total + add_cost > max_chars:
                break
            total += add_cost
            start = i
        return messages[start:]
