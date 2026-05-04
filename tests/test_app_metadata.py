"""Tests for src/app_metadata.py — DynamoDB-backed app registry.

Uses moto to give us a real DynamoDB-compatible backend so we can verify
the if_not_exists semantics and that no `expire_at` attribute is written
(which is what keeps these rows alive in a TTL-enabled table).
"""
from __future__ import annotations

import time

import boto3
import pytest

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover
    pytest.skip("moto not installed", allow_module_level=True)

from src.app_metadata import (
    ALLOWED_CHANNEL_IDS_ATTR,
    ALLOWED_USER_IDS_ATTR,
    AppMetadataStore,
)


TABLE = "lambda-gurumi-bot-test"
REGION = "us-east-1"


def _create_table() -> None:
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "user", "AttributeType": "S"},
            {"AttributeName": "expire_at", "AttributeType": "N"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "user-index",
                "KeySchema": [
                    {"AttributeName": "user", "KeyType": "HASH"},
                    {"AttributeName": "expire_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            }
        ],
        ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
    )


@mock_aws
def test_record_creates_row_with_first_and_last_seen():
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    before = int(time.time())
    store.record("A123", team_id="T1")
    item = store.get("A123")
    assert item is not None
    assert item["id"] == "app:A123"
    assert int(item["first_seen_at"]) >= before
    assert int(item["last_seen_at"]) >= before
    assert item["team_id"] == "T1"


@mock_aws
def test_record_does_not_set_expire_at():
    """Critical: TTL-enabled table only deletes items with `expire_at`. App
    metadata must NEVER carry it or the registry would auto-evict."""
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.record("A123", team_id="T1")
    item = store.get("A123")
    assert "expire_at" not in item


@mock_aws
def test_record_preserves_first_seen_on_subsequent_calls(monkeypatch):
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)

    fake_time = [1_000_000]
    monkeypatch.setattr("src.app_metadata.time.time", lambda: fake_time[0])
    store.record("A1", team_id="T1")
    first_seen = int(store.get("A1")["first_seen_at"])

    fake_time[0] = 1_000_500
    store.record("A1", team_id="T1")
    item = store.get("A1")
    assert int(item["first_seen_at"]) == first_seen
    assert int(item["last_seen_at"]) == 1_000_500


@mock_aws
def test_record_overwrites_team_id_on_reinstall():
    """If the same app is moved to another workspace, the row reflects
    the current team."""
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.record("A1", team_id="T-old")
    store.record("A1", team_id="T-new")
    assert store.get("A1")["team_id"] == "T-new"


@mock_aws
def test_record_without_team_id_leaves_existing_team_id_alone():
    """Some Slack events lack team_id; calling record without one must not
    clobber a previously stored value."""
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.record("A1", team_id="T1")
    store.record("A1")  # no team_id
    item = store.get("A1")
    assert item["team_id"] == "T1"


@mock_aws
def test_record_empty_app_id_is_noop():
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.record("", team_id="T1")
    # No throw, no row written
    assert store.get("") is None


@mock_aws
def test_get_unknown_app_returns_none():
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    assert store.get("A-unknown") is None


@mock_aws
def test_record_coexists_with_ttl_rows_in_same_table():
    """App metadata (no expire_at) and dedup rows (with expire_at) share
    the same table — verify both can be written without collision."""
    _create_table()
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    # A dedup-style row with expire_at
    table.put_item(Item={"id": "dedup:msg1", "user": "U1", "expire_at": int(time.time()) + 60})
    # An app row without expire_at
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.record("A1", team_id="T1")

    dedup_item = table.get_item(Key={"id": "dedup:msg1"}).get("Item")
    app_item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert dedup_item is not None and "expire_at" in dedup_item
    assert app_item is not None and "expire_at" not in app_item


# --------------------------------------------------------------------------- #
# record() returns the row (ALL_NEW) — same roundtrip as the write so
# `_process` can read per-app ACL without a separate GetItem.
# --------------------------------------------------------------------------- #


@mock_aws
def test_record_returns_full_row():
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    row = store.record("A1", team_id="T1")
    assert row is not None
    assert row["id"] == "app:A1"
    assert int(row["first_seen_at"]) > 0
    assert int(row["last_seen_at"]) > 0
    assert row["team_id"] == "T1"


