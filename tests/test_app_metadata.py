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

from src.app_metadata import AppMetadataStore


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
