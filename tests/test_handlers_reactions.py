"""Tests for src.handlers.reactions — _process_reaction dispatcher and
_handle_reaction_x_delete authorization model."""
import pytest

from src import router as _router
from src import runtime as _runtime
from src.handlers import message as _message
from src.handlers import reactions as _reactions

from tests._helpers import _FakeCreds, _FakeDedup, _NullMetadata


@pytest.fixture
def app_module():
    """Import the app module fresh."""
    import app

    return app


class _RecordingMetadata:
    """AppMetadataStore stand-in that returns a configurable row from record()."""

    def __init__(self, row=None):
        self._row = row

    def record(self, *_args, **_kwargs):
        return self._row


def _reset_bot_user_id_cache(app_module):
    _runtime._bot_user_ids.clear()


def test_get_bot_user_id_caches_per_app(app_module, monkeypatch):
    """auth.test should be called once per api_app_id then cached."""
    _reset_bot_user_id_cache(app_module)
    calls = []

    class FakeClient:
        def auth_test(self):
            calls.append(1)
            return {"user_id": "U-BOT-A1"}

    client = FakeClient()
    assert _runtime._get_bot_user_id(client, "A1") == "U-BOT-A1"
    assert _runtime._get_bot_user_id(client, "A1") == "U-BOT-A1"
    assert len(calls) == 1
    # A different api_app_id triggers a fresh lookup (different cache key).
    assert _runtime._get_bot_user_id(client, "A2") == "U-BOT-A1"
    assert len(calls) == 2


def test_get_bot_user_id_returns_empty_on_auth_test_failure(app_module, monkeypatch):
    """If auth.test raises, _get_bot_user_id must NOT raise — it returns ""
    and the caller short-circuits the delete flow."""
    _reset_bot_user_id_cache(app_module)

    class FakeClient:
        def auth_test(self):
            raise RuntimeError("network down")

    assert _runtime._get_bot_user_id(FakeClient(), "A1") == ""
    # Failure must NOT poison the cache — a later success should populate it.
    class FakeClient2:
        def auth_test(self):
            return {"user_id": "U-BOT"}

    assert _runtime._get_bot_user_id(FakeClient2(), "A1") == "U-BOT"


def test_process_worker_routes_reaction_event_to_process_reaction(app_module, monkeypatch):
    """A reaction_added event in the worker payload must call _process_reaction
    instead of the message-path _process."""
    from src.credentials import SlackAppCredentials

    monkeypatch.setattr(
        _runtime,
        "_get_credentials",
        lambda: _FakeCreds({"A1": SlackAppCredentials(signing_secret="s", bot_token="xoxb-tok")}),
    )

    class FakeWeb:
        def __init__(self, token):
            self.token = token

    monkeypatch.setattr(_router, "WebClient", FakeWeb)

    captured = {}

    def fake_reaction(event, client, api_app_id):
        captured["event"] = event
        captured["client"] = client
        captured["api_app_id"] = api_app_id

    def boom_process(*_a, **_kw):
        raise AssertionError("_process must not run for reaction_added events")

    monkeypatch.setattr(_reactions, "_process_reaction", fake_reaction)
    monkeypatch.setattr(_message, "_process", boom_process)

    payload = {
        "slack_event": {
            "type": "reaction_added",
            "reaction": "x",
            "user": "U-REACTOR",
            "item": {"type": "message", "channel": "C1", "ts": "1700000000.000100"},
            "item_user": "U-BOT",
            "event_ts": "1700000001.000200",
        },
        "is_dm": False,
        "api_app_id": "A1",
    }
    _router._process_worker(payload)

    assert captured["event"]["type"] == "reaction_added"
    assert captured["api_app_id"] == "A1"
    assert isinstance(captured["client"], FakeWeb)
    assert captured["client"].token == "xoxb-tok"