@mock_aws
def test_record_returned_row_includes_existing_acl_attributes():
    """If an operator pre-set per-app ACL via the CLI before any event
    arrived, the first record() call must surface those attributes alongside
    the just-written timestamps."""
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.set_allowlist("A1", ALLOWED_CHANNEL_IDS_ATTR, ["C1", "C2"])
    store.set_allowlist("A1", ALLOWED_USER_IDS_ATTR, [])

    row = store.record("A1", team_id="T1")
    assert row is not None
    assert row["allowed_channel_ids"] == ["C1", "C2"]
    # Empty list is preserved (not None / not missing) — this is the key
    # to the "explicit allow all" override semantics.
    assert row["allowed_user_ids"] == []
    assert row["team_id"] == "T1"


@mock_aws
def test_record_empty_app_id_returns_none():
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    assert store.record("") is None


# --------------------------------------------------------------------------- #
# set_allowlist / unset_allowlist — per-app ACL overrides
# --------------------------------------------------------------------------- #


@mock_aws
def test_set_allowlist_writes_attribute():
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.set_allowlist("A1", ALLOWED_CHANNEL_IDS_ATTR, ["C1", "C2"])
    item = store.get("A1")
    assert item["allowed_channel_ids"] == ["C1", "C2"]


@mock_aws
def test_set_allowlist_empty_list_is_preserved_distinct_from_missing():
    """[] is the explicit-allow-all override — it must round-trip as a
    real empty list, not be elided by DynamoDB or boto3 as 'no attribute'."""
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.set_allowlist("A1", ALLOWED_USER_IDS_ATTR, [])
    item = store.get("A1")
    assert "allowed_user_ids" in item
    assert item["allowed_user_ids"] == []


@mock_aws
def test_set_allowlist_creates_row_when_no_metadata_yet():
    """An operator can configure ACL before the bot ever receives an event
    from this app. The row gets created with just the ACL attribute (no
    first_seen_at), and the next record() call adds the timestamps."""
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.set_allowlist("A-NEW", ALLOWED_CHANNEL_IDS_ATTR, ["C1"])
    item = store.get("A-NEW")
    assert item is not None
    assert item["allowed_channel_ids"] == ["C1"]
    assert "first_seen_at" not in item
    assert "team_id" not in item


@mock_aws
def test_set_allowlist_rejects_unknown_attr():
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    with pytest.raises(ValueError, match="unknown ACL attribute"):
        store.set_allowlist("A1", "expire_at", ["999"])
    with pytest.raises(ValueError, match="unknown ACL attribute"):
        store.set_allowlist("A1", "team_id", ["T-evil"])


@mock_aws
def test_set_allowlist_rejects_empty_app_id():
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    with pytest.raises(ValueError, match="app_id is required"):
        store.set_allowlist("", ALLOWED_CHANNEL_IDS_ATTR, ["C1"])


@mock_aws
def test_unset_allowlist_removes_attribute_keeping_row():
    """Removing the ACL override must NOT delete the metadata row — other
    fields (timestamps, team_id) survive so the registry stays accurate."""
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.record("A1", team_id="T1")
    store.set_allowlist("A1", ALLOWED_CHANNEL_IDS_ATTR, ["C1"])
    store.set_allowlist("A1", ALLOWED_USER_IDS_ATTR, ["U1"])

    store.unset_allowlist("A1", ALLOWED_CHANNEL_IDS_ATTR)

    item = store.get("A1")
    assert "allowed_channel_ids" not in item
    assert item["allowed_user_ids"] == ["U1"]  # not touched
    assert item["team_id"] == "T1"  # not touched


@mock_aws
def test_unset_allowlist_on_missing_attr_is_noop():
    """REMOVE on a non-existent attribute is silent in DynamoDB — operator
    re-running unset shouldn't error out."""
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.record("A1")
    store.unset_allowlist("A1", ALLOWED_CHANNEL_IDS_ATTR)  # never was set
    item = store.get("A1")
    assert "allowed_channel_ids" not in item


@mock_aws
def test_unset_allowlist_rejects_unknown_attr():
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    with pytest.raises(ValueError, match="unknown ACL attribute"):
        store.unset_allowlist("A1", "team_id")


@mock_aws
def test_acl_persists_across_record_calls():
    """The most important invariant: per-app ACL set BEFORE an event arrives
    must survive the subsequent record() write that adds timestamps."""
    _create_table()
    store = AppMetadataStore(table_name=TABLE, region=REGION)
    store.set_allowlist("A1", ALLOWED_CHANNEL_IDS_ATTR, ["C1", "C2"])
    store.set_allowlist("A1", ALLOWED_USER_IDS_ATTR, [])

    # Simulate a few events arriving
    store.record("A1", team_id="T1")
    store.record("A1", team_id="T1")
    store.record("A1", team_id="T1")

    item = store.get("A1")
    assert item["allowed_channel_ids"] == ["C1", "C2"]
    assert item["allowed_user_ids"] == []
    assert item["team_id"] == "T1"
