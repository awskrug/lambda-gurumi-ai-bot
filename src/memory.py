"""Per-user persistent memory.

Single row per user at `mem:{user_id}` in the shared DynamoDB table.
The row carries an `entries` JSON blob: `{key: {value, ts}, ...}`.
Distinct from `ctx:{thread_ts}` (per-thread conversation buffer with
1h TTL) and `dedup:`/`done:` (idempotency).

Why `user_id` only (no `api_app_id` scoping)?
============================================
Operators in this deployment confirmed user_id is unique enough across
the apps they run, and they want a person's saved context (team,
project, preferences) to follow them when the same human switches
between apps. If that assumption ever breaks, switch to
`mem:{api_app_id}:{user_id}` — only the row id changes.

No TTL
======
The row is written WITHOUT `expire_at`, so DynamoDB's TTL never
evicts it (TTL only acts on items that explicitly carry the
attribute). This matches the `app:{app_id}` registry pattern in
the same table.

Caps (defense against runaway accumulation)
===========================================
- per-entry value:    1000 chars
- per-user entries:   50 keys
- serialized blob:    ~30 KB (well under DDB's 400 KB item limit)

When a write would exceed a cap, `put` raises ValueError and the
calling tool surfaces a clear error to the LLM.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# Caps live as module constants so tools and tests share the same source.
MAX_VALUE_CHARS = 1000
MAX_ENTRIES_PER_USER = 50
MAX_BLOB_CHARS = 30_000

# Concurrent writes to the same `mem:{user_id}` row are rare but possible
# (one user mentioning the bot from two threads at once). Each write
# does a read-modify-write of the JSON blob, so we guard with an
# `entries = :prev_blob` ConditionExpression. A few retries reconcile
# routine collisions; `MEMORY_WRITE_MAX_ATTEMPTS` caps the spin so a
# pathological fight surfaces as ValueError instead of looping forever.
MEMORY_WRITE_MAX_ATTEMPTS = 3


class MemoryStore:
    """DynamoDB-backed per-user memory.

    All methods are best-effort: on a DDB outage, reads return an empty
    list and writes log + raise so the caller can surface the failure
    to the LLM (which then tells the user "couldn't save").
    """

    def __init__(self, table_name: str, region: str, table: Any = None) -> None:
        self.table_name = table_name
        self.region = region
        self._table = table

    def _get_table(self) -> Any:
        if self._table is None:
            import boto3

            self._table = boto3.resource("dynamodb", region_name=self.region).Table(self.table_name)
        return self._table

    def get(self, user_id: str) -> list[dict[str, Any]]:
        """Return memory entries for `user_id` as `[{key, value, ts}, ...]`,
        ordered newest-first. Returns [] when no memory exists."""
        if not user_id:
            return []
        entries, _ = self._read_raw(user_id)
        # Stable ordering: newest first by ts. Tie-break on key for
        # determinism so tests don't flake.
        items = [
            {"key": k, "value": v.get("value", ""), "ts": int(v.get("ts", 0))}
            for k, v in entries.items()
            if isinstance(v, dict)
        ]
        items.sort(key=lambda e: (-e["ts"], e["key"]))
        return items

    def put(self, user_id: str, key: str, value: str) -> None:
        """Upsert a single memory entry. Existing key is overwritten.

        Raises ValueError when the write would breach a cap so the
        calling tool can surface the limit to the LLM. Concurrent writes
        from the same user are reconciled via an `entries = :prev_blob`
        condition + a small retry loop; if every attempt collides with a
        racing writer, the final ValueError tells the user to retry.
        """
        if not user_id:
            raise ValueError("user_id is required")
        key = (key or "").strip()
        if not key:
            raise ValueError("key must be a non-empty string")
        if len(key) > 64:
            raise ValueError("key must be 64 chars or fewer")
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        if len(value) > MAX_VALUE_CHARS:
            raise ValueError(f"value exceeds {MAX_VALUE_CHARS} chars")

        for attempt in range(MEMORY_WRITE_MAX_ATTEMPTS):
            existing, prev_blob = self._read_raw(user_id)
            existing[key] = {"value": value, "ts": int(time.time())}
            if len(existing) > MAX_ENTRIES_PER_USER:
                raise ValueError(
                    f"memory full ({MAX_ENTRIES_PER_USER} entries max). "
                    "Remove something with `forget` before adding more."
                )
            blob = json.dumps(existing, ensure_ascii=False)
            if len(blob) > MAX_BLOB_CHARS:
                raise ValueError(
                    f"memory blob would exceed {MAX_BLOB_CHARS} chars. "
                    "Trim long values or call `forget` on stale keys."
                )
            try:
                self._conditional_put(user_id, blob, prev_blob)
                return
            except _MemoryWriteConflict:
                if attempt + 1 >= MEMORY_WRITE_MAX_ATTEMPTS:
                    raise ValueError(
                        "memory write conflict — another save raced this "
                        "one. Please retry."
                    )
                continue
            except ClientError as exc:
                logger.warning("memory put failed for %s: %s", user_id, exc)
                raise

    def delete(self, user_id: str, key: str) -> bool:
        """Remove a single entry. Returns True if the key existed and
        was removed, False if it was already absent.

        When the deletion empties the row, the row itself is removed
        via DeleteItem so we don't accumulate empty `{}` blobs. The
        Lambda IAM policy grants `dynamodb:DeleteItem` for this path —
        removing it would AccessDenied here.

        Concurrent writes are reconciled the same way `put` does, with
        an additional fast-path: if a concurrent writer already removed
        the same key, the conflict is benign and we return True.
        """
        if not user_id or not key:
            return False

        for attempt in range(MEMORY_WRITE_MAX_ATTEMPTS):
            existing, prev_blob = self._read_raw(user_id)
            if key not in existing:
                # Either the key was never there or a concurrent writer
                # already removed it — both observe as "not present" to
                # the caller.
                return attempt > 0
            existing.pop(key, None)
            try:
                if existing:
                    blob = json.dumps(existing, ensure_ascii=False)
                    self._conditional_put(user_id, blob, prev_blob)
                else:
                    self._conditional_delete(user_id, prev_blob)
                return True
            except _MemoryWriteConflict:
                if attempt + 1 >= MEMORY_WRITE_MAX_ATTEMPTS:
                    raise ValueError(
                        "memory delete conflict — another write raced "
                        "this one. Please retry."
                    )
                continue
            except ClientError as exc:
                logger.warning("memory delete failed for %s: %s", user_id, exc)
                raise
        return False  # unreachable, satisfies type checkers

    def _read_raw(
        self, user_id: str
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        """Read the entries dict and the raw blob string.

        The raw blob is what `put`/`delete` use for the `entries =
        :prev_blob` ConditionExpression — it's the only value DDB can
        compare against on a server-side condition. `None` means the
        row doesn't exist yet, which maps to `attribute_not_exists(id)`
        on the conditional write side.

        On a malformed blob we still return the raw value so the
        condition matches — that lets a write recover the row instead
        of being trapped behind unparseable history.
        """
        try:
            res = self._get_table().get_item(Key={"id": _row_id(user_id)})
        except ClientError as exc:
            logger.warning("memory read failed for %s: %s", user_id, exc)
            return {}, None
        item = res.get("Item")
        if item is None:
            return {}, None
        raw = item.get("entries") or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}, raw
        return (data if isinstance(data, dict) else {}), raw

    def _conditional_put(
        self, user_id: str, blob: str, prev_blob: str | None
    ) -> None:
        """Write the blob iff the row is unchanged since `_read_raw`.

        Raises `_MemoryWriteConflict` on contention so the caller can
        retry. Other ClientErrors propagate (transient DDB issue) so
        the warning log surfaces the cause.
        """
        item = {"id": _row_id(user_id), "entries": blob}
        try:
            if prev_blob is None:
                self._get_table().put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(id)",
                )
            else:
                self._get_table().put_item(
                    Item=item,
                    ConditionExpression="entries = :prev",
                    ExpressionAttributeValues={":prev": prev_blob},
                )
        except ClientError as exc:
            if _is_conditional_check_failure(exc):
                raise _MemoryWriteConflict() from exc
            raise

    def _conditional_delete(self, user_id: str, prev_blob: str | None) -> None:
        """Delete the row iff `entries` still equals `prev_blob`."""
        if prev_blob is None:
            # Nothing to delete — treat as benign no-op so retries don't
            # spin forever on a row that vanished beneath us.
            return
        try:
            self._get_table().delete_item(
                Key={"id": _row_id(user_id)},
                ConditionExpression="entries = :prev",
                ExpressionAttributeValues={":prev": prev_blob},
            )
        except ClientError as exc:
            if _is_conditional_check_failure(exc):
                raise _MemoryWriteConflict() from exc
            raise


class _MemoryWriteConflict(Exception):
    """Raised inside `MemoryStore` when a conditional write loses to a
    concurrent writer. Internal — never surfaces past `put`/`delete`."""


def _is_conditional_check_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _row_id(user_id: str) -> str:
    return f"mem:{user_id}"


@dataclass(frozen=True)
class _MemoryEntry:
    """Read-only view of a single memory entry. Used by callers that
    want type-safe access; `MemoryStore.get` returns plain dicts so the
    LLM-facing tool result stays JSON-friendly."""

    key: str
    value: str
    ts: int
