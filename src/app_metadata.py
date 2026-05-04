"""DynamoDB store for Slack app metadata + per-app ACL overrides.

Each known Slack `api_app_id` gets a row at `app:{app_id}` with first-seen
and last-seen timestamps, the team_id we observed it from, and OPTIONAL
per-app overrides for the channel / user allowlists. Items have NO
`expire_at` attribute, so the table's TTL never deletes them — DynamoDB
TTL only acts on items that explicitly carry the configured attribute,
so permanent rows coexist with TTL'd `dedup:` and `ctx:` rows in the
same table.

Per-app ACL semantics
=====================
Two optional list attributes on the row, written by `set_allowlist`:

  - `allowed_channel_ids`: per-app override for `ALLOWED_CHANNEL_IDS` env var
  - `allowed_user_ids`:    per-app override for `ALLOWED_USER_IDS` env var

Resolution rule (applied in `app._process`):

  - attribute ABSENT      → use the global env var
  - attribute PRESENT     → use this value, IGNORE the global
  - attribute is `[]`     → "this app explicitly allows all" — overrides
                            even a non-empty global list

This three-state design lets operators (a) leave per-app config untouched
and inherit the deployment-wide default, (b) lock down a specific app
independently, or (c) explicitly carve out an unrestricted app even when
the global is restrictive.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# Attribute names live as constants so the CLI, tests, and runtime never
# disagree on spelling — a typo in any single string would silently revert
# affected apps to the global env var.
ALLOWED_CHANNEL_IDS_ATTR = "allowed_channel_ids"
ALLOWED_USER_IDS_ATTR = "allowed_user_ids"
_ACL_ATTRS = (ALLOWED_CHANNEL_IDS_ATTR, ALLOWED_USER_IDS_ATTR)


class AppMetadataStore:
    def __init__(self, table_name: str, region: str, table: Any = None) -> None:
        self.table_name = table_name
        self.region = region
        self._table = table

    def _get_table(self) -> Any:
        if self._table is None:
            self._table = boto3.resource("dynamodb", region_name=self.region).Table(self.table_name)
        return self._table

    def record(self, app_id: str, team_id: str | None = None) -> dict[str, Any] | None:
        """Upsert metadata for `app_id` and return the resulting row.

        first_seen_at is preserved across calls via if_not_exists. last_seen_at
        and team_id are overwritten on every call so the row reflects the
        most recently observed workspace — useful when an app is reinstalled
        elsewhere.

        Uses `ReturnValues=ALL_NEW` so the caller gets the full row back —
        including any per-app ACL attributes operator set via `set_allowlist`
        — without a separate GetItem. This is what lets `app._process` apply
        per-app ACL on the same DynamoDB roundtrip it already needed for the
        metadata write.

        Returns None on DynamoDB error so the caller can fall back to global
        env-var ACL behavior; the bot stays operational through transient
        DynamoDB issues.
        """
        if not app_id:
            return None
        now = int(time.time())
        update_expr = "SET first_seen_at = if_not_exists(first_seen_at, :now), last_seen_at = :now"
        attr_values: dict[str, Any] = {":now": now}
        if team_id:
            update_expr += ", team_id = :team_id"
            attr_values[":team_id"] = team_id
        try:
            res = self._get_table().update_item(
                Key={"id": f"app:{app_id}"},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=attr_values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            logger.warning("app metadata record failed for %s: %s", app_id, exc)
            return None
        return res.get("Attributes") or {}

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

    def set_allowlist(self, app_id: str, attr: str, values: list[str]) -> None:
        """Write a per-app allowlist override.

        `values=[]` is a meaningful state (explicit "allow all per-app",
        overrides any non-empty global env var) and is preserved as an
        empty list in DynamoDB — distinct from the attribute being absent.
        Use `unset_allowlist` to clear the override and revert to global.
        """
        if attr not in _ACL_ATTRS:
            raise ValueError(
                f"unknown ACL attribute: {attr!r} (expected one of {_ACL_ATTRS})"
            )
        if not app_id:
            raise ValueError("app_id is required")
        self._get_table().update_item(
            Key={"id": f"app:{app_id}"},
            UpdateExpression="SET #attr = :v",
            ExpressionAttributeNames={"#attr": attr},
            ExpressionAttributeValues={":v": list(values)},
        )

    def unset_allowlist(self, app_id: str, attr: str) -> None:
        """Remove a per-app allowlist override; behavior reverts to the
        global env var. Removing a non-existent attribute is a silent no-op
        (DynamoDB REMOVE semantics)."""
        if attr not in _ACL_ATTRS:
            raise ValueError(
                f"unknown ACL attribute: {attr!r} (expected one of {_ACL_ATTRS})"
            )
        if not app_id:
            raise ValueError("app_id is required")
        try:
            self._get_table().update_item(
                Key={"id": f"app:{app_id}"},
                UpdateExpression="REMOVE #attr",
                ExpressionAttributeNames={"#attr": attr},
            )
        except ClientError as exc:
            logger.warning(
                "app metadata unset_allowlist failed for %s/%s: %s", app_id, attr, exc
            )
