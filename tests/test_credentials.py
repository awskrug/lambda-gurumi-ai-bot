"""Tests for src/credentials.py — SSM-backed Slack app credential loader.

We use a hand-rolled fake SSM client rather than moto because the moto
SSM mock for `get_parameters` is feature-complete enough that we'd just
be testing moto. A small fake lets us assert exactly which Names were
requested and inject ClientError to exercise the error path.
"""
from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from src.credentials import CredentialsStore, SlackAppCredentials


class _FakeSSM:
    def __init__(self) -> None:
        self.params: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.raise_next: Exception | None = None

    def get_parameters(self, Names: list[str], WithDecryption: bool) -> dict[str, Any]:  # noqa: N803
        self.calls.append(list(Names))
        if self.raise_next is not None:
            exc = self.raise_next
            self.raise_next = None
            raise exc
        found = [{"Name": n, "Value": self.params[n]} for n in Names if n in self.params]
        invalid = [n for n in Names if n not in self.params]
        return {"Parameters": found, "InvalidParameters": invalid}


def _store(ssm: _FakeSSM, ttl: int = 300) -> CredentialsStore:
    return CredentialsStore(region="us-east-1", ttl_seconds=ttl, client=ssm)


def test_get_returns_credentials_when_both_params_present():
    ssm = _FakeSSM()
    ssm.params["/gurumi-bot/apps/A123/signing_secret"] = "sig"
    ssm.params["/gurumi-bot/apps/A123/bot_token"] = "xoxb-tok"
    store = _store(ssm)

    creds = store.get("A123")
    assert creds == SlackAppCredentials(signing_secret="sig", bot_token="xoxb-tok")


def test_get_returns_none_when_only_signing_secret_present():
    """Partial config = treat as unconfigured. We don't want to half-process."""
    ssm = _FakeSSM()
    ssm.params["/gurumi-bot/apps/A123/signing_secret"] = "sig"
    store = _store(ssm)
    assert store.get("A123") is None


def test_get_returns_none_when_only_bot_token_present():
    ssm = _FakeSSM()
    ssm.params["/gurumi-bot/apps/A123/bot_token"] = "xoxb-tok"
    store = _store(ssm)
    assert store.get("A123") is None


def test_get_returns_none_when_app_unknown():
    ssm = _FakeSSM()
    store = _store(ssm)
    assert store.get("A-unknown") is None


def test_get_caches_positive_result():
    ssm = _FakeSSM()
    ssm.params["/gurumi-bot/apps/A1/signing_secret"] = "s"
    ssm.params["/gurumi-bot/apps/A1/bot_token"] = "t"
    store = _store(ssm)

    store.get("A1")
    store.get("A1")
    store.get("A1")
    assert len(ssm.calls) == 1


def test_get_caches_negative_result():
    """A missing app_id must NOT storm SSM on every retry of a misconfigured bot."""
    ssm = _FakeSSM()
    store = _store(ssm)

    assert store.get("A-missing") is None
    assert store.get("A-missing") is None
    assert store.get("A-missing") is None
    assert len(ssm.calls) == 1


def test_get_does_not_cache_transient_clienterror():
    """ClientError (throttling, network) should retry on the next call."""
    ssm = _FakeSSM()
    ssm.raise_next = ClientError({"Error": {"Code": "Throttling"}}, "GetParameters")
    store = _store(ssm)

    assert store.get("A1") is None
    # Second call: no error injected, but still missing params → None cached
    assert store.get("A1") is None
    # Two SSM calls: first failed, second got empty result
    assert len(ssm.calls) == 2


def test_invalidate_drops_cache():
    ssm = _FakeSSM()
    ssm.params["/gurumi-bot/apps/A1/signing_secret"] = "s"
    ssm.params["/gurumi-bot/apps/A1/bot_token"] = "t"
    store = _store(ssm)

    store.get("A1")
    store.invalidate("A1")
    store.get("A1")
    assert len(ssm.calls) == 2


def test_get_uses_custom_prefix():
    ssm = _FakeSSM()
    ssm.params["/myorg/slack/A1/signing_secret"] = "s"
    ssm.params["/myorg/slack/A1/bot_token"] = "t"
    store = CredentialsStore(region="us-east-1", prefix="/myorg/slack", client=ssm)

    assert store.get("A1") is not None
    assert ssm.calls[0] == [
        "/myorg/slack/A1/signing_secret",
        "/myorg/slack/A1/bot_token",
    ]


def test_get_strips_trailing_slash_from_prefix():
    ssm = _FakeSSM()
    ssm.params["/p/A1/signing_secret"] = "s"
    ssm.params["/p/A1/bot_token"] = "t"
    store = CredentialsStore(region="us-east-1", prefix="/p/", client=ssm)
    assert store.get("A1") is not None


def test_get_empty_app_id_returns_none_without_ssm_call():
    ssm = _FakeSSM()
    store = _store(ssm)
    assert store.get("") is None
    assert ssm.calls == []


def test_get_uses_with_decryption_true():
    """SecureString parameters MUST be requested with decryption — otherwise
    we get ciphertext back instead of the secret value."""
    ssm = _FakeSSM()
    original = ssm.get_parameters
    captured: dict[str, Any] = {}

    def spy(Names, WithDecryption):  # noqa: N803
        captured["with_decryption"] = WithDecryption
        return original(Names=Names, WithDecryption=WithDecryption)

    ssm.get_parameters = spy  # type: ignore[method-assign]
    store = _store(ssm)
    store.get("A1")
    assert captured["with_decryption"] is True


def test_get_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch):
    ssm = _FakeSSM()
    ssm.params["/gurumi-bot/apps/A1/signing_secret"] = "s"
    ssm.params["/gurumi-bot/apps/A1/bot_token"] = "t"
    store = _store(ssm, ttl=60)

    fake_now = [1_000_000.0]
    monkeypatch.setattr("src.credentials.time.time", lambda: fake_now[0])

    store.get("A1")
    fake_now[0] += 30  # within TTL
    store.get("A1")
    assert len(ssm.calls) == 1

    fake_now[0] += 60  # past TTL
    store.get("A1")
    assert len(ssm.calls) == 2


def test_rotation_reflected_after_cache_expires(monkeypatch: pytest.MonkeyPatch):
    """After TTL the next get reflects the new value in SSM."""
    ssm = _FakeSSM()
    ssm.params["/gurumi-bot/apps/A1/signing_secret"] = "old"
    ssm.params["/gurumi-bot/apps/A1/bot_token"] = "tok"
    store = _store(ssm, ttl=60)

    fake_now = [1_000_000.0]
    monkeypatch.setattr("src.credentials.time.time", lambda: fake_now[0])

    assert store.get("A1").signing_secret == "old"
    ssm.params["/gurumi-bot/apps/A1/signing_secret"] = "new"

    fake_now[0] += 30
    assert store.get("A1").signing_secret == "old"  # still cached

    fake_now[0] += 60
    assert store.get("A1").signing_secret == "new"
