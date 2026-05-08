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
        try:
            res = self._get_table().get_item(Key={"id": _row_id(user_id)})
        except ClientError as exc:
            logger.warning("memory get failed for %s: %s", user_id, exc)
            return []
        item = res.get("Item")
        if not item:
            return []
        raw = item.get("entries") or "{}"
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("malformed memory blob for %s", user_id)
            return []
        if not isinstance(entries, dict):
            return []
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
        calling tool can surface the limit to the LLM. The DDB write
        itself is best-effort; on outage we log + raise so the user
        gets a clear "couldn't save" reply rather than a silent miss.
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

        # Read-modify-write — race risk is acceptable here (a person
        # rarely edits memory from two threads simultaneously). If two
        # writes do collide, last-writer-wins, same as ConversationStore.
        existing = self._read_raw(user_id)
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
            self._get_table().put_item(Item={"id": _row_id(user_id), "entries": blob})
        except ClientError as exc:
            logger.warning("memory put failed for %s: %s", user_id, exc)
            raise

    def delete(self, user_id: str, key: str) -> bool:
        """Remove a single entry. Returns True if the key existed and
        was removed, False if it was already absent."""
        if not user_id or not key:
            return False
        existing = self._read_raw(user_id)
        if key not in existing:
            return False
        existing.pop(key, None)
        try:
            if existing:
                blob = json.dumps(existing, ensure_ascii=False)
                self._get_table().put_item(Item={"id": _row_id(user_id), "entries": blob})
            else:
                # No entries left — drop the row entirely. Empty blob
                # row would still occupy storage and complicate
                # `is empty?` checks elsewhere.
                self._get_table().delete_item(Key={"id": _row_id(user_id)})
        except ClientError as exc:
            logger.warning("memory delete failed for %s: %s", user_id, exc)
            raise
        return True

    def _read_raw(self, user_id: str) -> dict[str, dict[str, Any]]:
        """Read the raw entries dict (key → {value, ts}). Internal use."""
        try:
            res = self._get_table().get_item(Key={"id": _row_id(user_id)})
        except ClientError as exc:
            logger.warning("memory read failed for %s: %s", user_id, exc)
            return {}
        item = res.get("Item") or {}
        raw = item.get("entries") or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}


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
