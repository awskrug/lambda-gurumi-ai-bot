"""DynamoDB store for Slack app metadata + per-app ACL overrides.

Each known Slack `api_app_id` gets a row at `app:{app_id}` with first-seen
and last-seen timestamps, the team_id we observed it from, and OPTIONAL
per-app overrides for the channel / user allowlists. Items have NO
`expire_at` attribute, so the table's TTL never deletes them — DynamoDB
TTL only acts on items that explicitly carry the configured attribute,
so permanent rows coexist with TTL'd `dedup:` and `ctx:` rows in the
same table.

App identification fields
=========================
The CLI populates these to make `apps list` readable at a glance — none
of them affect runtime behavior:

  - `team_name`      — Slack workspace name (from `auth.test`)
  - `bot_user_name`  — bot's user handle in that workspace (from `auth.test`)
  - `team_domain`    — workspace subdomain, e.g. "acme" (from `auth.test`)
  - `display_name`   — operator-set human label, takes precedence over
                       the auto-populated team_name/bot_user_name

`auth.test` runs from the operator's CLI (`apps refresh` or, by default,
on every `apps set`), never from the Lambda runtime — the worker stays
focused on processing events.

Per-app overrides
=================
Three optional attributes on the row override the matching deployment-wide
env var for that one app:

  - `allowed_channel_ids` (list)  — overrides `ALLOWED_CHANNEL_IDS`
  - `allowed_user_ids`    (list)  — overrides `ALLOWED_USER_IDS`
  - `persona_message`     (str)   — overrides `PERSONA_MESSAGE`

Resolution rule (applied in `app._process`, same shape for all three):

  - attribute ABSENT  → use the global env var
  - attribute PRESENT → use this value, IGNORE the global

For the list attributes, the empty list `[]` is preserved as a meaningful
PRESENT value — it means "this app explicitly allows all", overriding even
a non-empty global. For the string attribute, the empty string `""` is
preserved the same way — it means "this app has no persona", overriding
even a non-empty global persona.

This design lets operators (a) leave per-app config untouched and inherit
the deployment-wide default, (b) lock a specific app down or open it up
independently, or (c) carve out a single app's behavior even when the
global is restrictive.

`SYSTEM_MESSAGE` (operator policy that gets appended to the base task
rules) intentionally has NO per-app override — it's a security/policy
field that should stay consistent across the deployment.
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
PERSONA_MESSAGE_ATTR = "persona_message"
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

    def set_persona(self, app_id: str, value: str) -> None:
        """Write the per-app persona override.

        `value=""` is a meaningful state — it means "this app has no
        persona" and overrides any non-empty global `PERSONA_MESSAGE`.
        Use `unset_persona` to remove the attribute and revert to global.
        """
        if not app_id:
            raise ValueError("app_id is required")
        if not isinstance(value, str):
            raise TypeError(f"persona value must be str, got {type(value).__name__}")
        self._get_table().update_item(
            Key={"id": f"app:{app_id}"},
            UpdateExpression="SET #attr = :v",
            ExpressionAttributeNames={"#attr": PERSONA_MESSAGE_ATTR},
            ExpressionAttributeValues={":v": value},
        )

    def unset_persona(self, app_id: str) -> None:
        """Remove the per-app persona override; behavior reverts to the
        global `PERSONA_MESSAGE` env var. No-op when the attribute is
        already absent."""
        if not app_id:
            raise ValueError("app_id is required")
        try:
            self._get_table().update_item(
                Key={"id": f"app:{app_id}"},
                UpdateExpression="REMOVE #attr",
                ExpressionAttributeNames={"#attr": PERSONA_MESSAGE_ATTR},
            )
        except ClientError as exc:
            logger.warning("app metadata unset_persona failed for %s: %s", app_id, exc)

    def update_app_info(
        self,
        app_id: str,
        *,
        team_name: str | None = None,
        bot_user_name: str | None = None,
        team_domain: str | None = None,
    ) -> None:
        """Write Slack app identification fields (typically from `auth.test`).

        Each kwarg is optional — pass only what you have. Values overwrite
        any existing value (workspaces and bot user names can change, so we
        don't use `if_not_exists`).
        """
        if not app_id:
            raise ValueError("app_id is required")
        set_clauses: list[str] = []
        attr_names: dict[str, str] = {}
        attr_values: dict[str, Any] = {}
        if team_name is not None:
            set_clauses.append("#tn = :tn")
            attr_names["#tn"] = "team_name"
            attr_values[":tn"] = team_name
        if bot_user_name is not None:
            set_clauses.append("#bu = :bu")
            attr_names["#bu"] = "bot_user_name"
            attr_values[":bu"] = bot_user_name
        if team_domain is not None:
            set_clauses.append("#td = :td")
            attr_names["#td"] = "team_domain"
            attr_values[":td"] = team_domain
        if not set_clauses:
            return
        self._get_table().update_item(
            Key={"id": f"app:{app_id}"},
            UpdateExpression=f"SET {', '.join(set_clauses)}",
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
        )

    def set_display_name(self, app_id: str, name: str) -> None:
        """Operator-supplied human label for the app, takes precedence over
        the auto-populated team_name/bot_user_name in `apps list` output."""
        if not app_id:
            raise ValueError("app_id is required")
        if not isinstance(name, str):
            raise TypeError(f"display name must be str, got {type(name).__name__}")
        self._get_table().update_item(
            Key={"id": f"app:{app_id}"},
            UpdateExpression="SET display_name = :v",
            ExpressionAttributeValues={":v": name},
        )

    def unset_display_name(self, app_id: str) -> None:
        """Remove the operator-set display name; auto-populated fields fill in."""
        if not app_id:
            raise ValueError("app_id is required")
        try:
            self._get_table().update_item(
                Key={"id": f"app:{app_id}"},
                UpdateExpression="REMOVE display_name",
            )
        except ClientError as exc:
            logger.warning("app metadata unset_display_name failed for %s: %s", app_id, exc)