class _RecordingClient:
    """WebClient stand-in for reaction tests.

    Models the two-step asker lookup the handler does:
      1. conversations_history(latest=msg_ts, oldest=msg_ts) → bot
         message with `thread_ts` field pointing at the parent
      2. conversations_replies(ts=parent_ts) → parent message whose
         `user` is the original asker

    Knobs:
      - bot_user_id          : auth.test().user_id
      - thread_parent_user   : asker user_id returned by step 2
                               (None ⇒ replies returns empty messages)
      - parent_ts            : the bot message's thread_ts field (defaults
                               to a different-from-msg-ts value so the
                               handler's two-step lookup actually runs)
      - history_raises       : conversations_history raises
      - replies_raises       : conversations_replies raises
      - delete_raises        : chat_delete raises
    """

    def __init__(
        self,
        bot_user_id="U-BOT",
        thread_parent_user="U-ASKER",
        parent_ts="1700000000.000000",
        history_raises=False,
        replies_raises=False,
        delete_raises=False,
    ):
        self.bot_user_id = bot_user_id
        self.thread_parent_user = thread_parent_user
        self.parent_ts = parent_ts
        self.history_raises = history_raises
        self.replies_raises = replies_raises
        self.delete_raises = delete_raises
        self.deleted = []
        self.history_calls = []
        self.replies_calls = []

    def auth_test(self):
        return {"user_id": self.bot_user_id}

    def conversations_history(self, channel, latest, inclusive, limit):
        self.history_calls.append({"channel": channel, "ts": latest})
        if self.history_raises:
            raise RuntimeError("missing_scope")
        # The bot message: ts is the message itself, thread_ts is the parent.
        return {"messages": [{"ts": latest, "user": self.bot_user_id, "thread_ts": self.parent_ts}]}

    def conversations_replies(self, channel, ts, limit=1):
        self.replies_calls.append({"channel": channel, "ts": ts, "limit": limit})
        if self.replies_raises:
            raise RuntimeError("missing_scope")
        if self.thread_parent_user is None:
            return {"messages": []}
        return {"messages": [{"user": self.thread_parent_user, "ts": ts}]}

    def chat_delete(self, channel, ts):
        if self.delete_raises:
            raise RuntimeError("cant_delete_message")
        self.deleted.append({"channel": channel, "ts": ts})


def _reaction_event(reaction="x", item_type="message", user="U-REACTOR", item_user="U-BOT", channel="C1", ts="1700000000.000100", event_ts="1700000001.000200"):
    return {
        "type": "reaction_added",
        "reaction": reaction,
        "user": user,
        "item": {"type": item_type, "channel": channel, "ts": ts},
        "item_user": item_user,
        "event_ts": event_ts,
    }


def test_process_reaction_skips_non_x_emoji(app_module, monkeypatch):
    """Defense-in-depth: even if a non-:x: event reaches the worker, no delete."""
    _reset_bot_user_id_cache(app_module)
    client = _RecordingClient()
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    _reactions._process_reaction(_reaction_event(reaction="thumbsup"), client, api_app_id="A1")

    assert client.deleted == []
    assert client.replies_calls == []


def test_process_reaction_skips_non_message_item(app_module, monkeypatch):
    """Reactions on files/file_comments must not trigger a chat.delete attempt."""
    _reset_bot_user_id_cache(app_module)
    client = _RecordingClient()
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    _reactions._process_reaction(_reaction_event(item_type="file"), client, api_app_id="A1")

    assert client.deleted == []


def test_process_reaction_skips_when_target_not_bot_message(app_module, monkeypatch):
    """item_user disagreeing with auth.test().user_id means the message wasn't
    posted by THIS bot — short-circuit before the chat.delete that would 403."""
    _reset_bot_user_id_cache(app_module)
    client = _RecordingClient(bot_user_id="U-BOT")
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    event = _reaction_event(item_user="U-OTHER-BOT")
    _reactions._process_reaction(event, client, api_app_id="A1")

    assert client.deleted == []
    # We never even hit conversations.replies on the non-bot-message path.
    assert client.replies_calls == []


