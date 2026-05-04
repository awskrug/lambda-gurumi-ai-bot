"""Tests for scripts/apps.py — operator CLI for multi-tenant credentials.

Strategy:
  * SSM is mocked with a small hand-rolled fake. The CLI uses only a
    handful of SSM operations (describe_parameters, put_parameter,
    delete_parameters, get_paginator("describe_parameters")), so a fake
    is faster and clearer than wrestling with moto's SSM filters.
  * DynamoDB uses moto so we exercise the real table semantics — the
    CLI's scan + filter expression should match the same `app:` rows
    the runtime writes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import boto3
import pytest

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover
    pytest.skip("moto not installed", allow_module_level=True)

from scripts import apps as apps_cli


TABLE = "lambda-gurumi-bot-test"
REGION = "us-east-1"
PREFIX = "/gurumi-bot/apps"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class _FakeSSMPaginator:
    def __init__(self, params):
        self._params = params

    def paginate(self, ParameterFilters):  # noqa: N803
        prefix = ParameterFilters[0]["Values"][0]
        page = [p for p in self._params if p["Name"].startswith(prefix)]
        yield {"Parameters": page}


class _FakeSSM:
    def __init__(self):
        # Name -> {"Value": ..., "Type": ..., "Version": int, "LastModifiedDate": dt}
        self.params: dict[str, dict] = {}
        self.put_calls = []
        self.delete_calls = []

    def get_paginator(self, name):
        assert name == "describe_parameters"
        params = [
            {"Name": k, "Version": v["Version"], "LastModifiedDate": v["LastModifiedDate"]}
            for k, v in self.params.items()
        ]
        return _FakeSSMPaginator(params)

    def describe_parameters(self, ParameterFilters):  # noqa: N803
        # Used by `get` for single-name lookup
        flt = ParameterFilters[0]
        if flt.get("Key") == "Name" and "Values" in flt:
            wanted = set(flt["Values"])
            results = []
            for k, v in self.params.items():
                if k in wanted:
                    results.append(
                        {"Name": k, "Version": v["Version"], "LastModifiedDate": v["LastModifiedDate"]}
                    )
            return {"Parameters": results}
        return {"Parameters": []}

    def put_parameter(self, *, Name, Value, Type, Overwrite):  # noqa: N803
        self.put_calls.append({"Name": Name, "Type": Type, "Overwrite": Overwrite})
        existing = self.params.get(Name)
        version = (existing["Version"] + 1) if existing else 1
        self.params[Name] = {
            "Value": Value,
            "Type": Type,
            "Version": version,
            "LastModifiedDate": datetime.now(tz=timezone.utc),
        }
        return {"Version": version}

    def delete_parameters(self, *, Names):  # noqa: N803
        self.delete_calls.append(list(Names))
        deleted = []
        invalid = []
        for n in Names:
            if n in self.params:
                del self.params[n]
                deleted.append(n)
            else:
                invalid.append(n)
        return {"DeletedParameters": deleted, "InvalidParameters": invalid}

    def get_parameter(self, *, Name, WithDecryption):  # noqa: N803
        if Name not in self.params:
            from botocore.exceptions import ClientError as _CE

            raise _CE({"Error": {"Code": "ParameterNotFound"}}, "GetParameter")
        return {"Parameter": {"Name": Name, "Value": self.params[Name].get("Value", "")}}


def _create_ddb_table():
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
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _ns(**kwargs):
    """Build a simple argparse-namespace-like object."""
    from types import SimpleNamespace

    return SimpleNamespace(**kwargs)


# --------------------------------------------------------------------------- #
# helpers / pure functions
# --------------------------------------------------------------------------- #


def test_format_ts_handles_none_and_zero():
    assert apps_cli._format_ts(None) == "-"
    assert apps_cli._format_ts(0) == "-"


def test_format_ts_handles_string_int():
    """DynamoDB returns numeric attrs as Decimal — must coerce gracefully."""
    from decimal import Decimal

    out = apps_cli._format_ts(Decimal("1700000000"))
    assert "2023" in out  # 2023-11-14T22:13:20Z


def test_ssm_status_branches():
    f = apps_cli._ssm_status
    assert f({"signing_secret": object(), "bot_token": object()}) == "ok"
    assert f({}) == "none"
    assert "no bot_token" in f({"signing_secret": object()})
    assert "no signing_secret" in f({"bot_token": object()})


def test_list_ssm_apps_skips_unrelated_keys():
    """A future sibling key under the same prefix must not break parsing."""
    ssm = _FakeSSM()
    ssm.params = {
        f"{PREFIX}/A1/signing_secret": {"Version": 1, "LastModifiedDate": datetime.now(tz=timezone.utc)},
        f"{PREFIX}/A1/bot_token": {"Version": 1, "LastModifiedDate": datetime.now(tz=timezone.utc)},
        f"{PREFIX}/A1/extra_key": {"Version": 1, "LastModifiedDate": datetime.now(tz=timezone.utc)},
        f"{PREFIX}/legacy": {"Version": 1, "LastModifiedDate": datetime.now(tz=timezone.utc)},
        f"{PREFIX}/A2/signing_secret": {"Version": 1, "LastModifiedDate": datetime.now(tz=timezone.utc)},
    }
    out = apps_cli._list_ssm_apps(ssm, PREFIX)
    assert set(out.keys()) == {"A1", "A2"}
    assert set(out["A1"].keys()) == {"signing_secret", "bot_token"}
    assert set(out["A2"].keys()) == {"signing_secret"}  # partial


# --------------------------------------------------------------------------- #
# cmd_list — combines DDB + SSM with orphan markers
# --------------------------------------------------------------------------- #


@mock_aws
def test_cmd_list_shows_orphans_in_either_direction(capsys):
    table = _create_ddb_table()
    # A1: both stores
    table.put_item(Item={"id": "app:A1", "first_seen_at": 1700000000, "last_seen_at": 1700000999, "team_id": "T1"})
    # A2: DDB only (was active before secrets got purged)
    table.put_item(Item={"id": "app:A2", "first_seen_at": 1700000000, "last_seen_at": 1700000500, "team_id": "T2"})
    # A3: SSM only (provisioned but never used yet)

    ssm = _FakeSSM()
    now = datetime.now(tz=timezone.utc)
    ssm.params = {
        f"{PREFIX}/A1/signing_secret": {"Version": 1, "LastModifiedDate": now},
        f"{PREFIX}/A1/bot_token": {"Version": 1, "LastModifiedDate": now},
        f"{PREFIX}/A3/signing_secret": {"Version": 1, "LastModifiedDate": now},
        f"{PREFIX}/A3/bot_token": {"Version": 1, "LastModifiedDate": now},
    }

    rc = apps_cli.cmd_list(_ns(json=False), ssm=ssm, table=table, prefix=PREFIX)
    assert rc == 0
    out = capsys.readouterr().out
    # A1: ok / ok
    assert "A1" in out and "ok" in out and "T1" in out
    # A2: SSM gone, metadata still there
    assert "A2" in out and "none" in out  # SSM=none for A2
    # A3: SSM present, no metadata yet
    assert "A3" in out


@mock_aws
def test_cmd_list_empty_returns_friendly_message(capsys):
    table = _create_ddb_table()
    ssm = _FakeSSM()
    rc = apps_cli.cmd_list(_ns(json=False), ssm=ssm, table=table, prefix=PREFIX)
    assert rc == 0
    assert "no apps registered" in capsys.readouterr().out


@mock_aws
def test_cmd_list_json_output_machine_readable(capsys):
    table = _create_ddb_table()
    table.put_item(Item={"id": "app:A1", "first_seen_at": 1700000000, "last_seen_at": 1700000999, "team_id": "T1"})
    ssm = _FakeSSM()
    now = datetime.now(tz=timezone.utc)
    ssm.params = {
        f"{PREFIX}/A1/signing_secret": {"Version": 1, "LastModifiedDate": now},
        f"{PREFIX}/A1/bot_token": {"Version": 1, "LastModifiedDate": now},
    }
    rc = apps_cli.cmd_list(_ns(json=True), ssm=ssm, table=table, prefix=PREFIX)
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed[0]["app_id"] == "A1"
    assert parsed[0]["ssm"] == "ok"
    assert parsed[0]["metadata"] == "ok"
    # NAME column falls through to team_id when no team_name is populated yet.
    assert parsed[0]["name"] == "T1"


# --------------------------------------------------------------------------- #
# cmd_get
# --------------------------------------------------------------------------- #


@mock_aws
def test_cmd_get_missing_secrets_shows_setup_hint(capsys):
    table = _create_ddb_table()
    ssm = _FakeSSM()
    rc = apps_cli.cmd_get(_ns(app_id="A-NEW", json=False), ssm=ssm, table=table, prefix=PREFIX)
    assert rc == 0
    out = capsys.readouterr().out
    assert "A-NEW" in out
    assert "MISSING" in out
    assert "set" in out  # hint to run the set command
    assert "no row" in out  # no DDB metadata yet


@mock_aws
def test_cmd_get_present_secrets_show_versions(capsys):
    table = _create_ddb_table()
    table.put_item(Item={"id": "app:A1", "first_seen_at": 1700000000, "last_seen_at": 1700000500, "team_id": "T1"})
    ssm = _FakeSSM()
    now = datetime.now(tz=timezone.utc)
    ssm.params = {
        f"{PREFIX}/A1/signing_secret": {"Version": 3, "LastModifiedDate": now},
        f"{PREFIX}/A1/bot_token": {"Version": 1, "LastModifiedDate": now},
    }
    rc = apps_cli.cmd_get(_ns(app_id="A1", json=False), ssm=ssm, table=table, prefix=PREFIX)
    assert rc == 0
    out = capsys.readouterr().out
    assert "v3" in out and "v1" in out
    assert "T1" in out


@mock_aws
def test_cmd_get_does_not_print_secret_values(capsys):
    """Defensive: the CLI must NEVER call get_parameter with decryption.
    Even though our code uses describe_parameters (which has no Value
    field), assert the output text doesn't contain anything
    suspiciously secret-shaped."""
    table = _create_ddb_table()
    ssm = _FakeSSM()
    now = datetime.now(tz=timezone.utc)
    ssm.params = {
        f"{PREFIX}/A1/signing_secret": {"Value": "SECRET-DO-NOT-LEAK", "Version": 1, "LastModifiedDate": now},
        f"{PREFIX}/A1/bot_token": {"Value": "xoxb-DO-NOT-LEAK", "Version": 1, "LastModifiedDate": now},
    }
    apps_cli.cmd_get(_ns(app_id="A1", json=False), ssm=ssm, table=table, prefix=PREFIX)
    out = capsys.readouterr().out
    assert "DO-NOT-LEAK" not in out
    assert "xoxb-DO-NOT-LEAK" not in out


# --------------------------------------------------------------------------- #
# cmd_set — interactive + env var paths
# --------------------------------------------------------------------------- #


@mock_aws
def test_cmd_set_writes_both_when_both_provided(monkeypatch, capsys):
    table = _create_ddb_table()
    ssm = _FakeSSM()
    monkeypatch.setenv("SIG_NEW", "new-signing")
    monkeypatch.setenv("TOK_NEW", "new-token")

    rc = apps_cli.cmd_set(
        _ns(app_id="A1", signing_secret_env="SIG_NEW", bot_token_env="TOK_NEW"),
        ssm=ssm,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    assert ssm.params[f"{PREFIX}/A1/signing_secret"]["Value"] == "new-signing"
    assert ssm.params[f"{PREFIX}/A1/bot_token"]["Value"] == "new-token"
    # All puts use Overwrite=True so rotation against an existing secret works.
    for call in ssm.put_calls:
        assert call["Overwrite"] is True
        assert call["Type"] == "SecureString"


@mock_aws
def test_cmd_set_skips_unset_env_var_with_warning(monkeypatch, capsys):
    """One-side rotation: only signing_secret env is populated."""
    table = _create_ddb_table()
    ssm = _FakeSSM()
    # Pre-populate bot_token so we can verify it's left alone
    ssm.params[f"{PREFIX}/A1/bot_token"] = {
        "Value": "old-token",
        "Type": "SecureString",
        "Version": 7,
        "LastModifiedDate": datetime.now(tz=timezone.utc),
    }
    monkeypatch.setenv("SIG_NEW", "new-signing")
    monkeypatch.delenv("TOK_NEW", raising=False)

    rc = apps_cli.cmd_set(
        _ns(app_id="A1", signing_secret_env="SIG_NEW", bot_token_env="TOK_NEW"),
        ssm=ssm,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    assert ssm.params[f"{PREFIX}/A1/signing_secret"]["Value"] == "new-signing"
    assert ssm.params[f"{PREFIX}/A1/bot_token"]["Value"] == "old-token"  # untouched
    err = capsys.readouterr().err
    assert "TOK_NEW" in err and "skipping" in err


@mock_aws
def test_cmd_set_returns_error_when_nothing_to_update(monkeypatch, capsys):
    table = _create_ddb_table()
    ssm = _FakeSSM()
    # Both env vars unset → both prompts return None via _prompter
    rc = apps_cli.cmd_set(
        _ns(
            app_id="A1",
            signing_secret_env=None,
            bot_token_env=None,
            _prompter=lambda _msg: "",  # empty interactive input → skip
        ),
        ssm=ssm,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 1
    assert "nothing to update" in capsys.readouterr().err
    assert ssm.put_calls == []


@mock_aws
def test_cmd_set_interactive_prompter_collects_both(monkeypatch):
    """When no env vars are passed, the prompter callback is used (this
    is `getpass.getpass` in production). Empty answer means skip."""
    table = _create_ddb_table()
    ssm = _FakeSSM()
    answers = iter(["sig-from-prompt", "tok-from-prompt"])

    rc = apps_cli.cmd_set(
        _ns(app_id="A1", signing_secret_env=None, bot_token_env=None, _prompter=lambda _msg: next(answers)),
        ssm=ssm,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    assert ssm.params[f"{PREFIX}/A1/signing_secret"]["Value"] == "sig-from-prompt"
    assert ssm.params[f"{PREFIX}/A1/bot_token"]["Value"] == "tok-from-prompt"


# --------------------------------------------------------------------------- #
# cmd_delete — confirmation + scoping flags
# --------------------------------------------------------------------------- #


@mock_aws
def test_cmd_delete_aborts_when_confirmation_does_not_match(capsys):
    table = _create_ddb_table()
    table.put_item(Item={"id": "app:A1", "team_id": "T1"})
    ssm = _FakeSSM()
    now = datetime.now(tz=timezone.utc)
    ssm.params = {
        f"{PREFIX}/A1/signing_secret": {"Value": "s", "Version": 1, "LastModifiedDate": now},
        f"{PREFIX}/A1/bot_token": {"Value": "t", "Version": 1, "LastModifiedDate": now},
    }

    rc = apps_cli.cmd_delete(
        _ns(app_id="A1", yes=False, keep_secrets=False, keep_metadata=False, _confirmer=lambda _p: "WRONG"),
        ssm=ssm,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 1
    assert "aborted" in capsys.readouterr().err
    # Nothing was actually deleted
    assert ssm.params != {}
    assert table.get_item(Key={"id": "app:A1"}).get("Item") is not None


@mock_aws
def test_cmd_delete_with_correct_confirmation_removes_both(capsys):
    table = _create_ddb_table()
    table.put_item(Item={"id": "app:A1", "team_id": "T1"})
    ssm = _FakeSSM()
    now = datetime.now(tz=timezone.utc)
    ssm.params = {
        f"{PREFIX}/A1/signing_secret": {"Value": "s", "Version": 1, "LastModifiedDate": now},
        f"{PREFIX}/A1/bot_token": {"Value": "t", "Version": 1, "LastModifiedDate": now},
    }

    rc = apps_cli.cmd_delete(
        _ns(app_id="A1", yes=False, keep_secrets=False, keep_metadata=False, _confirmer=lambda _p: "A1"),
        ssm=ssm,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    assert ssm.params == {}
    assert table.get_item(Key={"id": "app:A1"}).get("Item") is None


@mock_aws
def test_cmd_delete_yes_skips_confirmation(capsys):
    table = _create_ddb_table()
    table.put_item(Item={"id": "app:A1", "team_id": "T1"})
    ssm = _FakeSSM()
    now = datetime.now(tz=timezone.utc)
    ssm.params = {
        f"{PREFIX}/A1/signing_secret": {"Value": "s", "Version": 1, "LastModifiedDate": now},
        f"{PREFIX}/A1/bot_token": {"Value": "t", "Version": 1, "LastModifiedDate": now},
    }

    def boom(_p):
        raise AssertionError("must not prompt when --yes is set")

    rc = apps_cli.cmd_delete(
        _ns(app_id="A1", yes=True, keep_secrets=False, keep_metadata=False, _confirmer=boom),
        ssm=ssm,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0


@mock_aws
def test_cmd_delete_keep_metadata_only_removes_ssm():
    table = _create_ddb_table()
    table.put_item(Item={"id": "app:A1", "team_id": "T1"})
    ssm = _FakeSSM()
    now = datetime.now(tz=timezone.utc)
    ssm.params = {
        f"{PREFIX}/A1/signing_secret": {"Value": "s", "Version": 1, "LastModifiedDate": now},
        f"{PREFIX}/A1/bot_token": {"Value": "t", "Version": 1, "LastModifiedDate": now},
    }

    rc = apps_cli.cmd_delete(
        _ns(app_id="A1", yes=True, keep_secrets=False, keep_metadata=True, _confirmer=lambda _p: "A1"),
        ssm=ssm,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    assert ssm.params == {}
    assert table.get_item(Key={"id": "app:A1"}).get("Item") is not None


@mock_aws
def test_cmd_delete_keep_secrets_only_removes_metadata():
    table = _create_ddb_table()
    table.put_item(Item={"id": "app:A1", "team_id": "T1"})
    ssm = _FakeSSM()
    now = datetime.now(tz=timezone.utc)
    ssm.params = {
        f"{PREFIX}/A1/signing_secret": {"Value": "s", "Version": 1, "LastModifiedDate": now},
        f"{PREFIX}/A1/bot_token": {"Value": "t", "Version": 1, "LastModifiedDate": now},
    }

    rc = apps_cli.cmd_delete(
        _ns(app_id="A1", yes=True, keep_secrets=True, keep_metadata=False, _confirmer=lambda _p: "A1"),
        ssm=ssm,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    assert table.get_item(Key={"id": "app:A1"}).get("Item") is None
    assert set(ssm.params.keys()) == {
        f"{PREFIX}/A1/signing_secret",
        f"{PREFIX}/A1/bot_token",
    }


@mock_aws
def test_cmd_delete_both_keep_flags_returns_noop_error(capsys):
    table = _create_ddb_table()
    ssm = _FakeSSM()
    rc = apps_cli.cmd_delete(
        _ns(app_id="A1", yes=True, keep_secrets=True, keep_metadata=True, _confirmer=lambda _p: "A1"),
        ssm=ssm,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 1
    assert "nothing to delete" in capsys.readouterr().err


@mock_aws
def test_cmd_delete_handles_missing_ssm_params_gracefully(capsys):
    """Re-running delete on an already-clean app shouldn't fail —
    `delete_parameters` returns InvalidParameters which we report but
    don't escalate."""
    table = _create_ddb_table()
    ssm = _FakeSSM()
    rc = apps_cli.cmd_delete(
        _ns(app_id="A-gone", yes=True, keep_secrets=False, keep_metadata=False, _confirmer=lambda _p: "A-gone"),
        ssm=ssm,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "not found" in out  # InvalidParameters branch


# --------------------------------------------------------------------------- #
# acl — per-app channel/user allowlist overrides
#
# We re-use the moto DDB fixture via the same `table` helper. SSM is
# unused for ACL commands but the dispatch interface still requires it.
# --------------------------------------------------------------------------- #


def _settings_with_acl(channels=None, users=None):
    """Build a real Settings frozen dataclass with the two ACL fields set.
    Avoids hand-rolling a fake — keeps `cmd_acl_get` exercising the same
    attribute access path it does in production."""
    from src.config import Settings

    return Settings(
        slack_bot_token="",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        image_provider="openai",
        image_model="gpt-image-1",
        agent_max_steps=3,
        response_language="ko",
        dynamodb_table_name="t",
        aws_region="us-east-1",
        allowed_channel_ids=list(channels or []),
        allowed_user_ids=list(users or []),
    )


@mock_aws
def test_cmd_acl_set_writes_per_app_channel_override():
    table = _create_ddb_table()
    rc = apps_cli.cmd_acl_set(
        _ns(app_id="A1", channels="C1,C2", users=None),
        ssm=None,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert item["allowed_channel_ids"] == ["C1", "C2"]
    assert "allowed_user_ids" not in item  # not touched


@mock_aws
def test_cmd_acl_set_empty_string_writes_explicit_empty_list():
    """`--channels=""` is the explicit allow-all override, distinct from
    not setting the flag at all (which would leave the global env var
    in effect)."""
    table = _create_ddb_table()
    rc = apps_cli.cmd_acl_set(
        _ns(app_id="A1", channels="", users=None),
        ssm=None,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert "allowed_channel_ids" in item
    assert item["allowed_channel_ids"] == []


@mock_aws
def test_cmd_acl_set_strips_whitespace_and_drops_empties():
    table = _create_ddb_table()
    rc = apps_cli.cmd_acl_set(
        _ns(app_id="A1", channels=" C1 ,, C2 , ", users=None),
        ssm=None,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert item["allowed_channel_ids"] == ["C1", "C2"]


@mock_aws
def test_cmd_acl_set_both_channels_and_users(capsys):
    table = _create_ddb_table()
    rc = apps_cli.cmd_acl_set(
        _ns(app_id="A1", channels="C1", users="U1,U2"),
        ssm=None,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert item["allowed_channel_ids"] == ["C1"]
    assert item["allowed_user_ids"] == ["U1", "U2"]


@mock_aws
def test_cmd_acl_set_noop_returns_error(capsys):
    table = _create_ddb_table()
    rc = apps_cli.cmd_acl_set(
        _ns(app_id="A1", channels=None, users=None),
        ssm=None,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 1
    assert "nothing to set" in capsys.readouterr().err


@mock_aws
def test_cmd_acl_unset_removes_attribute_only():
    table = _create_ddb_table()
    table.put_item(
        Item={
            "id": "app:A1",
            "team_id": "T1",
            "allowed_channel_ids": ["C1"],
            "allowed_user_ids": ["U1"],
        }
    )
    rc = apps_cli.cmd_acl_unset(
        _ns(app_id="A1", channels=True, users=False),
        ssm=None,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert "allowed_channel_ids" not in item
    # other fields untouched — unset is surgical
    assert item["allowed_user_ids"] == ["U1"]
    assert item["team_id"] == "T1"


@mock_aws
def test_cmd_acl_unset_both_flags(capsys):
    table = _create_ddb_table()
    table.put_item(
        Item={
            "id": "app:A1",
            "team_id": "T1",
            "allowed_channel_ids": ["C1"],
            "allowed_user_ids": ["U1"],
        }
    )
    rc = apps_cli.cmd_acl_unset(
        _ns(app_id="A1", channels=True, users=True),
        ssm=None,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert "allowed_channel_ids" not in item
    assert "allowed_user_ids" not in item
    assert item["team_id"] == "T1"


@mock_aws
def test_cmd_acl_unset_noop_returns_error(capsys):
    table = _create_ddb_table()
    rc = apps_cli.cmd_acl_unset(
        _ns(app_id="A1", channels=False, users=False),
        ssm=None,
        table=table,
        prefix=PREFIX,
    )
    assert rc == 1
    assert "nothing to unset" in capsys.readouterr().err


@mock_aws
def test_cmd_acl_get_shows_per_app_global_and_effective(capsys):
    table = _create_ddb_table()
    table.put_item(
        Item={
            "id": "app:A1",
            "team_id": "T1",
            "allowed_channel_ids": ["C-PER"],
        }
    )
    settings = _settings_with_acl(channels=["C-GLOBAL"], users=["U-GLOBAL"])

    rc = apps_cli.cmd_acl_get(
        _ns(app_id="A1", json=False),
        ssm=None,
        table=table,
        prefix=PREFIX,
        settings=settings,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # channels: per-app set, global differs, effective = per-app
    assert "C-PER" in out
    assert "C-GLOBAL" in out
    # users: not set per-app, falls back to global
    assert "falls back to global" in out
    assert "U-GLOBAL" in out


@mock_aws
def test_cmd_acl_get_shows_explicit_empty_per_app_label(capsys):
    """When per-app is `[]`, the operator needs to see clearly that this
    is INTENTIONAL allow-all — not just an empty-list rendering accident."""
    table = _create_ddb_table()
    table.put_item(Item={"id": "app:A1", "allowed_user_ids": []})
    settings = _settings_with_acl(users=["U-GLOBAL"])

    rc = apps_cli.cmd_acl_get(
        _ns(app_id="A1", json=False),
        ssm=None,
        table=table,
        prefix=PREFIX,
        settings=settings,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "ALLOW ALL" in out
    assert "overrides global" in out


@mock_aws
def test_cmd_acl_get_no_row_falls_back_to_global(capsys):
    table = _create_ddb_table()
    settings = _settings_with_acl(channels=["C-G"], users=["U-G"])
    rc = apps_cli.cmd_acl_get(
        _ns(app_id="A-NEW", json=False),
        ssm=None,
        table=table,
        prefix=PREFIX,
        settings=settings,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "falls back to global" in out
    assert "C-G" in out and "U-G" in out


@mock_aws
def test_cmd_acl_get_json_output(capsys):
    table = _create_ddb_table()
    table.put_item(
        Item={
            "id": "app:A1",
            "allowed_channel_ids": ["C-PER"],
            "allowed_user_ids": [],
        }
    )
    settings = _settings_with_acl(channels=["C-G"], users=["U-G"])

    rc = apps_cli.cmd_acl_get(
        _ns(app_id="A1", json=True),
        ssm=None,
        table=table,
        prefix=PREFIX,
        settings=settings,
    )
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["app_id"] == "A1"
    assert parsed["channels"]["per_app"] == ["C-PER"]
    assert parsed["channels"]["global"] == ["C-G"]
    assert parsed["channels"]["effective"] == ["C-PER"]
    # users: per_app=[] (explicit) → effective=[]
    assert parsed["users"]["per_app"] == []
    assert parsed["users"]["effective"] == []


def test_parse_id_list():
    assert apps_cli._parse_id_list("") == []
    assert apps_cli._parse_id_list("C1") == ["C1"]
    assert apps_cli._parse_id_list("C1,C2") == ["C1", "C2"]
    assert apps_cli._parse_id_list(" C1 , C2 ") == ["C1", "C2"]
    assert apps_cli._parse_id_list("C1,,C2,") == ["C1", "C2"]
    assert apps_cli._parse_id_list(",,") == []


# --------------------------------------------------------------------------- #
# persona — per-app PERSONA_MESSAGE override (string, not list)
# --------------------------------------------------------------------------- #


def _settings_with_persona(persona):
    """Settings with a configurable global persona_message."""
    from src.config import Settings

    return Settings(
        slack_bot_token="",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        image_provider="openai",
        image_model="gpt-image-1",
        agent_max_steps=3,
        response_language="ko",
        dynamodb_table_name="t",
        aws_region="us-east-1",
        persona_message=persona,
    )


@mock_aws
def test_cmd_persona_set_writes_value_from_positional():
    table = _create_ddb_table()
    rc = apps_cli.cmd_persona_set(
        _ns(app_id="A1", value="당신은 친근한 어시스턴트", from_file=None, from_stdin=False),
        ssm=None, table=table, prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert item["persona_message"] == "당신은 친근한 어시스턴트"


@mock_aws
def test_cmd_persona_set_empty_string_writes_explicit_no_persona():
    """`persona set <id> ""` is the explicit no-persona override — distinct
    from `unset` which removes the attribute entirely."""
    table = _create_ddb_table()
    rc = apps_cli.cmd_persona_set(
        _ns(app_id="A1", value="", from_file=None, from_stdin=False),
        ssm=None, table=table, prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert "persona_message" in item
    assert item["persona_message"] == ""


@mock_aws
def test_cmd_persona_set_from_file(tmp_path):
    """Multi-line persona via file — common case for non-trivial personas
    that don't fit comfortably on a CLI argument."""
    table = _create_ddb_table()
    persona_file = tmp_path / "persona.txt"
    persona_file.write_text("line one\nline two\n공식적인 톤")
    rc = apps_cli.cmd_persona_set(
        _ns(app_id="A1", value=None, from_file=str(persona_file), from_stdin=False),
        ssm=None, table=table, prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert item["persona_message"] == "line one\nline two\n공식적인 톤"


@mock_aws
def test_cmd_persona_set_from_stdin(monkeypatch):
    """Pipe-friendly: `cat persona.txt | apps.py persona set A1 --from-stdin`."""
    import io

    table = _create_ddb_table()
    monkeypatch.setattr("sys.stdin", io.StringIO("piped persona"))
    rc = apps_cli.cmd_persona_set(
        _ns(app_id="A1", value=None, from_file=None, from_stdin=True),
        ssm=None, table=table, prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert item["persona_message"] == "piped persona"


@mock_aws
def test_cmd_persona_set_no_input_returns_error(capsys):
    table = _create_ddb_table()
    rc = apps_cli.cmd_persona_set(
        _ns(app_id="A1", value=None, from_file=None, from_stdin=False),
        ssm=None, table=table, prefix=PREFIX,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "no input source" in err


@mock_aws
def test_cmd_persona_unset_removes_attribute_keeping_row():
    """Unset removes ONLY the persona attribute — other metadata (timestamps,
    team_id, ACL) survive."""
    table = _create_ddb_table()
    table.put_item(
        Item={
            "id": "app:A1",
            "team_id": "T1",
            "persona_message": "원래 페르소나",
            "allowed_channel_ids": ["C1"],
        }
    )
    rc = apps_cli.cmd_persona_unset(
        _ns(app_id="A1"), ssm=None, table=table, prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert "persona_message" not in item
    assert item["allowed_channel_ids"] == ["C1"]
    assert item["team_id"] == "T1"


@mock_aws
def test_cmd_persona_get_shows_per_app_global_and_effective(capsys):
    table = _create_ddb_table()
    table.put_item(Item={"id": "app:A1", "persona_message": "PER-APP"})

    rc = apps_cli.cmd_persona_get(
        _ns(app_id="A1", json=False),
        ssm=None, table=table, prefix=PREFIX,
        settings=_settings_with_persona("GLOBAL"),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "PER-APP" in out
    assert "GLOBAL" in out


@mock_aws
def test_cmd_persona_get_explicit_empty_label(capsys):
    """`persona get` must clearly mark the explicit empty-string override
    so an operator doesn't read it as 'nothing set' (which would imply
    falling back to global)."""
    table = _create_ddb_table()
    table.put_item(Item={"id": "app:A1", "persona_message": ""})

    rc = apps_cli.cmd_persona_get(
        _ns(app_id="A1", json=False),
        ssm=None, table=table, prefix=PREFIX,
        settings=_settings_with_persona("GLOBAL"),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "NO PERSONA" in out
    assert "overrides global" in out


@mock_aws
def test_cmd_persona_get_no_attr_falls_back_to_global(capsys):
    table = _create_ddb_table()
    rc = apps_cli.cmd_persona_get(
        _ns(app_id="A-NEW", json=False),
        ssm=None, table=table, prefix=PREFIX,
        settings=_settings_with_persona("GLOBAL persona"),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "falls back to global" in out
    assert "GLOBAL persona" in out


@mock_aws
def test_cmd_persona_get_json_output(capsys):
    table = _create_ddb_table()
    table.put_item(Item={"id": "app:A1", "persona_message": "PER-APP"})
    rc = apps_cli.cmd_persona_get(
        _ns(app_id="A1", json=True),
        ssm=None, table=table, prefix=PREFIX,
        settings=_settings_with_persona("GLOBAL"),
    )
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["app_id"] == "A1"
    assert parsed["per_app"] == "PER-APP"
    assert parsed["global"] == "GLOBAL"
    assert parsed["effective"] == "PER-APP"


# --------------------------------------------------------------------------- #
# refresh / name / list-NAME — make app rows recognizable in `apps list`
# --------------------------------------------------------------------------- #


def test_visual_width_handles_cjk_and_ascii():
    """Hangul / CJK chars take 2 terminal columns, ASCII takes 1."""
    f = apps_cli._visual_width
    assert f("") == 0
    assert f("ascii") == 5
    assert f("당근") == 4              # 2 wide chars
    assert f("당근 / chatgpt") == 14   # 4 + 1 + 1 + 1 + 7 = 14
    assert f("nalbam / gurumi") == 15


def test_pad_uses_visual_width_not_len():
    """`_pad` must reach a TERMINAL column count, not a character count —
    otherwise CJK rows undershoot and later columns shift left."""
    assert apps_cli._pad("ascii", 10) == "ascii     "        # 5 chars + 5 spaces = visual 10
    assert apps_cli._pad("당근", 10) == "당근      "         # visual 4 + 6 spaces = visual 10
    assert apps_cli._pad("당근 / kaptain", 20) == "당근 / kaptain      "  # visual 14 + 6 spaces = visual 20
    # No truncation on overflow — return the string as-is rather than corrupt it.
    assert apps_cli._pad("toolong", 3) == "toolong"


def test_print_table_aligns_cjk_and_ascii_rows(capsys):
    """Regression: when a column mixes CJK and ASCII rows, every cell in the
    next column must start at the same terminal column. Compute row offsets
    by re-measuring with `_visual_width` instead of trusting `len`."""
    apps_cli._print_table(
        ["NAME", "TS"],
        [
            ["당근 / chatgpt", "2026-05-04"],
            ["nalbam / gurumi", "2026-05-04"],
            ["당근 / sreassistant2", "2026-05-04"],
        ],
    )
    out = capsys.readouterr().out.splitlines()
    # All non-separator rows must share the same offset of the second column.
    data_lines = [out[0], out[2], out[3], out[4]]  # header + 3 data rows
    offsets = [apps_cli._visual_width(line.split("2026-05-04")[0]) for line in data_lines[1:]]
    # Header offset is `_visual_width("NAME") + padding + sep`. We just need
    # the data offsets to all match each other.
    assert len(set(offsets)) == 1, f"misaligned offsets: {offsets}"


def test_resolve_name_priority():
    """display_name > team_name+bot > team_name > team_id > -."""
    f = apps_cli._resolve_name
    assert f(None) == "-"
    assert f({}) == "-"
    assert f({"team_id": "T1"}) == "T1"
    assert f({"team_id": "T1", "team_name": "Acme"}) == "Acme"
    assert f({"team_id": "T1", "team_name": "Acme", "bot_user_name": "bot"}) == "Acme / bot"
    # display_name wins even if team info is also present
    assert f({"display_name": "Custom", "team_name": "Acme", "bot_user_name": "bot"}) == "Custom"


class _FakeWebClient:
    """Mocks the slack_sdk WebClient surface that `_call_auth_test` uses."""

    def __init__(self, token: str, *, auth_resp=None, users_resp=None,
                 auth_raises=None, users_raises=None):
        self.token = token
        self._auth_resp = auth_resp
        self._users_resp = users_resp
        self._auth_raises = auth_raises
        self._users_raises = users_raises
        self.auth_test_calls = 0
        self.users_info_calls = []

    def auth_test(self):
        self.auth_test_calls += 1
        if self._auth_raises:
            raise self._auth_raises
        return _FakeSlackResponse(self._auth_resp)

    def users_info(self, *, user):
        self.users_info_calls.append(user)
        if self._users_raises:
            raise self._users_raises
        return _FakeSlackResponse(self._users_resp)


class _FakeSlackResponse:
    """Mimics slack_sdk's SlackResponse: dict-like + .get() + .data."""

    def __init__(self, data):
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)


def _patch_slack_sdk(monkeypatch, fake_client):
    """Patch slack_sdk.WebClient so `_call_auth_test` uses our fake."""
    import slack_sdk
    monkeypatch.setattr(slack_sdk, "WebClient", lambda token: fake_client)


def test_call_auth_test_upgrades_user_handle_to_display_name(monkeypatch):
    """Regression: `auth.test`'s `user` field is the @handle. Operators
    expect the App Display Name (e.g. "Bruce Bot"), so `_call_auth_test`
    follows up with `users.info` and replaces `user` when it gets a
    real_name / display_name."""
    fake = _FakeWebClient(
        "xoxb-x",
        auth_resp={
            "ok": True, "team": "Acme", "user": "sreassistant2",
            "user_id": "U123", "url": "https://acme.slack.com/",
        },
        users_resp={
            "ok": True,
            "user": {
                "real_name": "Bruce Bot",
                "profile": {"display_name": "", "real_name": "Bruce Bot"},
            },
        },
    )
    _patch_slack_sdk(monkeypatch, fake)

    data = apps_cli._call_auth_test("xoxb-x")

    assert data["user"] == "Bruce Bot"  # upgraded from @handle
    assert data["team"] == "Acme"
    assert fake.users_info_calls == ["U123"]


def test_call_auth_test_prefers_display_name_over_real_name(monkeypatch):
    """When the workspace admin set a custom Display name on the bot,
    that wins over real_name."""
    fake = _FakeWebClient(
        "xoxb-x",
        auth_resp={"ok": True, "team": "T", "user": "h", "user_id": "U1", "url": ""},
        users_resp={
            "ok": True,
            "user": {
                "real_name": "Bruce Bot",
                "profile": {"display_name": "Bruce 🤖", "real_name": "Bruce Bot"},
            },
        },
    )
    _patch_slack_sdk(monkeypatch, fake)
    assert apps_cli._call_auth_test("xoxb-x")["user"] == "Bruce 🤖"


def test_call_auth_test_falls_back_to_handle_when_users_info_fails(monkeypatch):
    """Bot may lack `users:read` scope. Don't error — keep the @handle."""
    from slack_sdk.errors import SlackApiError

    fake = _FakeWebClient(
        "xoxb-x",
        auth_resp={"ok": True, "team": "T", "user": "brucebot", "user_id": "U1", "url": ""},
        users_raises=SlackApiError("missing_scope", _FakeSlackResponse({"error": "missing_scope"})),
    )
    _patch_slack_sdk(monkeypatch, fake)
    assert apps_cli._call_auth_test("xoxb-x")["user"] == "brucebot"


def test_call_auth_test_returns_none_when_auth_fails(monkeypatch):
    fake = _FakeWebClient(
        "xoxb-x",
        auth_resp={"ok": False, "error": "invalid_auth"},
    )
    _patch_slack_sdk(monkeypatch, fake)
    assert apps_cli._call_auth_test("xoxb-x") is None


def test_domain_from_url():
    f = apps_cli._domain_from_url
    assert f(None) is None
    assert f("") is None
    assert f("https://acme.slack.com/") == "acme"
    assert f("https://acme.slack.com") == "acme"
    assert f("https://example.com") is None  # not slack.com


def test_cmd_set_calls_auth_test_by_default(monkeypatch):
    """The post-write verify is on by default — populates team_name etc.
    so the operator immediately sees what app got configured."""

    @mock_aws
    def _run():
        table = _create_ddb_table()
        ssm = _FakeSSM()
        monkeypatch.setenv("SIG", "sig-val")
        monkeypatch.setenv("TOK", "tok-val")

        captured = {}

        def fake_auth_test(token):
            captured["token"] = token
            return {"team": "Acme Corp", "user": "my_bot", "url": "https://acme.slack.com/"}

        monkeypatch.setattr(apps_cli, "_call_auth_test", fake_auth_test)

        rc = apps_cli.cmd_set(
            _ns(app_id="A1", signing_secret_env="SIG", bot_token_env="TOK", no_verify=False),
            ssm=ssm, table=table, prefix=PREFIX,
        )
        assert rc == 0
        assert captured["token"] == "tok-val"
        item = table.get_item(Key={"id": "app:A1"}).get("Item")
        assert item["team_name"] == "Acme Corp"
        assert item["bot_user_name"] == "my_bot"
        assert item["team_domain"] == "acme"

    _run()


def test_cmd_set_no_verify_skips_auth_test(monkeypatch):
    @mock_aws
    def _run():
        table = _create_ddb_table()
        ssm = _FakeSSM()
        monkeypatch.setenv("SIG", "sig-val")
        monkeypatch.setenv("TOK", "tok-val")

        def boom(_token):
            raise AssertionError("auth.test must not be called when --no-verify is set")

        monkeypatch.setattr(apps_cli, "_call_auth_test", boom)

        rc = apps_cli.cmd_set(
            _ns(app_id="A1", signing_secret_env="SIG", bot_token_env="TOK", no_verify=True),
            ssm=ssm, table=table, prefix=PREFIX,
        )
        assert rc == 0
        # SSM written, but no DDB metadata enrichment.
        assert table.get_item(Key={"id": "app:A1"}).get("Item") is None

    _run()


def test_cmd_set_auth_test_failure_does_not_break_secret_write(monkeypatch):
    """auth.test failures must be a warning, not a hard error — the SSM
    writes already succeeded and rolling back would be more confusing."""
    @mock_aws
    def _run():
        table = _create_ddb_table()
        ssm = _FakeSSM()
        monkeypatch.setenv("SIG", "sig-val")
        monkeypatch.setenv("TOK", "tok-val")
        monkeypatch.setattr(apps_cli, "_call_auth_test", lambda _t: None)  # simulate failure

        rc = apps_cli.cmd_set(
            _ns(app_id="A1", signing_secret_env="SIG", bot_token_env="TOK", no_verify=False),
            ssm=ssm, table=table, prefix=PREFIX,
        )
        # Secrets still written; DDB metadata not populated.
        assert rc == 0
        assert ssm.params[f"{PREFIX}/A1/bot_token"]["Value"] == "tok-val"
        assert table.get_item(Key={"id": "app:A1"}).get("Item") is None

    _run()


def test_cmd_set_signing_only_does_not_call_auth_test(monkeypatch):
    """When only signing_secret is set (no bot_token to verify), there's
    nothing to call auth.test with — must skip cleanly."""
    @mock_aws
    def _run():
        table = _create_ddb_table()
        ssm = _FakeSSM()
        monkeypatch.setenv("SIG", "sig-val")
        monkeypatch.delenv("TOK", raising=False)

        def boom(_token):
            raise AssertionError("auth.test must not run without bot_token")

        monkeypatch.setattr(apps_cli, "_call_auth_test", boom)

        rc = apps_cli.cmd_set(
            _ns(app_id="A1", signing_secret_env="SIG", bot_token_env="TOK", no_verify=False),
            ssm=ssm, table=table, prefix=PREFIX,
        )
        assert rc == 0

    _run()


@mock_aws
def test_cmd_refresh_populates_team_and_bot_fields(monkeypatch):
    table = _create_ddb_table()
    ssm = _FakeSSM()
    ssm.params[f"{PREFIX}/A1/bot_token"] = {
        "Value": "tok-val", "Type": "SecureString", "Version": 1,
        "LastModifiedDate": datetime.now(tz=timezone.utc),
    }
    monkeypatch.setattr(
        apps_cli, "_call_auth_test",
        lambda _t: {"team": "Acme", "user": "bot", "url": "https://acme.slack.com/"},
    )

    rc = apps_cli.cmd_refresh(_ns(app_id="A1"), ssm=ssm, table=table, prefix=PREFIX)
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert item["team_name"] == "Acme"
    assert item["bot_user_name"] == "bot"


@mock_aws
def test_cmd_refresh_missing_token_returns_error(capsys):
    """No bot_token in SSM = nothing to call auth.test with."""
    table = _create_ddb_table()
    ssm = _FakeSSM()
    rc = apps_cli.cmd_refresh(_ns(app_id="A-NEW"), ssm=ssm, table=table, prefix=PREFIX)
    assert rc == 1
    assert "no bot_token" in capsys.readouterr().err


@mock_aws
def test_cmd_refresh_auth_test_failure_returns_error(monkeypatch):
    table = _create_ddb_table()
    ssm = _FakeSSM()
    ssm.params[f"{PREFIX}/A1/bot_token"] = {
        "Value": "tok", "Type": "SecureString", "Version": 1,
        "LastModifiedDate": datetime.now(tz=timezone.utc),
    }
    monkeypatch.setattr(apps_cli, "_call_auth_test", lambda _t: None)
    rc = apps_cli.cmd_refresh(_ns(app_id="A1"), ssm=ssm, table=table, prefix=PREFIX)
    assert rc == 1


@mock_aws
def test_cmd_name_set_writes_display_name(capsys):
    table = _create_ddb_table()
    rc = apps_cli.cmd_name_set(
        _ns(app_id="A1", name="Production Bot"),
        ssm=None, table=table, prefix=PREFIX,
    )
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert item["display_name"] == "Production Bot"


@mock_aws
def test_cmd_name_unset_removes_display_name_keeping_other_fields():
    table = _create_ddb_table()
    table.put_item(Item={
        "id": "app:A1",
        "display_name": "Old Name",
        "team_name": "Acme",
        "team_id": "T1",
    })
    rc = apps_cli.cmd_name_unset(_ns(app_id="A1"), ssm=None, table=table, prefix=PREFIX)
    assert rc == 0
    item = table.get_item(Key={"id": "app:A1"}).get("Item")
    assert "display_name" not in item
    assert item["team_name"] == "Acme"  # untouched


@mock_aws
def test_cmd_list_uses_name_column_with_priority(capsys):
    """Verify each rung of _resolve_name's priority shows up in the table."""
    table = _create_ddb_table()
    ssm = _FakeSSM()
    now = datetime.now(tz=timezone.utc)
    # Apps populated to varying degrees:
    table.put_item(Item={"id": "app:A1", "display_name": "Custom Label", "team_name": "Acme"})
    table.put_item(Item={"id": "app:A2", "team_name": "BetaCorp", "bot_user_name": "beta_bot"})
    table.put_item(Item={"id": "app:A3", "team_name": "GammaInc"})
    table.put_item(Item={"id": "app:A4", "team_id": "T-DELTA"})
    table.put_item(Item={"id": "app:A5"})  # nothing identifying
    # All have SSM secrets so they show up in list:
    for app_id in ("A1", "A2", "A3", "A4", "A5"):
        for kind in ("signing_secret", "bot_token"):
            ssm.params[f"{PREFIX}/{app_id}/{kind}"] = {
                "Value": "v", "Type": "SecureString", "Version": 1, "LastModifiedDate": now,
            }

    rc = apps_cli.cmd_list(_ns(json=False), ssm=ssm, table=table, prefix=PREFIX)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Custom Label" in out
    assert "BetaCorp / beta_bot" in out
    assert "GammaInc" in out
    assert "T-DELTA" in out
    # A5 has no identifying info → "-" (column should still render)
    assert "A5" in out


@mock_aws
def test_cmd_list_json_uses_name_field(capsys):
    table = _create_ddb_table()
    ssm = _FakeSSM()
    table.put_item(Item={"id": "app:A1", "display_name": "Custom", "team_name": "Acme"})
    ssm.params[f"{PREFIX}/A1/signing_secret"] = {
        "Value": "s", "Version": 1, "LastModifiedDate": datetime.now(tz=timezone.utc),
    }
    ssm.params[f"{PREFIX}/A1/bot_token"] = {
        "Value": "t", "Version": 1, "LastModifiedDate": datetime.now(tz=timezone.utc),
    }
    rc = apps_cli.cmd_list(_ns(json=True), ssm=ssm, table=table, prefix=PREFIX)
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed[0]["app_id"] == "A1"
    assert parsed[0]["name"] == "Custom"


@mock_aws
def test_cmd_get_shows_extended_metadata(capsys):
    table = _create_ddb_table()
    ssm = _FakeSSM()
    table.put_item(Item={
        "id": "app:A1",
        "team_id": "T1",
        "team_name": "Acme Corp",
        "bot_user_name": "my_bot",
        "team_domain": "acme",
        "display_name": "Production",
    })
    rc = apps_cli.cmd_get(_ns(app_id="A1", json=False), ssm=ssm, table=table, prefix=PREFIX)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Acme Corp" in out
    assert "my_bot" in out
    assert "acme" in out
    assert "Production" in out


@mock_aws
def test_cmd_persona_get_json_distinguishes_empty_from_missing(capsys):
    """JSON output must preserve the per_app=null vs per_app="" distinction
    so machine consumers can tell the override-to-empty case from the
    fall-through-to-global case."""
    table = _create_ddb_table()
    # Case 1: per_app explicitly empty
    table.put_item(Item={"id": "app:A1", "persona_message": ""})
    apps_cli.cmd_persona_get(
        _ns(app_id="A1", json=True), ssm=None, table=table, prefix=PREFIX,
        settings=_settings_with_persona("G"),
    )
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["per_app"] == ""
    assert parsed["effective"] == ""

    # Case 2: attribute absent
    apps_cli.cmd_persona_get(
        _ns(app_id="A2", json=True), ssm=None, table=table, prefix=PREFIX,
        settings=_settings_with_persona("G"),
    )
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["per_app"] is None
    assert parsed["effective"] == "G"
