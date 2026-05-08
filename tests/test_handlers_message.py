"""Tests for src.handlers.message._process — agent dispatch, allowlists,
per-app overrides, throttle, history persistence."""
import pytest

from src import runtime as _runtime
from src.handlers import message as _message

from tests._helpers import _FakeDedup, _NullMetadata


@pytest.fixture
def app_module():
    """Import the app module fresh."""
    import app

    return app




def test_process_blocked_channel_substitutes_first_allowed_channel(app_module, monkeypatch):
    """비허용 채널 응답의 `{}` 는 ALLOWED_CHANNEL_IDS 의 첫 번째 채널로 치환되며,
    Slack 채널 멘션 형식(`<#ID>`)으로 감싸 클릭 가능한 링크로 렌더되어야 한다."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=["C04PPA399CP", "C08A9550X"],
        allowed_channel_message="구루미에게 질문은 {} 채널을 이용해 주세요~",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    posts = []

    def fake_say(text, thread_ts=None):
        posts.append({"text": text, "thread_ts": thread_ts})

    event = {
        "channel": "C-BLOCKED",
        "ts": "1700000000.000100",
        "text": "hi",
        "user": "U1",
        "client_msg_id": "msg-block-1",
    }
    _message._process(event, client=object(), say=fake_say, is_dm=False)

    assert posts == [
        {
            "text": "구루미에게 질문은 <#C04PPA399CP> 채널을 이용해 주세요~",
            "thread_ts": "1700000000.000100",
        }
    ]


def test_process_blocked_channel_message_without_placeholder_unchanged(app_module, monkeypatch):
    """`{}` 가 없는 메시지는 가공 없이 그대로 전송되어야 한다."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=["C04PPA399CP"],
        allowed_channel_message="허용되지 않은 채널입니다.",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    posts = []
    _message._process(
        {
            "channel": "C-X",
            "ts": "1.1",
            "text": "hi",
            "user": "U1",
            "client_msg_id": "msg-block-2",
        },
        client=object(),
        say=lambda text, thread_ts=None: posts.append({"text": text, "thread_ts": thread_ts}),
        is_dm=False,
    )

    assert posts == [{"text": "허용되지 않은 채널입니다.", "thread_ts": "1.1"}]


def test_process_blocked_channel_no_message_when_unset(app_module, monkeypatch):
    """ALLOWED_CHANNEL_MESSAGE 가 비어 있으면 차단된 채널에서 아무 응답도 가지 않는다."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=["C04PPA399CP"],
        allowed_channel_message="",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    posts = []
    _message._process(
        {
            "channel": "C-X",
            "ts": "1.1",
            "text": "hi",
            "user": "U1",
            "client_msg_id": "msg-block-3",
        },
        client=object(),
        say=lambda text, thread_ts=None: posts.append({"text": text, "thread_ts": thread_ts}),
        is_dm=False,
    )

    assert posts == []


# --------------------------------------------------------------------------- #
# User allowlist — block reply with first-user substitution (channel + DM)
# --------------------------------------------------------------------------- #


def test_process_blocked_user_is_silent_even_with_message_set(app_module, monkeypatch, caplog):
    """비허용 유저는 ALLOWED_USER_MESSAGE 가 설정되어 있어도 응답을 받지 않는다.
    의도적으로 silent 처리 — 외부에 봇 존재를 노출하지 않기 위함. 차단이 실제로
    일어났는지는 log_event 의 user.blocked 레코드로 확인한다."""
    import dataclasses
    import logging

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=[],
        allowed_user_ids=["U-ADMIN", "U-OPS"],
        allowed_user_message="이 봇은 {} 만 답변합니다.",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    posts = []
    with caplog.at_level(logging.INFO, logger="app"):
        _message._process(
            {
                "channel": "C-OK",
                "ts": "1.1",
                "text": "hi",
                "user": "U-RANDOM",
                "client_msg_id": "msg-user-1",
            },
            client=object(),
            say=lambda text, thread_ts=None: posts.append({"text": text, "thread_ts": thread_ts}),
            is_dm=False,
        )

    assert posts == []
    assert any("user.blocked" in r.getMessage() for r in caplog.records)


def test_process_blocked_user_in_dm_is_silent(app_module, monkeypatch, caplog):
    """유저 화이트리스트는 DM 경로에도 적용되되 — 채널 화이트리스트와 다르게 —
    차단 시 응답 없이 silent 처리. 차단 자체는 log_event 로 확인."""
    import dataclasses
    import logging

    override = dataclasses.replace(
        _runtime.settings,
        allowed_user_ids=["U-ADMIN"],
        allowed_user_message="DM 도 차단합니다.",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    posts = []
    with caplog.at_level(logging.INFO, logger="app"):
        _message._process(
            {
                "channel": "D-DM",
                "ts": "1.1",
                "text": "hi",
                "user": "U-RANDOM",
                "client_msg_id": "msg-user-2",
            },
            client=object(),
            say=lambda text, thread_ts=None: posts.append({"text": text, "thread_ts": thread_ts}),
            is_dm=True,
        )

    assert posts == []
    assert any("user.blocked" in r.getMessage() for r in caplog.records)


def test_process_blocked_user_no_message_when_unset(app_module, monkeypatch):
    """ALLOWED_USER_MESSAGE 가 비어 있으면 차단된 유저에게 응답이 가지 않는다."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_user_ids=["U-ADMIN"],
        allowed_user_message="",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    posts = []
    _message._process(
        {
            "channel": "C-OK",
            "ts": "1.1",
            "text": "hi",
            "user": "U-RANDOM",
            "client_msg_id": "msg-user-3",
        },
        client=object(),
        say=lambda text, thread_ts=None: posts.append({"text": text, "thread_ts": thread_ts}),
        is_dm=False,
    )

    assert posts == []


