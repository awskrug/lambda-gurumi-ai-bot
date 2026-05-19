"""Tests for src.handlers.commands._process_command — slash command worker.

Covers provider/model selection, dedup contract, files_upload_v2 invocation,
and response_url ephemeral error fallback. External boundaries (Slack
WebClient, get_llm, urllib) are stubbed.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from src import runtime as _runtime
from src.handlers import commands as _commands

from tests._helpers import _FakeDedup


@pytest.fixture
def app_module():
    import app

    return app


class _FakeWeb:
    """Minimal Slack WebClient stand-in capturing files_upload_v2 calls."""

    def __init__(self):
        self.uploads: list[dict] = []

    def files_upload_v2(self, **kwargs):
        self.uploads.append(kwargs)
        return {"file": {"permalink": "https://example.com/f"}}


class _FakeLLM:
    def __init__(self, image_bytes: bytes = b"PNGDATA"):
        self.image_bytes = image_bytes
        self.generate_calls: list[str] = []

    def generate_image(self, prompt: str) -> bytes:
        self.generate_calls.append(prompt)
        return self.image_bytes


def _settings_with(**overrides):
    return dataclasses.replace(_runtime.settings, **overrides)


def _stub_response_url(monkeypatch):
    posts: list[dict] = []

    def fake_respond(response_url, text):
        posts.append({"response_url": response_url, "text": text})

    monkeypatch.setattr(_commands, "_respond_error", fake_respond)
    return posts


# --------------------------------------------------------------------------- #
# Happy paths — /img-xai and /img-gpt build the correct LLM
# --------------------------------------------------------------------------- #


def test_img_xai_uses_xai_provider_and_image_model_xai(app_module, monkeypatch):
    monkeypatch.setattr(
        _runtime,
        "settings",
        _settings_with(image_model_xai="grok-imagine-image-quality"),
    )
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    captured: dict = {}

    def spy_get_llm(**kwargs):
        captured.update(kwargs)
        return _FakeLLM(image_bytes=b"xai-bytes")

    monkeypatch.setattr(_commands, "get_llm", spy_get_llm)

    client = _FakeWeb()
    payload = {
        "command": "/img-xai",
        "text": "사과 한 알",
        "channel_id": "C1",
        "user_id": "U1",
        "trigger_id": "trig-1",
        "response_url": "https://hooks.slack.com/r1",
    }
    _commands._process_command(payload, client=client, api_app_id="A1")

    assert captured["image_provider"] == "xai"
    assert captured["image_model"] == "grok-imagine-image-quality"
    assert len(client.uploads) == 1
    upload = client.uploads[0]
    assert upload["channel"] == "C1"
    assert upload["file"] == b"xai-bytes"
    assert upload["filename"] == "generated.png"
    assert upload["title"] == "grok-imagine-image-quality"
    assert "사과 한 알" in upload["initial_comment"]
    assert "/img-xai" in upload["initial_comment"]


def test_img_gpt_uses_openai_provider_and_image_model_gpt(app_module, monkeypatch):
    monkeypatch.setattr(
        _runtime,
        "settings",
        _settings_with(image_model_gpt="gpt-image-2"),
    )
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    captured: dict = {}

    def spy_get_llm(**kwargs):
        captured.update(kwargs)
        return _FakeLLM(image_bytes=b"gpt-bytes")

    monkeypatch.setattr(_commands, "get_llm", spy_get_llm)

    client = _FakeWeb()
    payload = {
        "command": "/img-gpt",
        "text": "blue sky",
        "channel_id": "C2",
        "user_id": "U2",
        "trigger_id": "trig-2",
        "response_url": "https://hooks.slack.com/r2",
    }
    _commands._process_command(payload, client=client, api_app_id="A2")

    assert captured["image_provider"] == "openai"
    assert captured["image_model"] == "gpt-image-2"
    assert client.uploads[0]["file"] == b"gpt-bytes"
    assert client.uploads[0]["channel"] == "C2"


def test_xai_api_key_is_forwarded_to_get_llm(app_module, monkeypatch):
    monkeypatch.setattr(_runtime, "settings", _settings_with(xai_api_key="xai-secret"))
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    captured: dict = {}

    def spy_get_llm(**kwargs):
        captured.update(kwargs)
        return _FakeLLM()

    monkeypatch.setattr(_commands, "get_llm", spy_get_llm)

    _commands._process_command(
        {
            "command": "/img-xai",
            "text": "x",
            "channel_id": "C1",
            "user_id": "U1",
            "trigger_id": "t1",
            "response_url": "https://r",
        },
        client=_FakeWeb(),
        api_app_id="A1",
    )
    assert captured["api_keys"] == {"xai": "xai-secret"}


# --------------------------------------------------------------------------- #
# Bad input — does not call get_llm, posts ephemeral error
# --------------------------------------------------------------------------- #


def test_empty_text_does_not_call_get_llm(app_module, monkeypatch):
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    posts = _stub_response_url(monkeypatch)

    def boom_get_llm(**_kwargs):
        raise AssertionError("get_llm must not be called for empty text")

    monkeypatch.setattr(_commands, "get_llm", boom_get_llm)

    client = _FakeWeb()
    _commands._process_command(
        {
            "command": "/img-xai",
            "text": "   ",
            "channel_id": "C1",
            "user_id": "U1",
            "trigger_id": "t1",
            "response_url": "https://hooks.slack.com/r1",
        },
        client=client,
        api_app_id="A1",
    )
    assert client.uploads == []
    assert len(posts) == 1
    assert "/img-xai" in posts[0]["text"]
    assert posts[0]["response_url"] == "https://hooks.slack.com/r1"


def test_unknown_command_posts_ephemeral_error(app_module, monkeypatch):
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    posts = _stub_response_url(monkeypatch)

    def boom_get_llm(**_kwargs):
        raise AssertionError("get_llm must not be called for unknown command")

    monkeypatch.setattr(_commands, "get_llm", boom_get_llm)

    client = _FakeWeb()
    _commands._process_command(
        {
            "command": "/unknown",
            "text": "x",
            "channel_id": "C1",
            "user_id": "U1",
            "trigger_id": "t1",
            "response_url": "https://hooks.slack.com/r1",
        },
        client=client,
        api_app_id="A1",
    )
    assert client.uploads == []
    assert len(posts) == 1
    assert "/unknown" in posts[0]["text"]


# --------------------------------------------------------------------------- #
# Dedup contract — same two-stage protocol as message handler
# --------------------------------------------------------------------------- #


def test_dedup_already_done_skips_processing(app_module, monkeypatch):
    """A retry for a command we already finished must short-circuit."""
    fake = _FakeDedup()
    fake.done_keys.add("dedup:cmd:trig-done")
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: fake)

    def boom_get_llm(**_kwargs):
        raise AssertionError("get_llm must not run when is_done is True")

    monkeypatch.setattr(_commands, "get_llm", boom_get_llm)

    client = _FakeWeb()
    _commands._process_command(
        {
            "command": "/img-gpt",
            "text": "x",
            "channel_id": "C1",
            "user_id": "U1",
            "trigger_id": "trig-done",
            "response_url": "",
        },
        client=client,
        api_app_id="A1",
    )
    assert client.uploads == []


def test_dedup_in_flight_skips_processing(app_module, monkeypatch):
    """When another worker already reserved this trigger_id, drop the retry."""

    class _BusyDedup(_FakeDedup):
        def reserve(self, key, user="system"):
            return False

    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _BusyDedup())

    def boom_get_llm(**_kwargs):
        raise AssertionError("get_llm must not run when reserve returns False")

    monkeypatch.setattr(_commands, "get_llm", boom_get_llm)

    client = _FakeWeb()
    _commands._process_command(
        {
            "command": "/img-gpt",
            "text": "x",
            "channel_id": "C1",
            "user_id": "U1",
            "trigger_id": "trig-busy",
            "response_url": "",
        },
        client=client,
        api_app_id="A1",
    )
    assert client.uploads == []


def test_mark_done_after_success(app_module, monkeypatch):
    fake = _FakeDedup()
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: fake)
    monkeypatch.setattr(_commands, "get_llm", lambda **_kwargs: _FakeLLM())

    _commands._process_command(
        {
            "command": "/img-gpt",
            "text": "x",
            "channel_id": "C1",
            "user_id": "U1",
            "trigger_id": "trig-ok",
            "response_url": "",
        },
        client=_FakeWeb(),
        api_app_id="A1",
    )
    assert "dedup:cmd:trig-ok" in fake.done_keys


def test_mark_done_not_called_on_failure(app_module, monkeypatch):
    fake = _FakeDedup()
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: fake)
    posts = _stub_response_url(monkeypatch)

    class _BoomLLM:
        def generate_image(self, prompt):
            raise RuntimeError("provider exploded")

    monkeypatch.setattr(_commands, "get_llm", lambda **_kwargs: _BoomLLM())

    client = _FakeWeb()
    _commands._process_command(
        {
            "command": "/img-gpt",
            "text": "x",
            "channel_id": "C1",
            "user_id": "U1",
            "trigger_id": "trig-fail",
            "response_url": "https://r",
        },
        client=client,
        api_app_id="A1",
    )
    assert client.uploads == []
    assert "dedup:cmd:trig-fail" not in fake.done_keys
    assert len(posts) == 1
    assert "이미지 생성 실패" in posts[0]["text"]


def test_files_upload_failure_falls_back_to_response_url(app_module, monkeypatch):
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    posts = _stub_response_url(monkeypatch)
    monkeypatch.setattr(_commands, "get_llm", lambda **_kwargs: _FakeLLM())

    class _BoomWeb:
        def files_upload_v2(self, **_kwargs):
            raise RuntimeError("slack rejected")

    _commands._process_command(
        {
            "command": "/img-gpt",
            "text": "x",
            "channel_id": "C1",
            "user_id": "U1",
            "trigger_id": "trig-upload-fail",
            "response_url": "https://r",
        },
        client=_BoomWeb(),
        api_app_id="A1",
    )
    assert len(posts) == 1
    assert "이미지 생성 실패" in posts[0]["text"]


# --------------------------------------------------------------------------- #
# _respond_error — POSTs ephemeral JSON to response_url
# --------------------------------------------------------------------------- #


def test_respond_error_posts_ephemeral_json(monkeypatch):
    captured: dict = {}

    class _FakeResp:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["method"] = req.get_method()
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    _commands._respond_error("https://hooks.slack.com/abc", "사용법 안내")

    assert captured["url"] == "https://hooks.slack.com/abc"
    assert captured["method"] == "POST"
    assert captured["headers"].get("content-type") == "application/json"
    payload = json.loads(captured["data"].decode("utf-8"))
    assert payload == {"response_type": "ephemeral", "text": "사용법 안내"}


def test_respond_error_silent_when_no_response_url(monkeypatch):
    """An empty response_url must not raise nor attempt any network call."""

    def boom_urlopen(*_args, **_kwargs):
        raise AssertionError("urlopen must not run for empty response_url")

    monkeypatch.setattr("urllib.request.urlopen", boom_urlopen)

    # Should not raise.
    _commands._respond_error("", "ignored")