def test_process_reaction_deletes_when_reactor_is_original_asker(app_module, monkeypatch):
    """The user who started the thread the bot replied in is always allowed
    to delete bot messages in that thread, regardless of ALLOWED_USER_IDS."""
    import dataclasses

    _reset_bot_user_id_cache(app_module)
    override = dataclasses.replace(_runtime.settings, allowed_user_ids=[])
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    client = _RecordingClient(thread_parent_user="U-REACTOR")  # asker == reactor
    event = _reaction_event(user="U-REACTOR")
    _reactions._process_reaction(event, client, api_app_id="A1")

    assert client.deleted == [{"channel": "C1", "ts": "1700000000.000100"}]


def test_process_reaction_deletes_when_reactor_is_in_allowed_users(app_module, monkeypatch):
    """A reactor in ALLOWED_USER_IDS can delete even when they're not the
    original asker — operators get an override path."""
    import dataclasses

    _reset_bot_user_id_cache(app_module)
    override = dataclasses.replace(_runtime.settings, allowed_user_ids=["U-OPS", "U-OTHER"])
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    client = _RecordingClient(thread_parent_user="U-SOMEONE-ELSE")
    event = _reaction_event(user="U-OPS")
    _reactions._process_reaction(event, client, api_app_id="A1")

    assert client.deleted == [{"channel": "C1", "ts": "1700000000.000100"}]


def test_process_reaction_per_app_allowed_users_overrides_global(app_module, monkeypatch):
    """Per-app ALLOWED_USER_IDS attribute (PRESENT) takes precedence over
    the global env var for the reaction flow."""
    import dataclasses

    _reset_bot_user_id_cache(app_module)
    override = dataclasses.replace(_runtime.settings, allowed_user_ids=["U-GLOBAL-OPS"])
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    # Per-app row says only U-APP-OPS may delete; the global U-GLOBAL-OPS
    # is ignored for THIS app.
    monkeypatch.setattr(
        _runtime,
        "_get_app_metadata",
        lambda: _RecordingMetadata({_reactions.ALLOWED_USER_IDS_ATTR: ["U-APP-OPS"]}),
    )

    client = _RecordingClient(thread_parent_user="U-SOMEONE-ELSE")

    # Global ops user is NOT in per-app allowlist → no delete.
    _reactions._process_reaction(_reaction_event(user="U-GLOBAL-OPS", event_ts="1.1"), client, api_app_id="A1")
    assert client.deleted == []

    # Per-app ops user IS allowed → delete.
    _reactions._process_reaction(_reaction_event(user="U-APP-OPS", event_ts="1.2"), client, api_app_id="A1")
    assert client.deleted == [{"channel": "C1", "ts": "1700000000.000100"}]


def test_process_reaction_unauthorized_reactor_is_noop(app_module, monkeypatch):
    """A reactor who is neither the original asker nor in ALLOWED_USER_IDS
    must not trigger a delete."""
    import dataclasses

    _reset_bot_user_id_cache(app_module)
    override = dataclasses.replace(_runtime.settings, allowed_user_ids=["U-OPS"])
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    client = _RecordingClient(thread_parent_user="U-ASKER")
    event = _reaction_event(user="U-RANDOM")
    _reactions._process_reaction(event, client, api_app_id="A1")

    assert client.deleted == []