def test_process_blocked_channel_short_circuits_before_user_check(app_module, monkeypatch):
    """채널·유저 둘 다 차단인 경우 채널 메시지 한 번만 전송 — 유저 검사로 진행 안 됨."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=["C-OK"],
        allowed_channel_message="채널 차단",
        allowed_user_ids=["U-ADMIN"],
        allowed_user_message="유저 차단",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    posts = []
    _message._process(
        {
            "channel": "C-BAD",
            "ts": "1.1",
            "text": "hi",
            "user": "U-RANDOM",
            "client_msg_id": "msg-both-1",
        },
        client=object(),
        say=lambda text, thread_ts=None: posts.append({"text": text, "thread_ts": thread_ts}),
        is_dm=False,
    )

    assert posts == [{"text": "채널 차단", "thread_ts": "1.1"}]


# --------------------------------------------------------------------------- #
# App metadata — auto-record on first call
# --------------------------------------------------------------------------- #


def test_process_records_app_metadata_when_api_app_id_present(app_module, monkeypatch):
    """First time we successfully process an event for an app, write a row
    so the registry can show first_seen / last_seen."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=[],
        allowed_user_ids=[],
        allowed_channel_message="",
        allowed_user_message="",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    recorded = []

    class _Spy:
        def record(self, app_id, team_id=None):
            recorded.append({"app_id": app_id, "team_id": team_id})

    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _Spy())
    # Use the real _process and monkeypatch its internals.
    # Stub the rest of _process so it doesn't try to actually run an agent.
    monkeypatch.setattr(_runtime, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(_runtime, "_get_llm", lambda: object())
    monkeypatch.setattr(_message, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(_message, "user_name_cache", _StubUserNameCache())
    monkeypatch.setattr(_message, "SlackMentionAgent", _StubAgent)
    monkeypatch.setattr(_message, "StreamingMessage", _StubStream)

    event = {
        "channel": "C1",
        "ts": "1.1",
        "text": "hello",
        "user": "U1",
        "client_msg_id": "msg-meta-1",
        "team": "T-WORKSPACE",
    }
    _message._process(
        event,
        client=_StubClient(),
        say=lambda **kw: None,
        is_dm=False,
        api_app_id="A1",
    )

    assert recorded == [{"app_id": "A1", "team_id": "T-WORKSPACE"}]


def test_process_skips_metadata_when_api_app_id_blank(app_module, monkeypatch):
    """Defensive path (api_app_id="") must not write metadata — otherwise
    we'd pollute the registry with a row keyed on the empty string."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=[],
        allowed_user_ids=[],
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    def boom_metadata():
        raise AssertionError("metadata must not be touched without api_app_id")

    monkeypatch.setattr(_runtime, "_get_app_metadata", boom_metadata)
    monkeypatch.setattr(_runtime, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(_runtime, "_get_llm", lambda: object())
    monkeypatch.setattr(_message, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(_message, "user_name_cache", _StubUserNameCache())
    monkeypatch.setattr(_message, "SlackMentionAgent", _StubAgent)
    monkeypatch.setattr(_message, "StreamingMessage", _StubStream)

    _message._process(
        {"channel": "C1", "ts": "1.1", "text": "hello", "user": "U1", "client_msg_id": "msg-no-meta"},
        client=_StubClient(),
        say=lambda **kw: None,
        is_dm=False,
        api_app_id="",
    )


# --------------------------------------------------------------------------- #
# Stubs for the metadata tests above (kept here, not in conftest, because
# they're tightly coupled to which _process internals get mocked).
# --------------------------------------------------------------------------- #


class _StubConvo:
    def get(self, _t):
        return []

    def put(self, *_a, **_kw):
        pass


class _StubUserNameCache:
    def get(self, _client, _user):
        return ""


class _StubClient:
    def chat_postMessage(self, **_kw):
        return {"ts": "1.2"}


class _StubAgent:
    def __init__(self, **_kw):
        pass

    def run(self, _text):
        from types import SimpleNamespace

        return SimpleNamespace(text="ok", steps=1, tool_calls_count=0, token_usage={"input": 0, "output": 0}, image_url=None)


class _StubStream:
    def __init__(self, **_kw):
        self.ts = None

    def start(self):
        self.ts = "1.5"

    def append(self, _delta):
        pass

    def stop(self, _final):
        pass


# --------------------------------------------------------------------------- #
# Per-app ACL — DynamoDB row attributes override the global env var.
#
# The contract under test (mirrored in src/app_metadata.py):
#   - attribute ABSENT  → use the global env var
#   - attribute PRESENT → use per-app, ignore global
#   - attribute is `[]` → "this app explicitly allows all" — even when the
#                          global is restrictive
# --------------------------------------------------------------------------- #


def _stub_metadata_returning(row):
    """Build a fake AppMetadataStore whose record() returns the given row.
    `None` simulates a DynamoDB read failure or a not-yet-initialized app."""

    class _Stub:
        def record(self, _app_id, team_id=None):
            return row

    return _Stub()


def test_process_per_app_channel_override_beats_global(app_module, monkeypatch):
    """Global allowlist=[C-GLOBAL]. Per-app override=[C-OVERRIDE]. A request
    in C-GLOBAL must be BLOCKED (it's not in the per-app list), and the
    block message's `{}` substitution uses the per-app channel."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=["C-GLOBAL"],
        allowed_channel_message="use {} please",
        allowed_user_ids=[],
        allowed_user_message="",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(
        _runtime,
        "_get_app_metadata",
        lambda: _stub_metadata_returning({"id": "app:A1", "allowed_channel_ids": ["C-OVERRIDE"]}),
    )

    posts = []
    _message._process(
        {"channel": "C-GLOBAL", "ts": "1.1", "text": "hi", "user": "U1", "client_msg_id": "msg-acl-ch-1"},
        client=object(),
        say=lambda text, thread_ts=None: posts.append(text),
        is_dm=False,
        api_app_id="A1",
    )

    # Blocked because C-GLOBAL is not in per-app [C-OVERRIDE], and `{}`
    # substitutes the per-app channel — operator gets pointed to the right
    # place for THIS app, not the deployment-wide default.
    assert posts == ["use <#C-OVERRIDE> please"]


def test_process_per_app_empty_channel_list_allows_all_overriding_global(app_module, monkeypatch):
    """Global allowlist=[C-GLOBAL] (restrictive). Per-app=[] means
    'this specific app is unrestricted' — request in any channel passes."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=["C-GLOBAL"],
        allowed_channel_message="should NOT see this",
        allowed_user_ids=[],
        allowed_user_message="",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(
        _runtime,
        "_get_app_metadata",
        lambda: _stub_metadata_returning({"id": "app:A1", "allowed_channel_ids": []}),
    )
    # Stub the rest so the agent path doesn't actually run.
    monkeypatch.setattr(_runtime, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(_runtime, "_get_llm", lambda: object())
    monkeypatch.setattr(_message, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(_message, "user_name_cache", _StubUserNameCache())
    monkeypatch.setattr(_message, "SlackMentionAgent", _StubAgent)
    monkeypatch.setattr(_message, "StreamingMessage", _StubStream)

    posts = []
    _message._process(
        {"channel": "C-NOT-IN-GLOBAL", "ts": "1.1", "text": "hi", "user": "U1", "client_msg_id": "msg-acl-ch-2"},
        client=_StubClient(),
        say=lambda **kw: posts.append(kw),
        is_dm=False,
        api_app_id="A1",
    )

    # No block message — the agent path ran instead.
    assert posts == []


def test_process_per_app_missing_channel_falls_back_to_global(app_module, monkeypatch):
    """Row exists (timestamps etc.) but no allowed_channel_ids attribute →
    the global env var is the effective allowlist."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=["C-GLOBAL"],
        allowed_channel_message="global blocks: use {}",
        allowed_user_ids=[],
        allowed_user_message="",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(
        _runtime,
        "_get_app_metadata",
        # Row present, but no ACL attribute → use global
        lambda: _stub_metadata_returning({"id": "app:A1", "first_seen_at": 1700000000}),
    )

    posts = []
    _message._process(
        {"channel": "C-OTHER", "ts": "1.1", "text": "hi", "user": "U1", "client_msg_id": "msg-acl-ch-3"},
        client=object(),
        say=lambda text, thread_ts=None: posts.append(text),
        is_dm=False,
        api_app_id="A1",
    )

    assert posts == ["global blocks: use <#C-GLOBAL>"]


def test_process_per_app_user_override_beats_global(app_module, monkeypatch, caplog):
    """Per-app allowed_user_ids=[U-PER] must replace the global allowlist
    [U-GLOBAL] for THIS app. U-GLOBAL is in the global list but NOT the
    per-app list → blocked. The block itself is silent (no message), so
    we verify via the user.blocked log event."""
    import dataclasses
    import logging

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=[],
        allowed_user_ids=["U-GLOBAL"],
        allowed_user_message="only {} can talk to this app",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(
        _runtime,
        "_get_app_metadata",
        lambda: _stub_metadata_returning({"id": "app:A1", "allowed_user_ids": ["U-PER"]}),
    )

    posts = []
    with caplog.at_level(logging.INFO, logger="app"):
        _message._process(
            {"channel": "C1", "ts": "1.1", "text": "hi", "user": "U-GLOBAL", "client_msg_id": "msg-acl-u-1"},
            client=object(),
            say=lambda text, thread_ts=None: posts.append(text),
            is_dm=False,
            api_app_id="A1",
        )

    assert posts == []
    assert any("user.blocked" in r.getMessage() for r in caplog.records)


def test_process_per_app_empty_user_list_allows_all_users_overriding_global(app_module, monkeypatch):
    """Global users=[U-ADMIN] (restrictive). Per-app users=[] means
    anyone can use THIS app — even users not in the global list."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=[],
        allowed_user_ids=["U-ADMIN"],
        allowed_user_message="should NOT see this",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(
        _runtime,
        "_get_app_metadata",
        lambda: _stub_metadata_returning({"id": "app:A1", "allowed_user_ids": []}),
    )
    monkeypatch.setattr(_runtime, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(_runtime, "_get_llm", lambda: object())
    monkeypatch.setattr(_message, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(_message, "user_name_cache", _StubUserNameCache())
    monkeypatch.setattr(_message, "SlackMentionAgent", _StubAgent)
    monkeypatch.setattr(_message, "StreamingMessage", _StubStream)

    posts = []
    _message._process(
        {"channel": "C1", "ts": "1.1", "text": "hi", "user": "U-RANDOM", "client_msg_id": "msg-acl-u-2"},
        client=_StubClient(),
        say=lambda **kw: posts.append(kw),
        is_dm=False,
        api_app_id="A1",
    )

    assert posts == []  # passed through


# --------------------------------------------------------------------------- #
# Per-app PERSONA_MESSAGE — same override contract as ACL but for a string.
#   - attribute ABSENT → use global env var
#   - attribute PRESENT → per-app value, even if `""` (= no persona)
# --------------------------------------------------------------------------- #


class _CapturingAgent:
    """Stub SlackMentionAgent that captures constructor kwargs so tests can
    assert what `_process` actually wired in."""

    captured: dict = {}

    def __init__(self, **kwargs):
        type(self).captured = kwargs

    def run(self, _text):
        from types import SimpleNamespace

        return SimpleNamespace(
            text="ok", steps=1, tool_calls_count=0,
            token_usage={"input": 0, "output": 0}, image_url=None,
        )


def _stub_process_dependencies(app_module, monkeypatch, *, app_row):
    """Wire up _process so it can run end-to-end against fakes, with a
    capturing agent to inspect the kwargs."""
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(
        _runtime,
        "_get_app_metadata",
        lambda: _stub_metadata_returning(app_row),
    )
    monkeypatch.setattr(_runtime, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(_runtime, "_get_llm", lambda: object())
    monkeypatch.setattr(_message, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(_message, "user_name_cache", _StubUserNameCache())
    monkeypatch.setattr(_message, "SlackMentionAgent", _CapturingAgent)
    monkeypatch.setattr(_message, "StreamingMessage", _StubStream)


def _run_process(app_module, **process_kwargs):
    event = process_kwargs.pop("event", {
        "channel": "C1", "ts": "1.1", "text": "hello", "user": "U1",
        "client_msg_id": "msg-persona-1",
    })
    _message._process(
        event,
        client=_StubClient(),
        say=lambda **kw: None,
        is_dm=False,
        api_app_id=process_kwargs.pop("api_app_id", "A1"),
    )


def test_process_per_app_persona_overrides_global(app_module, monkeypatch):
    """Per-app `persona_message` wins over `settings.persona_message`."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        persona_message="GLOBAL: be concise",
        allowed_channel_ids=[], allowed_user_ids=[],
    )
    monkeypatch.setattr(_runtime, "settings", override)
    _stub_process_dependencies(
        app_module, monkeypatch,
        app_row={"id": "app:A1", "persona_message": "PER-APP: be playful"},
    )

    _run_process(app_module)

    assert _CapturingAgent.captured["persona_message"] == "PER-APP: be playful"
    # SYSTEM_MESSAGE has NO per-app override — always global.
    assert _CapturingAgent.captured["system_message"] == override.system_message


def test_process_per_app_empty_persona_overrides_non_empty_global(app_module, monkeypatch):
    """Per-app `""` is the explicit "no persona for this app" override —
    must be passed through, NOT silently treated as 'use global'."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        persona_message="GLOBAL: be formal",
        allowed_channel_ids=[], allowed_user_ids=[],
    )
    monkeypatch.setattr(_runtime, "settings", override)
    _stub_process_dependencies(
        app_module, monkeypatch,
        app_row={"id": "app:A1", "persona_message": ""},
    )

    _run_process(app_module)

    # Empty string passed through. Agent's own `if self.persona_message:`
    # check then renders this as "no persona section" — but the override
    # decision happened HERE, in _process.
    assert _CapturingAgent.captured["persona_message"] == ""


def test_process_persona_falls_back_to_global_when_attr_absent(app_module, monkeypatch):
    """Row exists (timestamps etc.) but no persona_message attribute →
    behavior is the global persona."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        persona_message="GLOBAL persona",
        allowed_channel_ids=[], allowed_user_ids=[],
    )
    monkeypatch.setattr(_runtime, "settings", override)
    _stub_process_dependencies(
        app_module, monkeypatch,
        app_row={"id": "app:A1", "first_seen_at": 1700000000},
    )

    _run_process(app_module)

    assert _CapturingAgent.captured["persona_message"] == "GLOBAL persona"


def test_process_persona_falls_back_to_global_when_record_fails(app_module, monkeypatch):
    """If `record()` returns None (DDB outage), per-app override can't be
    resolved — global persona must still apply."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        persona_message="GLOBAL persona",
        allowed_channel_ids=[], allowed_user_ids=[],
    )
    monkeypatch.setattr(_runtime, "settings", override)
    _stub_process_dependencies(app_module, monkeypatch, app_row=None)

    _run_process(app_module)

    assert _CapturingAgent.captured["persona_message"] == "GLOBAL persona"


def test_process_metadata_failure_falls_back_to_global_acl(app_module, monkeypatch):
    """If the DynamoDB row read/write fails (record() raises or returns None),
    the bot must NOT lock everyone out — it falls back to the global env-var
    ACL so transient DDB issues don't take the bot offline."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=["C-OK"],
        allowed_channel_message="global: {}",
        allowed_user_ids=[],
        allowed_user_message="",
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    class _Broken:
        def record(self, *_a, **_kw):
            raise RuntimeError("DDB unreachable")

    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _Broken())

    posts = []
    _message._process(
        {"channel": "C-BLOCKED", "ts": "1.1", "text": "hi", "user": "U1", "client_msg_id": "msg-ddb-fail"},
        client=object(),
        say=lambda text, thread_ts=None: posts.append(text),
        is_dm=False,
        api_app_id="A1",
    )

    # Global C-OK list still enforced; substitution still works.
    assert posts == ["global: <#C-OK>"]


