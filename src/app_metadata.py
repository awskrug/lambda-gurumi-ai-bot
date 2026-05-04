"""DynamoDB store for Slack app metadata.

Each known Slack `api_app_id` gets a row at `app:{app_id}` with first-seen
and last-seen timestamps and the team_id we observed it from. Items have
NO `expire_at` attribute, so the table's TTL never deletes them — DynamoDB
TTL only acts on items that explicitly carry the configured attribute, so
permanent rows coexist with TTL'd `dedup:` and `ctx:` rows in the same
table.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class AppMetadataStore:
    def __init__(self, table_name: str, region: str, table: Any = None) -> None:
        self.table_name = table_name
        self.region = region
        self._table = table

    def _get_table(self) -> Any:
        if self._table is None:
            self._table = boto3.resource("dynamodb", region_name=self.region).Table(self.table_name)
        return self._table

    def record(self, app_id: str, team_id: str | None = None) -> None:
        """Upsert metadata for `app_id`.

        first_seen_at is preserved across calls via if_not_exists. last_seen_at
        and team_id are overwritten on every call so the row reflects the
        most recently observed workspace — useful when an app is reinstalled
        elsewhere.
        """
        if not app_id:
            return
        now = int(time.time())
        update_expr = "SET first_seen_at = if_not_exists(first_seen_at, :now), last_seen_at = :now"
        attr_values: dict[str, Any] = {":now": now}
        if team_id:
            update_expr += ", team_id = :team_id"
            attr_values[":team_id"] = team_id
        try:
            self._get_table().update_item(
                Key={"id": f"app:{app_id}"},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=attr_values,
            )
        except ClientError as exc:
            logger.warning("app metadata record failed for %s: %s", app_id, exc)

    def get(self, app_id: str) -> dict[str, Any] | None:
        """Read metadata row. Returns None if not found."""
        if not app_id:
            return None
        try:
            res = self._get_table().get_item(Key={"id": f"app:{app_id}"})
        except ClientError as exc:
            logger.warning("app metadata get failed for %s: %s", app_id, exc)
            return None
        return res.get("Item")