def test_process_reaction_dedup_drops_duplicate(app_module, monkeypatch):
    """A second delivery of the same reaction event must be a no-op."""
    import dataclasses

    _reset_bot_user_id_cache(app_module)
    override = dataclasses.replace(_runtime.settings, allowed_user_ids=[])
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    class _OneShotDedup:
        def __init__(self):
            self.seen = set()
            self.done_keys: set[str] = set()

        def reserve(self, key, user="system"):
            if key in self.seen:
                return False
            self.seen.add(key)
            return True

        def is_done(self, key):
            return key in self.done_keys

        def mark_done(self, key, user="system"):
            self.done_keys.add(key)

        def count_user_active(self, user):
            return 0

    dedup = _OneShotDedup()
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: dedup)

    client = _RecordingClient(thread_parent_user="U-REACTOR")
    event = _reaction_event(user="U-REACTOR")

    _reactions._process_reaction(event, client, api_app_id="A1")
    _reactions._process_reaction(event, client, api_app_id="A1")

    # Second call is dedup-suppressed before chat.delete.
    assert client.deleted == [{"channel": "C1", "ts": "1700000000.000100"}]


def test_process_reaction_chat_delete_failure_logged_not_raised(app_module, monkeypatch):
    """A chat.delete that 403s (e.g. Slack revoked permission, message
    already gone) must not propagate — log + move on."""
    import dataclasses

    _reset_bot_user_id_cache(app_module)
    override = dataclasses.replace(_runtime.settings, allowed_user_ids=[])
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    client = _RecordingClient(thread_parent_user="U-REACTOR", delete_raises=True)
    event = _reaction_event(user="U-REACTOR")

    # Should NOT raise — handler swallows and logs.
    _reactions._process_reaction(event, client, api_app_id="A1")


def test_process_reaction_two_step_lookup_finds_thread_root_asker(app_module, monkeypatch):
    """The bot replies inside a thread, so reactions land on a thread reply.
    `conversations.replies(ts=reply_ts)` doesn't return the parent — Slack
    only honors thread-root ts. The handler must:
      1. conversations.history(latest=msg_ts, oldest=msg_ts) → bot message
         with `thread_ts` field pointing at the parent
      2. conversations.replies(ts=parent_ts, limit=1) → the parent message
         whose `user` is the asker
    Verify both calls happen and reactor=asker → delete fires."""
    import dataclasses

    _reset_bot_user_id_cache(app_module)
    override = dataclasses.replace(_runtime.settings, allowed_user_ids=[])
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    client = _RecordingClient(
        thread_parent_user="U-REACTOR",
        parent_ts="1699999999.000000",  # parent ts ≠ message_ts
    )
    event = _reaction_event(user="U-REACTOR")
    _reactions._process_reaction(event, client, api_app_id="A1")

    # Step 1: history fetched the bot message itself (latest == message_ts)
    assert client.history_calls == [{"channel": "C1", "ts": "1700000000.000100"}]
    # Step 2: replies fetched with the PARENT ts from history, NOT message_ts
    assert client.replies_calls == [{"channel": "C1", "ts": "1699999999.000000", "limit": 1}]
    # Asker matched → deleted
    assert client.deleted == [{"channel": "C1", "ts": "1700000000.000100"}]


def test_process_reaction_history_failure_falls_back_to_allowlist(app_module, monkeypatch):
    """conversations.history failure (missing scope) should not abort —
    the ALLOWED_USER_IDS check still runs. Mirror behavior for the
    replies failure case."""
    import dataclasses

    _reset_bot_user_id_cache(app_module)
    override = dataclasses.replace(_runtime.settings, allowed_user_ids=["U-OPS"])
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    client = _RecordingClient(history_raises=True)
    # U-OPS is in allowlist → still allowed even when history lookup is unavailable.
    _reactions._process_reaction(_reaction_event(user="U-OPS", event_ts="3.1"), client, api_app_id="A1")
    assert client.deleted == [{"channel": "C1", "ts": "1700000000.000100"}]
    # No replies call since history failed before we knew the parent_ts.
    assert client.replies_calls == []