# --------------------------------------------------------------------------- #
# Two-stage dedup — `done:` short-circuit + `mark_done` on success.
#
# Together these protect against the silent-failure path: if a worker
# died mid-agent and Lambda async retries, the short-TTL `dedup:` row
# has expired and the retry can re-enter. Once a run succeeds we lock
# in `done:` so subsequent retries are quietly skipped.
# --------------------------------------------------------------------------- #


def test_process_skips_when_already_done(app_module, monkeypatch):
    """A request whose `done:` marker exists from a prior successful run
    must short-circuit before reserve / agent / response."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=[],
        allowed_user_ids=[],
    )
    monkeypatch.setattr(_runtime, "settings", override)

    class _DoneAlready:
        def is_done(self, _key):
            return True

        def reserve(self, *_a, **_kw):
            raise AssertionError("reserve must not run when is_done returned True")

        def mark_done(self, *_a, **_kw):
            raise AssertionError("mark_done must not run on a short-circuit")

        def count_user_active(self, _u):
            return 0

    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _DoneAlready())

    def boom_agent(**_kw):
        raise AssertionError("agent must not run when already done")

    monkeypatch.setattr(_message, "SlackMentionAgent", boom_agent)

    posts = []
    _message._process(
        {"channel": "C1", "ts": "1.1", "text": "hi", "user": "U1", "client_msg_id": "msg-done"},
        client=object(),
        say=lambda **kw: posts.append(kw),
        is_dm=False,
    )

    assert posts == []


def test_process_calls_mark_done_after_successful_run(app_module, monkeypatch):
    """The long-lived `done:` marker must be written after the agent run
    delivers its response — so subsequent retries see it and short-circuit."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=[],
        allowed_user_ids=[],
    )
    monkeypatch.setattr(_runtime, "settings", override)

    marked: list[str] = []

    class _RecordingDedup:
        def is_done(self, _key):
            return False

        def reserve(self, _key, user="system"):
            return True

        def mark_done(self, key, user="system"):
            marked.append(key)

        def count_user_active(self, _u):
            return 0

    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _RecordingDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _NullMetadata())
    monkeypatch.setattr(_runtime, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(_runtime, "_get_llm", lambda: object())
    monkeypatch.setattr(_message, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(_message, "user_name_cache", _StubUserNameCache())
    monkeypatch.setattr(_message, "SlackMentionAgent", _StubAgent)
    monkeypatch.setattr(_message, "StreamingMessage", _StubStream)

    _message._process(
        {"channel": "C1", "ts": "1.1", "text": "hi", "user": "U1", "client_msg_id": "msg-success"},
        client=_StubClient(),
        say=lambda **kw: None,
        is_dm=False,
    )

    assert marked == ["dedup:msg-success"]


def test_process_does_not_mark_done_when_agent_raises(app_module, monkeypatch):
    """If agent.run raises, the `dedup:` row is left to expire on its own
    (short TTL) and `done:` is NOT written — Lambda async retry will be
    allowed through after the in-flight TTL window."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=[],
        allowed_user_ids=[],
    )
    monkeypatch.setattr(_runtime, "settings", override)

    class _RecordingDedup:
        def __init__(self):
            self.marked: list[str] = []

        def is_done(self, _key):
            return False

        def reserve(self, _key, user="system"):
            return True

        def mark_done(self, key, user="system"):
            self.marked.append(key)

        def count_user_active(self, _u):
            return 0

    dedup = _RecordingDedup()
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: dedup)
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _NullMetadata())
    monkeypatch.setattr(_runtime, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(_runtime, "_get_llm", lambda: object())
    monkeypatch.setattr(_message, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(_message, "user_name_cache", _StubUserNameCache())
    monkeypatch.setattr(_message, "StreamingMessage", _StubStream)

    class _BoomAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _text):
            raise RuntimeError("agent exploded")

    monkeypatch.setattr(_message, "SlackMentionAgent", _BoomAgent)

    posts = []
    _message._process(
        {"channel": "C1", "ts": "1.1", "text": "hi", "user": "U1", "client_msg_id": "msg-fail"},
        client=_StubClient(),
        say=lambda **kw: posts.append(kw),
        is_dm=False,
    )

    assert dedup.marked == []  # no done marker written on failure


# --------------------------------------------------------------------------- #
# Mention handling — strip ONLY the bot's own mention; pre-warm any other
# user mentions so fetch_user_profile can resolve them on the first call.
# Regression: a CloudWatch incident showed the LLM calling
# fetch_user_profile("Uno") with an empty cache and failing — root cause was
# the mention regex stripping ALL `<@U…>` from the LLM's view of the text.
# --------------------------------------------------------------------------- #


def test_strip_bot_mention_preserves_other_user_mentions():
    """Direct unit test for the strip helper: only the bot's mention is
    removed; co-mentioned users stay so the LLM can route them into
    fetch_user_profile via the mention-syntax parser in `_resolve_user_id`."""
    from src.handlers.message import _strip_bot_mention

    raw = "<@U0BOT01> <@U0UNO01> 프로필 그려"
    out = _strip_bot_mention(raw, "U0BOT01")
    assert "<@U0UNO01>" in out
    assert "<@U0BOT01>" not in out


def test_strip_bot_mention_handles_alias_form():
    """Slack sometimes emits `<@USERID|displayname>` — strip those too when
    the USERID matches the bot."""
    from src.handlers.message import _strip_bot_mention

    raw = "<@U0BOT01|GurumiBot> <@U0UNO01|Uno> 그려"
    out = _strip_bot_mention(raw, "U0BOT01")
    assert "<@U0UNO01|Uno>" in out
    assert "U0BOT01" not in out


def test_strip_bot_mention_noop_when_bot_id_unknown():
    """If auth.test failed (no bot_user_id resolved), don't strip anything —
    falling through with all mentions intact is safer than aggressive
    stripping that hides user_ids from the LLM."""
    from src.handlers.message import _strip_bot_mention

    raw = "<@U0XYZ01> hello"
    assert _strip_bot_mention(raw, "") == "<@U0XYZ01> hello"


def test_process_pre_warms_mentioned_user_ids(app_module, monkeypatch):
    """Regression for the 2026-05-08 incident: when the message text
    carries `<@U-OTHER>` mentions, the handler must pre-warm their display
    names so fetch_user_profile resolves on the first attempt — even if
    the LLM forgets to call fetch_thread_history first."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=[],
        allowed_user_ids=[],
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _NullMetadata())
    monkeypatch.setattr(_runtime, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(_runtime, "_get_llm", lambda: object())
    monkeypatch.setattr(_runtime, "_get_memory", lambda: _StubMem())
    monkeypatch.setattr(
        _runtime, "_get_bot_user_id", lambda *_a, **_kw: "U0BOT01"
    )

    warmed: list[set[str]] = []

    class _CapturingCache:
        def warm(self, _client, ids):
            warmed.append(set(ids))

        def get(self, _client, _uid):
            return ""

    monkeypatch.setattr(_message, "user_name_cache", _CapturingCache())
    monkeypatch.setattr(_message, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(_message, "SlackMentionAgent", _CapturingAgent)
    monkeypatch.setattr(_message, "StreamingMessage", _StubStream)

    _message._process(
        {
            "channel": "C1",
            "ts": "1.1",
            "text": "<@U0BOT01> <@U0UNO01> 프로필 그려",
            "user": "U0BRUCE0",
            "client_msg_id": "msg-warm-1",
        },
        client=_StubClient(),
        say=lambda **kw: None,
        is_dm=False,
        api_app_id="A1",
    )

    # The bot's own id is excluded; only the co-mentioned user is warmed.
    assert warmed == [{"U0UNO01"}]
    # And the bot's mention has been stripped from what reached the agent,
    # while the other mention is preserved so the LLM can pass it through.
    captured_text = _CapturingAgent.captured["context"].event.get("text")
    # The event passed to the agent is unchanged (raw); the LLM-facing
    # `user_message` is the parsed text. We don't have direct access here
    # but the captured agent kwargs include the raw event for ToolContext.
    assert "<@U0UNO01>" in captured_text
    assert "<@U0BOT01>" in captured_text  # event.text untouched; only `user_message` is stripped


class _StubMem:
    def get(self, _u):
        return []


# --------------------------------------------------------------------------- #
# User memory — auto-loaded by user_id and surfaced in the agent prompt
# --------------------------------------------------------------------------- #


def test_process_loads_user_memory_and_passes_to_agent(app_module, monkeypatch):
    """Per-user memory must be fetched at agent construction (one call per
    request, NOT inside the agent loop) and passed through as
    `user_memory=[...]` so the agent can render it in the system prompt."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=[],
        allowed_user_ids=[],
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _NullMetadata())
    monkeypatch.setattr(_runtime, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(_runtime, "_get_llm", lambda: object())

    fetch_calls: list[str] = []

    class _MemStub:
        def get(self, user_id):
            fetch_calls.append(user_id)
            return [{"key": "company", "value": "Daangn", "ts": 1700000000}]

    monkeypatch.setattr(_runtime, "_get_memory", lambda: _MemStub())
    monkeypatch.setattr(_message, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(_message, "user_name_cache", _StubUserNameCache())
    monkeypatch.setattr(_message, "StreamingMessage", _StubStream)
    monkeypatch.setattr(_message, "SlackMentionAgent", _CapturingAgent)

    _message._process(
        {"channel": "C1", "ts": "1.1", "text": "hi", "user": "U-MEMORY", "client_msg_id": "msg-mem-1"},
        client=_StubClient(),
        say=lambda **kw: None,
        is_dm=False,
    )

    # Memory was fetched exactly once with the event's user_id (not, say, the
    # api_app_id) — per-user scoping.
    assert fetch_calls == ["U-MEMORY"]
    captured = _CapturingAgent.captured
    assert captured["user_memory"] == [
        {"key": "company", "value": "Daangn", "ts": 1700000000}
    ]
    # ToolContext also carries user_id so the `remember`/`forget` tools
    # can write under the right key without re-parsing the event.
    assert captured["context"].user_id == "U-MEMORY"


def test_process_continues_when_memory_load_fails(app_module, monkeypatch):
    """A DDB outage on memory read must NOT block the agent run — empty
    memory is the graceful fallback."""
    import dataclasses

    override = dataclasses.replace(
        _runtime.settings,
        allowed_channel_ids=[],
        allowed_user_ids=[],
    )
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _NullMetadata())
    monkeypatch.setattr(_runtime, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(_runtime, "_get_llm", lambda: object())

    class _BrokenMem:
        def get(self, _user_id):
            raise RuntimeError("DDB unreachable")

    monkeypatch.setattr(_runtime, "_get_memory", lambda: _BrokenMem())
    monkeypatch.setattr(_message, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(_message, "user_name_cache", _StubUserNameCache())
    monkeypatch.setattr(_message, "StreamingMessage", _StubStream)
    monkeypatch.setattr(_message, "SlackMentionAgent", _CapturingAgent)

    _message._process(
        {"channel": "C1", "ts": "1.1", "text": "hi", "user": "U-MEM", "client_msg_id": "msg-mem-2"},
        client=_StubClient(),
        say=lambda **kw: None,
        is_dm=False,
    )

    # Empty memory — the agent still ran (CapturingAgent.captured is set).
    assert _CapturingAgent.captured["user_memory"] == []


# --------------------------------------------------------------------------- #
# reaction_added — bot self-delete on :x: from authorized reactor
# --------------------------------------------------------------------------- #


