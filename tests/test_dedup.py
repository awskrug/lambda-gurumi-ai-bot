import time

import boto3
import pytest

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover
    pytest.skip("moto not installed", allow_module_level=True)

from src.dedup import ConversationStore, DedupStore


TABLE = "lambda-slack-bot-test"
REGION = "us-east-1"


def _create_table():
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
def test_dedup_reserve_first_call_succeeds():
    _create_table()
    store = DedupStore(table_name=TABLE, region=REGION)
    assert store.reserve("abc") is True


@mock_aws
def test_dedup_reserve_second_call_returns_false():
    _create_table()
    store = DedupStore(table_name=TABLE, region=REGION)
    assert store.reserve("abc") is True
    assert store.reserve("abc") is False


@mock_aws
def test_dedup_different_keys_independent():
    _create_table()
    store = DedupStore(table_name=TABLE, region=REGION)
    assert store.reserve("a") is True
    assert store.reserve("b") is True


@mock_aws
def test_count_user_active_ignores_expired():
    _create_table()
    store = DedupStore(table_name=TABLE, region=REGION)
    store.reserve("fresh", user="U1", ttl_seconds=3600)
    # Manually insert an expired record for the same user.
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).put_item(
        Item={"id": "dedup:old", "user": "U1", "expire_at": int(time.time()) - 10}
    )
    assert store.count_user_active("U1") == 1


@mock_aws
def test_count_user_active_unknown_user_zero():
    _create_table()
    store = DedupStore(table_name=TABLE, region=REGION)
    assert store.count_user_active("nobody") == 0


@mock_aws
def test_conversation_put_and_get_roundtrip():
    _create_table()
    convo = ConversationStore(table_name=TABLE, region=REGION)
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    convo.put("T1", "U1", msgs)
    assert convo.get("T1") == msgs


@mock_aws
def test_conversation_get_missing_returns_empty():
    _create_table()
    convo = ConversationStore(table_name=TABLE, region=REGION)
    assert convo.get("unseen") == []


@mock_aws
def test_conversation_truncate_to_chars():
    _create_table()
    convo = ConversationStore(table_name=TABLE, region=REGION)
    msgs = [{"role": "user", "content": "x" * 1000} for _ in range(10)]
    convo.put("T1", "U1", msgs, max_chars=3000)
    stored = convo.get("T1")
    import json
    assert len(json.dumps(stored, ensure_ascii=False)) <= 3000
    assert len(stored) < len(msgs)


def test_conversation_truncate_helper_direct():
    msgs = [{"role": "user", "content": "x" * 500} for _ in range(5)]
    trimmed = ConversationStore.truncate_to_chars(msgs, max_chars=1200)
    import json
    assert len(json.dumps(trimmed, ensure_ascii=False)) <= 1200
    assert len(trimmed) < len(msgs)


def test_conversation_truncate_keeps_newest_messages():
    """Truncation drops the oldest entries — the most recent turn must survive
    as long as it fits."""
    msgs = [
        {"role": "user", "content": f"msg-{i}"}
        for i in range(20)
    ]
    trimmed = ConversationStore.truncate_to_chars(msgs, max_chars=200)
    assert trimmed, "should keep at least some messages"
    # The newest message must be in the kept suffix.
    assert trimmed[-1]["content"] == "msg-19"


def test_conversation_truncate_budget_matches_exact_dumps_length():
    """The fast cumulative-size algorithm must agree with the naive
    json.dumps(kept) size, within a single byte."""
    import json

    msgs = [{"role": "user", "content": "a" * 17 + str(i)} for i in range(8)]
    for budget in (50, 80, 120, 200, 300, 500, 1000, 5000):
        trimmed = ConversationStore.truncate_to_chars(msgs, max_chars=budget)
        assert len(json.dumps(trimmed, ensure_ascii=False)) <= budget, (
            f"budget={budget}, kept={len(trimmed)}, "
            f"actual={len(json.dumps(trimmed, ensure_ascii=False))}"
        )


def test_conversation_truncate_single_large_msg_overflows_budget():
    """If every individual message exceeds the budget, return an empty list
    rather than partial garbage."""
    msgs = [{"role": "user", "content": "x" * 1000}]
    trimmed = ConversationStore.truncate_to_chars(msgs, max_chars=50)
    assert trimmed == []


# --------------------------------------------------------------------------- #
# Two-stage dedup — reserve (short TTL) + mark_done (long TTL) protect against
# the "worker died, Lambda async retry" silent-failure path.
# --------------------------------------------------------------------------- #


@mock_aws
def test_is_done_returns_false_when_no_marker():
    _create_table()
    store = DedupStore(table_name=TABLE, region=REGION)
    assert store.is_done("never-seen") is False


@mock_aws
def test_mark_done_then_is_done_returns_true():
    """Once mark_done writes the long-lived `done:` row, is_done observes it.
    This is the marker that lets a successful run short-circuit retries even
    after the short-TTL `dedup:` row expires."""
    _create_table()
    store = DedupStore(table_name=TABLE, region=REGION)
    assert store.is_done("k1") is False
    store.mark_done("k1")
    assert store.is_done("k1") is True


@mock_aws
def test_reserve_and_mark_done_use_distinct_rows():
    """`reserve` writes `dedup:{key}` and `mark_done` writes `done:{key}` —
    different DDB partitions. A reservation alone does NOT make is_done true,
    which is what allows a worker that crashed mid-handler to be retried."""
    _create_table()
    store = DedupStore(table_name=TABLE, region=REGION)

    assert store.reserve("k2") is True
    assert store.is_done("k2") is False  # reserved but not yet completed

    store.mark_done("k2")
    assert store.is_done("k2") is True


@mock_aws
def test_reserve_default_ttl_outlives_lambda_timeout():
    """The in-flight reservation TTL must be at least as long as the
    Lambda function timeout (300s). If it expires *while* the worker
    is still running a heavy tool (generate_image: 240s), a concurrent
    re-delivery would pass `reserve` and run the agent twice —
    duplicate response.

    Upper bound: well under 1 hour, so a crashed worker doesn't
    permanently block Lambda async retries (the `done:` marker, when
    written, owns the long-term idempotency window).
    """
    _create_table()
    store = DedupStore(table_name=TABLE, region=REGION)
    store.reserve("k3", user="U1")
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    item = table.get_item(Key={"id": "dedup:k3"}).get("Item")
    assert item is not None
    remaining = item["expire_at"] - int(time.time())
    # Lower bound — must outlive the worker's longest tool execution.
    assert remaining >= 240, f"TTL {remaining}s shorter than 240s tool timeout"
    # Upper bound — well under an hour.
    assert remaining < 600