def test_process_reaction_replies_failure_falls_back_to_allowlist(app_module, monkeypatch):
    """If conversations.replies fails (missing scope, network), the original-
    asker check is unavailable — but ALLOWED_USER_IDS check must still apply."""
    import dataclasses

    _reset_bot_user_id_cache(app_module)
    override = dataclasses.replace(_runtime.settings, allowed_user_ids=["U-OPS"])
    monkeypatch.setattr(_runtime, "settings", override)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    client = _RecordingClient(replies_raises=True)
    # U-OPS is in allowlist → still allowed even without replies.
    _reactions._process_reaction(_reaction_event(user="U-OPS", event_ts="1.1"), client, api_app_id="A1")
    assert client.deleted == [{"channel": "C1", "ts": "1700000000.000100"}]

    # U-RANDOM is not in allowlist AND we couldn't verify they're the asker → no delete.
    client.deleted.clear()
    _reactions._process_reaction(_reaction_event(user="U-RANDOM", event_ts="1.2"), client, api_app_id="A1")
    assert client.deleted == []


def test_reaction_dispatch_to_registered_handler(app_module, monkeypatch):
    """REACTION_HANDLERS dispatches to the registered handler for a reaction
    name; unregistered reactions are dropped silently. Used to extend the
    bot with new reaction behaviors without touching the dispatcher."""
    _reset_bot_user_id_cache(app_module)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    calls = []

    def fake_handler(event, client, api_app_id):
        calls.append({"reaction": event.get("reaction"), "api_app_id": api_app_id})

    # Register a temporary reaction → handler mapping for the test.
    monkeypatch.setitem(_reactions.REACTION_HANDLERS, "thumbsup", fake_handler)

    client = _RecordingClient()

    # Registered reaction → handler called.
    _reactions._process_reaction(
        _reaction_event(reaction="thumbsup", event_ts="2.1"), client, api_app_id="A1"
    )
    assert calls == [{"reaction": "thumbsup", "api_app_id": "A1"}]

    # Unregistered reaction → silently dropped, handler NOT called.
    _reactions._process_reaction(
        _reaction_event(reaction="rocket", event_ts="2.2"), client, api_app_id="A1"
    )
    assert len(calls) == 1


def test_on_reaction_added_handler_pre_filters_non_x_at_receiver(app_module, monkeypatch):
    """The Bolt receiver handler must drop non-:x: reactions before they
    cost a Lambda async invoke. Cuts cost; the worker re-checks anyway."""
    enqueued = []
    monkeypatch.setattr(
        _router,
        "_enqueue_worker",
        lambda event, is_dm, api_app_id: enqueued.append((event, is_dm, api_app_id)),
    )

    bolt_app = _router._get_bolt_app("A-test-react", "sig", "tok")
    handler = next(
        l.ack_function
        for l in bolt_app._listeners
        if getattr(l.ack_function, "__name__", "") == "_on_reaction_added"
    )

    def fake_ack():
        pass

    handler(
        event={"reaction": "thumbsup", "item": {"type": "message"}},
        body={"api_app_id": "A1"},
        ack=fake_ack,
    )
    assert enqueued == []

    handler(
        event={"reaction": "x", "item": {"type": "file"}},
        body={"api_app_id": "A1"},
        ack=fake_ack,
    )
    assert enqueued == []

    handler(
        event={"reaction": "x", "item": {"type": "message", "channel": "C1", "ts": "1.1"}},
        body={"api_app_id": "A1"},
        ack=fake_ack,
    )
    assert len(enqueued) == 1
    assert enqueued[0][2] == "A1"


# --------------------------------------------------------------------------- #
# :img-gpt: / :img-xai: — image generation reactions
# --------------------------------------------------------------------------- #


