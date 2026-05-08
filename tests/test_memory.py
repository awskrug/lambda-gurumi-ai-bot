"""Tests for src.memory.MemoryStore — per-user persistent memory."""
from __future__ import annotations

import json

import boto3
import pytest

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover
    pytest.skip("moto not installed", allow_module_level=True)

from src.memory import (
    MAX_BLOB_CHARS,
    MAX_ENTRIES_PER_USER,
    MAX_VALUE_CHARS,
    MemoryStore,
)


TABLE = "lambda-gurumi-bot-test"
REGION = "us-east-1"


def _create_table():
    """Mirrors the production table — same key schema as DedupStore tests
    so they can share the moto fixture style."""
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@mock_aws
def test_get_returns_empty_for_unknown_user():
    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    assert store.get("U-NEW") == []


@mock_aws
def test_put_then_get_roundtrip():
    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    store.put("U1", "company", "Daangn")
    out = store.get("U1")
    assert len(out) == 1
    assert out[0]["key"] == "company"
    assert out[0]["value"] == "Daangn"
    assert out[0]["ts"] > 0


@mock_aws
def test_put_overwrites_same_key():
    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    store.put("U1", "company", "OldCo")
    store.put("U1", "company", "NewCo")
    out = store.get("U1")
    assert len(out) == 1
    assert out[0]["value"] == "NewCo"


@mock_aws
def test_get_orders_newest_first():
    """Stable newest-first ordering so the system prompt surfaces the
    most recent context at the top."""
    import time as _time

    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    store.put("U1", "older", "first")
    _time.sleep(0.01)  # ensure ts difference even on fast machines
    store.put("U1", "newer", "second")

    out = store.get("U1")
    assert [e["key"] for e in out][0] == "newer"


@mock_aws
def test_delete_existing_returns_true():
    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    store.put("U1", "k", "v")
    assert store.delete("U1", "k") is True
    assert store.get("U1") == []


@mock_aws
def test_delete_missing_returns_false():
    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    assert store.delete("U1", "never") is False


@mock_aws
def test_delete_last_entry_removes_row_entirely():
    """An empty entries dict shouldn't linger as a phantom row in DDB —
    delete the whole item so future `get` returns the natural empty state."""
    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    store.put("U1", "k", "v")
    store.delete("U1", "k")
    raw = boto3.resource("dynamodb", region_name=REGION).Table(TABLE).get_item(
        Key={"id": "mem:U1"}
    )
    assert raw.get("Item") is None


@mock_aws
def test_users_are_isolated():
    """Per-user scoping: writing to U1 must not appear in U2's memory."""
    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    store.put("U1", "k", "alice-data")
    store.put("U2", "k", "bob-data")
    u1 = store.get("U1")
    u2 = store.get("U2")
    assert {e["value"] for e in u1} == {"alice-data"}
    assert {e["value"] for e in u2} == {"bob-data"}


@mock_aws
def test_put_rejects_empty_user_id():
    store = MemoryStore(table_name=TABLE, region=REGION)
    with pytest.raises(ValueError, match="user_id"):
        store.put("", "k", "v")


@mock_aws
def test_put_rejects_empty_key():
    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    with pytest.raises(ValueError, match="key"):
        store.put("U1", "", "v")


@mock_aws
def test_put_rejects_oversize_value():
    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    huge = "x" * (MAX_VALUE_CHARS + 1)
    with pytest.raises(ValueError, match=str(MAX_VALUE_CHARS)):
        store.put("U1", "k", huge)


@mock_aws
def test_put_rejects_too_many_entries():
    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    for i in range(MAX_ENTRIES_PER_USER):
        store.put("U1", f"k{i}", "v")
    with pytest.raises(ValueError, match="memory full"):
        store.put("U1", "one_too_many", "v")


@mock_aws
def test_get_handles_malformed_blob():
    """A row with a corrupted entries blob shouldn't poison reads — return
    empty list and let the user re-save."""
    _create_table()
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    table.put_item(Item={"id": "mem:U1", "entries": "not-json{{{"})

    store = MemoryStore(table_name=TABLE, region=REGION)
    assert store.get("U1") == []


@mock_aws
def test_no_ttl_attribute_written():
    """Memory rows must NOT carry `expire_at` — DDB TTL would otherwise
    silently evict the user's saved context."""
    _create_table()
    store = MemoryStore(table_name=TABLE, region=REGION)
    store.put("U1", "k", "v")
    raw = boto3.resource("dynamodb", region_name=REGION).Table(TABLE).get_item(
        Key={"id": "mem:U1"}
    )
    assert "expire_at" not in (raw.get("Item") or {})
