"""Tests for src.tools.memory — remember / forget."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.tools._helpers import _ctx
from src import runtime as _runtime
from src.tools.memory import forget, remember


class _FakeMemory:
    """In-memory MemoryStore stand-in. Records calls so tests can assert
    routing (per-user scope, value passthrough) without a real DDB."""

    def __init__(self):
        self._data: dict[str, dict[str, str]] = {}

    def put(self, user_id, key, value):
        self._data.setdefault(user_id, {})[key] = value

    def get(self, user_id):
        return [
            {"key": k, "value": v, "ts": 0}
            for k, v in self._data.get(user_id, {}).items()
        ]

    def delete(self, user_id, key):
        bucket = self._data.get(user_id, {})
        if key in bucket:
            del bucket[key]
            return True
        return False


def test_remember_writes_under_user_id(monkeypatch):
    fake = _FakeMemory()
    monkeypatch.setattr(_runtime, "_get_memory", lambda: fake)
    ctx = _ctx()
    ctx.user_id = "U1"

    out = remember(ctx, key="company", value="Daangn")

    assert out == {"key": "company", "saved": "ok"}
    assert fake.get("U1") == [{"key": "company", "value": "Daangn", "ts": 0}]


def test_remember_isolates_per_user(monkeypatch):
    """The same key under different users must not collide — the memory
    is per-user scoped, not global."""
    fake = _FakeMemory()
    monkeypatch.setattr(_runtime, "_get_memory", lambda: fake)

    ctx_a = _ctx()
    ctx_a.user_id = "U-ALICE"
    remember(ctx_a, key="favorite", value="ramen")

    ctx_b = _ctx()
    ctx_b.user_id = "U-BOB"
    remember(ctx_b, key="favorite", value="pizza")

    assert {e["value"] for e in fake.get("U-ALICE")} == {"ramen"}
    assert {e["value"] for e in fake.get("U-BOB")} == {"pizza"}


def test_remember_without_user_id_raises(monkeypatch):
    """Memory is per-user; a missing user_id (e.g. a bot-authored event)
    must surface a clear error rather than silently writing to a default
    bucket."""
    fake = _FakeMemory()
    monkeypatch.setattr(_runtime, "_get_memory", lambda: fake)
    ctx = _ctx()
    ctx.user_id = ""

    with pytest.raises(ValueError, match="user context"):
        remember(ctx, key="k", value="v")


def test_forget_removes_existing(monkeypatch):
    fake = _FakeMemory()
    fake.put("U1", "company", "Daangn")
    monkeypatch.setattr(_runtime, "_get_memory", lambda: fake)
    ctx = _ctx()
    ctx.user_id = "U1"

    out = forget(ctx, key="company")

    assert out == {"key": "company", "removed": True}
    assert fake.get("U1") == []


def test_forget_missing_key_returns_false(monkeypatch):
    """Forgetting a non-existent key should not raise — the LLM gets a
    structured 'no such memory' so it can tell the user gracefully."""
    fake = _FakeMemory()
    monkeypatch.setattr(_runtime, "_get_memory", lambda: fake)
    ctx = _ctx()
    ctx.user_id = "U1"

    out = forget(ctx, key="never_saved")

    assert out["removed"] is False
    assert "no such memory" in out["note"]


def test_forget_without_user_id_raises(monkeypatch):
    fake = _FakeMemory()
    monkeypatch.setattr(_runtime, "_get_memory", lambda: fake)
    ctx = _ctx()
    ctx.user_id = ""

    with pytest.raises(ValueError, match="user context"):
        forget(ctx, key="anything")