class _ImageReactionClient:
    """Slack client stand-in for image-gen reaction tests.

    Captures `files_upload_v2` and `chat_postEphemeral` calls and serves
    a configurable `conversations_history` response so we can flex the
    'text/thread_ts on the reacted message' scenarios.
    """

    def __init__(
        self,
        text: str = "사과 한 알 그려줘",
        thread_ts: str | None = None,
        history_raises: bool = False,
        upload_raises: bool = False,
    ):
        self.text = text
        self.thread_ts = thread_ts
        self.history_raises = history_raises
        self.upload_raises = upload_raises
        self.history_calls: list[dict] = []
        self.uploads: list[dict] = []
        self.ephemerals: list[dict] = []

    def conversations_history(self, channel, latest, inclusive, limit):
        self.history_calls.append(
            {"channel": channel, "latest": latest, "inclusive": inclusive, "limit": limit}
        )
        if self.history_raises:
            raise RuntimeError("missing_scope")
        msg = {"ts": latest, "user": "U-AUTHOR", "text": self.text}
        if self.thread_ts is not None:
            msg["thread_ts"] = self.thread_ts
        return {"messages": [msg]}

    def files_upload_v2(self, **kwargs):
        if self.upload_raises:
            raise RuntimeError("file_upload_failed")
        self.uploads.append(kwargs)
        return {"file": {"permalink": "https://example.com/f"}}

    def chat_postEphemeral(self, **kwargs):
        self.ephemerals.append(kwargs)


def _img_reaction_event(reaction: str, channel: str = "C1", ts: str = "1700000000.000100"):
    return {
        "type": "reaction_added",
        "reaction": reaction,
        "user": "U-REACTOR",
        "item": {"type": "message", "channel": channel, "ts": ts},
        "item_user": "U-AUTHOR",
        "event_ts": "1700000001.000200",
    }


def _stub_get_llm(monkeypatch, image_bytes: bytes = b"PNGDATA"):
    """Spy on `get_llm` and return a fake LLM whose generate_image yields fixed bytes."""
    captured: dict = {}

    class _LLM:
        def generate_image(self, prompt: str) -> bytes:
            captured["prompt"] = prompt
            return image_bytes

    def spy(**kwargs):
        captured.update(kwargs)
        return _LLM()

    monkeypatch.setattr(_reactions, "get_llm", spy)
    return captured


def _settings_with_image_models(monkeypatch, **overrides):
    import dataclasses

    monkeypatch.setattr(_runtime, "settings", dataclasses.replace(_runtime.settings, **overrides))


def test_reaction_img_xai_uses_xai_provider_and_image_model_xai(app_module, monkeypatch):
    _settings_with_image_models(monkeypatch, image_model_xai="grok-imagine-image-quality")
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    captured = _stub_get_llm(monkeypatch, image_bytes=b"xai-bytes")

    client = _ImageReactionClient(text="사과 한 알", thread_ts=None)
    _reactions._process_reaction(
        _img_reaction_event("img-xai"), client, api_app_id="A1"
    )

    assert captured["image_provider"] == "xai"
    assert captured["image_model"] == "grok-imagine-image-quality"
    assert captured["prompt"] == "사과 한 알"

    assert len(client.uploads) == 1
    upload = client.uploads[0]
    assert upload["channel"] == "C1"
    assert upload["file"] == b"xai-bytes"
    # No parent thread on the reacted message → use the message's own ts.
    assert upload["thread_ts"] == "1700000000.000100"
    assert "사과 한 알" in upload["initial_comment"]
    assert ":img-xai:" in upload["initial_comment"]
    assert client.ephemerals == []


def test_reaction_img_gpt_uses_openai_provider_and_image_model_gpt(app_module, monkeypatch):
    _settings_with_image_models(monkeypatch, image_model_gpt="gpt-image-2")
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    captured = _stub_get_llm(monkeypatch, image_bytes=b"gpt-bytes")

    client = _ImageReactionClient(text="blue sky")
    _reactions._process_reaction(
        _img_reaction_event("img-gpt"), client, api_app_id="A1"
    )

    assert captured["image_provider"] == "openai"
    assert captured["image_model"] == "gpt-image-2"
    assert client.uploads[0]["file"] == b"gpt-bytes"


def test_reaction_uses_parent_thread_when_reacted_message_is_thread_reply(
    app_module, monkeypatch
):
    """If the reacted message is itself a thread reply (has `thread_ts`),
    we must post the result into the same thread root — Slack doesn't
    allow nested threads, and posting to the reply's own ts would
    silently flatten to channel-level on some clients."""
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    _stub_get_llm(monkeypatch)

    client = _ImageReactionClient(text="prompt", thread_ts="1699999999.000000")
    _reactions._process_reaction(
        _img_reaction_event("img-gpt"), client, api_app_id="A1"
    )

    assert client.uploads[0]["thread_ts"] == "1699999999.000000"


def test_reaction_empty_message_text_notifies_reactor(app_module, monkeypatch):
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    def boom_get_llm(**_kwargs):
        raise AssertionError("get_llm must not run for empty message text")

    monkeypatch.setattr(_reactions, "get_llm", boom_get_llm)

    client = _ImageReactionClient(text="")
    _reactions._process_reaction(
        _img_reaction_event("img-xai"), client, api_app_id="A1"
    )

    assert client.uploads == []
    assert len(client.ephemerals) == 1
    assert client.ephemerals[0]["user"] == "U-REACTOR"
    assert client.ephemerals[0]["channel"] == "C1"
    assert "텍스트" in client.ephemerals[0]["text"]


def test_reaction_history_failure_notifies_reactor(app_module, monkeypatch):
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    def boom_get_llm(**_kwargs):
        raise AssertionError("get_llm must not run when history fails")

    monkeypatch.setattr(_reactions, "get_llm", boom_get_llm)

    client = _ImageReactionClient(history_raises=True)
    _reactions._process_reaction(
        _img_reaction_event("img-xai"), client, api_app_id="A1"
    )

    assert client.uploads == []
    assert len(client.ephemerals) == 1
    assert "메시지" in client.ephemerals[0]["text"]


def test_reaction_image_generation_failure_notifies_reactor(app_module, monkeypatch):
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())

    class _BoomLLM:
        def generate_image(self, prompt):
            raise RuntimeError("provider exploded")

    monkeypatch.setattr(_reactions, "get_llm", lambda **_kwargs: _BoomLLM())

    client = _ImageReactionClient()
    _reactions._process_reaction(
        _img_reaction_event("img-gpt"), client, api_app_id="A1"
    )

    assert client.uploads == []
    assert len(client.ephemerals) == 1
    assert "이미지 생성 실패" in client.ephemerals[0]["text"]


def test_reaction_handlers_dict_wires_both_image_reactions(app_module):
    """Adding entries to REACTION_HANDLERS is what opens the Bolt receiver
    pre-filter for these reactions — if either entry disappears, the
    receiver silently drops them and the worker never runs."""
    assert _reactions.REACTION_HANDLERS["img-gpt"] is _reactions._handle_reaction_image_gen
    assert _reactions.REACTION_HANDLERS["img-xai"] is _reactions._handle_reaction_image_gen


def test_on_reaction_added_handler_admits_image_reactions_at_receiver(
    app_module, monkeypatch
):
    """The Bolt receiver pre-filter must let `img-gpt`/`img-xai` through to
    the worker invoke — otherwise they'd be silently dropped before any
    of the handler logic runs."""
    enqueued = []
    monkeypatch.setattr(
        _router,
        "_enqueue_worker",
        lambda event, is_dm, api_app_id: enqueued.append((event, is_dm, api_app_id)),
    )

    bolt_app = _router._get_bolt_app("A-test-react-img", "sig", "tok")
    handler = next(
        l.ack_function
        for l in bolt_app._listeners
        if getattr(l.ack_function, "__name__", "") == "_on_reaction_added"
    )

    def fake_ack():
        pass

    for reaction in ("img-gpt", "img-xai"):
        handler(
            event={
                "reaction": reaction,
                "item": {"type": "message", "channel": "C1", "ts": "1.1"},
            },
            body={"api_app_id": "A1"},
            ack=fake_ack,
        )
    assert len(enqueued) == 2
    assert {e[0]["reaction"] for e in enqueued} == {"img-gpt", "img-xai"}
