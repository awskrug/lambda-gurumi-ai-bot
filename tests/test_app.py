"""Tests for the receiver / worker / multi-tenant routing in app.lambda_handler.

We exercise the routing + enqueue layer here. `_process` itself is covered
transitively by tests/test_agent.py and friends. All external dependencies
(boto3, slack_sdk.WebClient, Bolt, SSM) are mocked so the tests don't need
real credentials.
"""
import base64
import json

import pytest


@pytest.fixture
def app_module():
    """Import the app module fresh.

    `app.Settings.from_env()` reads env at import time but does not
    validate Slack credentials, so this is safe without setting
    SLACK_BOT_TOKEN.
    """
    import app

    return app


# --------------------------------------------------------------------------- #
# lambda_handler routing
# --------------------------------------------------------------------------- #


def test_lambda_handler_routes_worker_flag_to_process_worker(app_module, monkeypatch):
    """`event["_worker"] is True` must skip Slack / Bolt entirely and call the worker."""
    received = {}

    def fake_worker(payload):
        received["payload"] = payload

    monkeypatch.setattr(app_module, "_process_worker", fake_worker)

    def boom_route(event, context):
        raise AssertionError("_route_request must not be called on the worker path")

    monkeypatch.setattr(app_module, "_route_request", boom_route)

    event = {"_worker": True, "slack_event": {"channel": "C1", "text": "hi"}, "is_dm": False, "api_app_id": "A1"}
    result = app_module.lambda_handler(event, None)

    assert result == {"statusCode": 200, "body": ""}
    assert received["payload"] is event


def test_lambda_handler_short_circuits_slack_retry_without_routing(app_module, monkeypatch):
    """Receiver path: an X-Slack-Retry-Num header means Slack is re-delivering.
    We already dispatched the first try to a worker; swallow the retry."""

    def boom_route(event, context):
        raise AssertionError("_route_request must not be invoked on a retried delivery")

    monkeypatch.setattr(app_module, "_route_request", boom_route)

    event = {"headers": {"X-Slack-Retry-Num": "1"}, "body": "..."}
    assert app_module.lambda_handler(event, None) == {"statusCode": 200, "body": ""}


def test_lambda_handler_short_circuits_slack_retry_lowercase_header(app_module, monkeypatch):
    """API Gateway may lowercase the header name. Our guard normalizes."""
    monkeypatch.setattr(
        app_module,
        "_route_request",
        lambda *_: (_ for _ in ()).throw(AssertionError("should not reach routing")),
    )
    event = {"headers": {"x-slack-retry-num": "3"}}
    assert app_module.lambda_handler(event, None) == {"statusCode": 200, "body": ""}


def test_lambda_handler_delegates_normal_request_to_route(app_module, monkeypatch):
    """A normal Slack HTTP event (no retry header, no _worker flag) must
    reach _route_request."""
    calls = []

    def fake_route(event, context):
        calls.append(("route", event, context))
        return {"statusCode": 200, "body": "routed"}

    monkeypatch.setattr(app_module, "_route_request", fake_route)

    event = {"headers": {"Content-Type": "application/json"}, "body": "{}"}
    result = app_module.lambda_handler(event, "ctx")

    assert result == {"statusCode": 200, "body": "routed"}
    assert calls == [("route", event, "ctx")]


def test_lambda_handler_worker_flag_false_takes_receiver_path(app_module, monkeypatch):
    """`_worker=False` should fall through to the receiver path, not be
    treated as a worker marker."""

    def boom_worker(payload):
        raise AssertionError("_process_worker must not run when _worker is falsy")

    monkeypatch.setattr(app_module, "_process_worker", boom_worker)
    monkeypatch.setattr(app_module, "_route_request", lambda *_: {"statusCode": 202, "body": ""})

    assert app_module.lambda_handler({"_worker": False, "headers": {}}, None) == {"statusCode": 202, "body": ""}


# --------------------------------------------------------------------------- #
# _route_request — multi-tenant dispatch
# --------------------------------------------------------------------------- #


class _FakeCreds:
    def __init__(self, mapping):
        self._map = mapping
        self.calls = []

    def get(self, app_id):
        self.calls.append(app_id)
        return self._map.get(app_id)


def _stub_route_dependencies(monkeypatch, app_module, creds_map):
    """Replace _get_credentials + _get_bolt_app + SlackRequestHandler so
    _route_request can be exercised without touching SSM / Bolt."""
    fake_creds = _FakeCreds(creds_map)
    monkeypatch.setattr(app_module, "_get_credentials", lambda: fake_creds)

    bolt_calls = {}

    def fake_get_bolt(api_app_id, signing_secret, bot_token):
        bolt_calls.setdefault("seen", []).append((api_app_id, signing_secret, bot_token))
        return f"bolt:{api_app_id}"

    monkeypatch.setattr(app_module, "_get_bolt_app", fake_get_bolt)

    handle_calls = []

    class FakeHandler:
        def __init__(self, bolt_app):
            handle_calls.append(("init", bolt_app))

        def handle(self, event, context):
            handle_calls.append(("handle", event, context))
            return {"statusCode": 200, "body": "bolt-handled"}

    monkeypatch.setattr(app_module, "SlackRequestHandler", FakeHandler)
    return fake_creds, bolt_calls, handle_calls


def test_route_request_url_verification_echoes_challenge_without_creds_lookup(app_module, monkeypatch):
    """URL verification has no api_app_id in the body, so we cannot look up
    a signing_secret. Echo the challenge directly — it's a setup-time ping
    with no actionable payload."""
    fake_creds, _, handle_calls = _stub_route_dependencies(monkeypatch, app_module, {})

    body = json.dumps({"type": "url_verification", "challenge": "abc-123"})
    event = {"body": body}

    result = app_module._route_request(event, None)
    assert result == {"statusCode": 200, "body": "abc-123"}
    # Crucially, we did NOT call SSM or Bolt.
    assert fake_creds.calls == []
    assert handle_calls == []


def test_route_request_unparseable_body_returns_400(app_module, monkeypatch):
    fake_creds, _, _ = _stub_route_dependencies(monkeypatch, app_module, {})
    event = {"body": "<<not json>>"}
    assert app_module._route_request(event, None) == {"statusCode": 400, "body": ""}
    assert fake_creds.calls == []


def test_route_request_empty_body_returns_400(app_module, monkeypatch):
    _stub_route_dependencies(monkeypatch, app_module, {})
    assert app_module._route_request({}, None) == {"statusCode": 400, "body": ""}


def test_route_request_missing_api_app_id_returns_200(app_module, monkeypatch):
    """Some Slack request shapes lack api_app_id (e.g. unknown event types).
    We can't route, but we don't want Slack to retry — return 200."""
    fake_creds, _, handle_calls = _stub_route_dependencies(monkeypatch, app_module, {})

    body = json.dumps({"type": "event_callback", "event": {"type": "unknown"}})
    result = app_module._route_request({"body": body}, None)

    assert result == {"statusCode": 200, "body": ""}
    assert fake_creds.calls == []
    assert handle_calls == []


def test_route_request_unknown_app_returns_200_without_dispatch(app_module, monkeypatch):
    """An app that hasn't been provisioned in SSM yet gets a structured
    log + 200. No Bolt invocation (no signing_secret to verify with)."""
    fake_creds, _, handle_calls = _stub_route_dependencies(monkeypatch, app_module, {})

    body = json.dumps({"api_app_id": "A-NEW", "type": "event_callback", "event": {}})
    result = app_module._route_request({"body": body}, None)

    assert result == {"statusCode": 200, "body": ""}
    assert fake_creds.calls == ["A-NEW"]
    assert handle_calls == []


def test_route_request_known_app_dispatches_to_bolt_with_correct_secrets(app_module, monkeypatch):
    """Happy path — both secrets present, Bolt App built per app_id, request
    forwarded into SlackRequestHandler."""
    from src.credentials import SlackAppCredentials

    fake_creds, bolt_calls, handle_calls = _stub_route_dependencies(
        monkeypatch,
        app_module,
        {"A1": SlackAppCredentials(signing_secret="sig-1", bot_token="xoxb-1")},
    )

    body = json.dumps({"api_app_id": "A1", "type": "event_callback", "event": {}})
    event = {"body": body}
    result = app_module._route_request(event, "ctx-x")

    assert result == {"statusCode": 200, "body": "bolt-handled"}
    assert fake_creds.calls == ["A1"]
    assert bolt_calls["seen"] == [("A1", "sig-1", "xoxb-1")]
    assert handle_calls == [("init", "bolt:A1"), ("handle", event, "ctx-x")]


def test_route_request_decodes_base64_body(app_module, monkeypatch):
    """API Gateway delivers binary content types as base64. The parser must
    decode before JSON parsing — otherwise a base64 body looks unparseable."""
    from src.credentials import SlackAppCredentials

    _stub_route_dependencies(
        monkeypatch,
        app_module,
        {"A1": SlackAppCredentials(signing_secret="s", bot_token="t")},
    )

    raw = json.dumps({"api_app_id": "A1", "type": "event_callback"})
    encoded = base64.b64encode(raw.encode()).decode()
    event = {"body": encoded, "isBase64Encoded": True}

    result = app_module._route_request(event, None)
    assert result == {"statusCode": 200, "body": "bolt-handled"}


# --------------------------------------------------------------------------- #
# _get_bolt_app — per-app cache + rotation detection
# --------------------------------------------------------------------------- #


def test_get_bolt_app_caches_per_app_id(app_module, monkeypatch):
    """Same (app_id, signing_secret, bot_token) returns the same App object."""
    monkeypatch.setattr(app_module, "_bolt_apps", {})

    constructed = []

    class FakeApp:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def event(self, _name):
            def deco(fn):
                return fn
            return deco

    monkeypatch.setattr(app_module, "App", FakeApp)

    a1 = app_module._get_bolt_app("A1", "sig", "tok")
    a2 = app_module._get_bolt_app("A1", "sig", "tok")
    assert a1 is a2
    assert len(constructed) == 1


def test_get_bolt_app_isolates_apps(app_module, monkeypatch):
    """Different api_app_ids get different App instances."""
    monkeypatch.setattr(app_module, "_bolt_apps", {})

    constructed = []

    class FakeApp:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def event(self, _name):
            def deco(fn):
                return fn
            return deco

    monkeypatch.setattr(app_module, "App", FakeApp)

    a1 = app_module._get_bolt_app("A1", "sig1", "tok1")
    a2 = app_module._get_bolt_app("A2", "sig2", "tok2")
    assert a1 is not a2
    assert len(constructed) == 2
    # Each App must be constructed with its own credentials.
    tokens = sorted(c["token"] for c in constructed)
    secrets = sorted(c["signing_secret"] for c in constructed)
    assert tokens == ["tok1", "tok2"]
    assert secrets == ["sig1", "sig2"]


def test_get_bolt_app_rebuilds_on_secret_rotation(app_module, monkeypatch):
    """When CredentialsStore returns a different secret tuple after TTL
    refresh, the cached App is replaced — otherwise rotation wouldn't take
    effect until the container died."""
    monkeypatch.setattr(app_module, "_bolt_apps", {})

    constructed = []

    class FakeApp:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def event(self, _name):
            def deco(fn):
                return fn
            return deco

    monkeypatch.setattr(app_module, "App", FakeApp)

    first = app_module._get_bolt_app("A1", "sig-old", "tok-old")
    second = app_module._get_bolt_app("A1", "sig-new", "tok-new")
    assert first is not second
    assert constructed[1]["signing_secret"] == "sig-new"
    assert constructed[1]["token"] == "tok-new"


def test_get_bolt_app_disables_token_verification(app_module, monkeypatch):
    """We don't want Bolt calling auth.test on every cold container × per
    app_id — that's a needless Slack roundtrip on the request path."""
    monkeypatch.setattr(app_module, "_bolt_apps", {})

    constructed = []

    class FakeApp:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def event(self, _name):
            def deco(fn):
                return fn
            return deco

    monkeypatch.setattr(app_module, "App", FakeApp)
    app_module._get_bolt_app("A1", "sig", "tok")
    assert constructed[0].get("token_verification_enabled") is False


# --------------------------------------------------------------------------- #
# _enqueue_worker — receiver → worker bridge with api_app_id propagation
# --------------------------------------------------------------------------- #


def test_enqueue_worker_runs_inline_when_not_in_lambda(app_module, monkeypatch):
    """Without AWS_LAMBDA_FUNCTION_NAME (local dev / tests), the receiver
    must execute the worker path inline."""
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)

    def boom_client():
        raise AssertionError("boto3 must not be touched off-Lambda")

    monkeypatch.setattr(app_module, "_get_lambda_client", boom_client)

    captured = []
    monkeypatch.setattr(app_module, "_process_worker", lambda payload: captured.append(payload))

    event = {"channel": "C1", "text": "hello"}
    app_module._enqueue_worker(event, is_dm=False, api_app_id="A1")

    assert captured == [{"slack_event": event, "is_dm": False, "api_app_id": "A1"}]


def test_enqueue_worker_fires_async_invoke_in_lambda(app_module, monkeypatch):
    """When AWS_LAMBDA_FUNCTION_NAME is set, issue an async Lambda invoke
    with the api_app_id present in the payload — and NOT run inline."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "gurumi-mention")

    invocations = []

    class FakeLambdaClient:
        def invoke(self, **kwargs):
            invocations.append(kwargs)
            return {"StatusCode": 202}

    monkeypatch.setattr(app_module, "_get_lambda_client", lambda: FakeLambdaClient())

    def boom_inline(payload):
        raise AssertionError("inline worker must not run when invoke succeeds")

    monkeypatch.setattr(app_module, "_process_worker", boom_inline)

    event = {"channel": "D1", "text": "안녕", "user": "U1"}
    app_module._enqueue_worker(event, is_dm=True, api_app_id="A-multi")

    assert len(invocations) == 1
    call = invocations[0]
    assert call["FunctionName"] == "gurumi-mention"
    assert call["InvocationType"] == "Event"
    payload = json.loads(call["Payload"].decode("utf-8"))
    assert payload == {
        "_worker": True,
        "slack_event": event,
        "is_dm": True,
        "api_app_id": "A-multi",
    }


def test_enqueue_worker_does_not_ship_secrets_in_payload(app_module, monkeypatch):
    """Defense-in-depth: the async invoke payload must NEVER carry tokens.
    Lambda invoke payloads can be visible in CloudTrail; secrets stay in SSM."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "gurumi-mention")

    invocations = []

    class FakeLambdaClient:
        def invoke(self, **kwargs):
            invocations.append(kwargs)

    monkeypatch.setattr(app_module, "_get_lambda_client", lambda: FakeLambdaClient())
    monkeypatch.setattr(app_module, "_process_worker", lambda _p: None)

    app_module._enqueue_worker({"text": "hi"}, is_dm=False, api_app_id="A1")
    payload_bytes = invocations[0]["Payload"]
    assert b"xoxb" not in payload_bytes
    assert b"signing_secret" not in payload_bytes
    assert b"bot_token" not in payload_bytes


def test_enqueue_worker_falls_back_to_inline_on_invoke_failure(app_module, monkeypatch):
    """If boto3.invoke raises, fall back to inline execution — still with
    api_app_id in the payload so the worker can resolve credentials."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "gurumi-mention")

    class BrokenClient:
        def invoke(self, **_kwargs):
            raise RuntimeError("network unreachable")

    monkeypatch.setattr(app_module, "_get_lambda_client", lambda: BrokenClient())

    captured = []
    monkeypatch.setattr(app_module, "_process_worker", lambda payload: captured.append(payload))

    event = {"channel": "C1", "text": "hi"}
    app_module._enqueue_worker(event, is_dm=False, api_app_id="A1")

    assert captured == [{"slack_event": event, "is_dm": False, "api_app_id": "A1"}]


def test_enqueue_worker_payload_preserves_non_ascii(app_module, monkeypatch):
    """Korean / emoji in the Slack event must survive the JSON round-trip."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "gurumi-mention")

    invocations = []

    class FakeClient:
        def invoke(self, **kwargs):
            invocations.append(kwargs)

    monkeypatch.setattr(app_module, "_get_lambda_client", lambda: FakeClient())
    monkeypatch.setattr(app_module, "_process_worker", lambda _p: None)

    event = {"text": "이미지 그려줘 🎨", "channel": "C1"}
    app_module._enqueue_worker(event, is_dm=False, api_app_id="A1")

    payload = json.loads(invocations[0]["Payload"].decode("utf-8"))
    assert payload["slack_event"]["text"] == "이미지 그려줘 🎨"


# --------------------------------------------------------------------------- #
# _process_worker — async entrypoint resolves credentials via SSM
# --------------------------------------------------------------------------- #


def test_process_worker_no_api_app_id_is_noop(app_module, monkeypatch):
    """Defensive: a malformed payload without api_app_id can't be routed,
    so we drop it instead of crashing."""

    def boom_creds():
        raise AssertionError("credentials lookup must not run without api_app_id")

    monkeypatch.setattr(app_module, "_get_credentials", boom_creds)

    def boom_process(*args, **kwargs):
        raise AssertionError("_process must not run without credentials")

    monkeypatch.setattr(app_module, "_process", boom_process)
    # Should not raise.
    app_module._process_worker({"slack_event": {"channel": "C1", "text": "hi"}})


def test_process_worker_unknown_app_skips_processing(app_module, monkeypatch):
    """If an app's secrets disappear between receiver and worker (race with
    operator deletion), drop the work — there's no token to reply with."""
    monkeypatch.setattr(app_module, "_get_credentials", lambda: _FakeCreds({}))

    def boom_process(*args, **kwargs):
        raise AssertionError("_process must not run without credentials")

    monkeypatch.setattr(app_module, "_process", boom_process)

    app_module._process_worker(
        {"slack_event": {"channel": "C1", "text": "hi"}, "is_dm": False, "api_app_id": "A-gone"}
    )


def test_process_worker_builds_webclient_from_ssm_bot_token(app_module, monkeypatch):
    """The worker must mint a fresh WebClient using the bot_token resolved
    from SSM for the carried api_app_id — not from any static settings field."""
    from src.credentials import SlackAppCredentials

    monkeypatch.setattr(
        app_module,
        "_get_credentials",
        lambda: _FakeCreds({"A1": SlackAppCredentials(signing_secret="s", bot_token="xoxb-from-ssm")}),
    )

    created = []

    class FakeWeb:
        def __init__(self, token):
            created.append(token)
            self.token = token

    monkeypatch.setattr(app_module, "WebClient", FakeWeb)

    captured = {}

    def fake_process(event, client, say, is_dm, api_app_id=""):
        captured["event"] = event
        captured["client"] = client
        captured["say"] = say
        captured["is_dm"] = is_dm
        captured["api_app_id"] = api_app_id

    monkeypatch.setattr(app_module, "_process", fake_process)

    payload = {"slack_event": {"channel": "C1", "text": "hi"}, "is_dm": True, "api_app_id": "A1"}
    app_module._process_worker(payload)

    assert created == ["xoxb-from-ssm"]
    assert captured["event"] == {"channel": "C1", "text": "hi"}
    assert captured["is_dm"] is True
    assert captured["api_app_id"] == "A1"
    assert isinstance(captured["client"], FakeWeb)
    assert callable(captured["say"])


def test_process_worker_say_callable_posts_to_event_channel(app_module, monkeypatch):
    """The `say` closure passed to _process must post to the channel the
    original Slack event came from — not some default — and must forward
    thread_ts when provided."""
    from src.credentials import SlackAppCredentials

    monkeypatch.setattr(
        app_module,
        "_get_credentials",
        lambda: _FakeCreds({"A1": SlackAppCredentials(signing_secret="s", bot_token="t")}),
    )

    posts = []

    class FakeWeb:
        def __init__(self, token):
            pass

        def chat_postMessage(self, **kwargs):
            posts.append(kwargs)

    monkeypatch.setattr(app_module, "WebClient", FakeWeb)

    captured_say = {}

    def fake_process(event, client, say, is_dm, api_app_id=""):
        captured_say["fn"] = say

    monkeypatch.setattr(app_module, "_process", fake_process)

    payload = {"slack_event": {"channel": "C-origin", "text": "x"}, "is_dm": False, "api_app_id": "A1"}
    app_module._process_worker(payload)

    say = captured_say["fn"]
    say("hello world")
    say("threaded reply", thread_ts="1700000000.000100")

    assert posts == [
        {"channel": "C-origin", "text": "hello world"},
        {"channel": "C-origin", "text": "threaded reply", "thread_ts": "1700000000.000100"},
    ]


# --------------------------------------------------------------------------- #
# Channel allowlist — block reply with first-channel substitution
# --------------------------------------------------------------------------- #


class _FakeDedup:
    """Minimal DedupStore stand-in: reserve always succeeds, no throttle."""

    def reserve(self, key, user="system"):
        return True

    def count_user_active(self, user):
        return 0


class _NullMetadata:
    """No-op AppMetadataStore for tests that pass api_app_id but don't care
    about the registry side-effect."""

    def record(self, *_args, **_kwargs):
        pass


def test_process_blocked_channel_substitutes_first_allowed_channel(app_module, monkeypatch):
    """비허용 채널 응답의 `{}` 는 ALLOWED_CHANNEL_IDS 의 첫 번째 채널로 치환되며,
    Slack 채널 멘션 형식(`<#ID>`)으로 감싸 클릭 가능한 링크로 렌더되어야 한다."""
    import dataclasses

    override = dataclasses.replace(
        app_module.settings,
        allowed_channel_ids=["C04PPA399CP", "C08A9550X"],
        allowed_channel_message="구루미에게 질문은 {} 채널을 이용해 주세요~",
    )
    monkeypatch.setattr(app_module, "settings", override)
    monkeypatch.setattr(app_module, "_get_dedup", lambda: _FakeDedup())

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
    app_module._process(event, client=object(), say=fake_say, is_dm=False)

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
        app_module.settings,
        allowed_channel_ids=["C04PPA399CP"],
        allowed_channel_message="허용되지 않은 채널입니다.",
    )
    monkeypatch.setattr(app_module, "settings", override)
    monkeypatch.setattr(app_module, "_get_dedup", lambda: _FakeDedup())

    posts = []
    app_module._process(
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
        app_module.settings,
        allowed_channel_ids=["C04PPA399CP"],
        allowed_channel_message="",
    )
    monkeypatch.setattr(app_module, "settings", override)
    monkeypatch.setattr(app_module, "_get_dedup", lambda: _FakeDedup())

    posts = []
    app_module._process(
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


def test_process_blocked_user_substitutes_first_allowed_user(app_module, monkeypatch):
    """비허용 유저 응답의 `{}` 는 ALLOWED_USER_IDS 의 첫 번째 유저를
    Slack 멘션 형식(`<@ID>`)으로 치환해야 한다. 채널 검사를 통과한 뒤에도 유저로
    차단되는 케이스."""
    import dataclasses

    override = dataclasses.replace(
        app_module.settings,
        allowed_channel_ids=[],  # 채널 검사 통과
        allowed_user_ids=["U-ADMIN", "U-OPS"],
        allowed_user_message="이 봇은 {} 만 답변합니다.",
    )
    monkeypatch.setattr(app_module, "settings", override)
    monkeypatch.setattr(app_module, "_get_dedup", lambda: _FakeDedup())

    posts = []
    app_module._process(
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

    assert posts == [{"text": "이 봇은 <@U-ADMIN> 만 답변합니다.", "thread_ts": "1.1"}]


def test_process_blocked_user_applies_in_dm(app_module, monkeypatch):
    """유저 화이트리스트는 DM 경로에도 적용되어야 한다 — 채널 화이트리스트와의
    핵심 차이. is_dm=True 라도 비허용 유저는 차단 메시지를 받는다."""
    import dataclasses

    override = dataclasses.replace(
        app_module.settings,
        allowed_user_ids=["U-ADMIN"],
        allowed_user_message="DM 도 차단합니다.",
    )
    monkeypatch.setattr(app_module, "settings", override)
    monkeypatch.setattr(app_module, "_get_dedup", lambda: _FakeDedup())

    posts = []
    app_module._process(
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

    assert posts == [{"text": "DM 도 차단합니다.", "thread_ts": "1.1"}]


def test_process_blocked_user_no_message_when_unset(app_module, monkeypatch):
    """ALLOWED_USER_MESSAGE 가 비어 있으면 차단된 유저에게 응답이 가지 않는다."""
    import dataclasses

    override = dataclasses.replace(
        app_module.settings,
        allowed_user_ids=["U-ADMIN"],
        allowed_user_message="",
    )
    monkeypatch.setattr(app_module, "settings", override)
    monkeypatch.setattr(app_module, "_get_dedup", lambda: _FakeDedup())

    posts = []
    app_module._process(
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
        app_module.settings,
        allowed_channel_ids=["C-OK"],
        allowed_channel_message="채널 차단",
        allowed_user_ids=["U-ADMIN"],
        allowed_user_message="유저 차단",
    )
    monkeypatch.setattr(app_module, "settings", override)
    monkeypatch.setattr(app_module, "_get_dedup", lambda: _FakeDedup())

    posts = []
    app_module._process(
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
        app_module.settings,
        allowed_channel_ids=[],
        allowed_user_ids=[],
        allowed_channel_message="",
        allowed_user_message="",
    )
    monkeypatch.setattr(app_module, "settings", override)
    monkeypatch.setattr(app_module, "_get_dedup", lambda: _FakeDedup())

    recorded = []

    class _Spy:
        def record(self, app_id, team_id=None):
            recorded.append({"app_id": app_id, "team_id": team_id})

    monkeypatch.setattr(app_module, "_get_app_metadata", lambda: _Spy())
    # Short-circuit the agent run — we only care that record() was called.
    monkeypatch.setattr(
        app_module,
        "_process",
        app_module._process,  # use real _process; we'll monkeypatch internals
    )
    # Stub the rest of _process so it doesn't try to actually run an agent.
    monkeypatch.setattr(app_module, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(app_module, "_get_llm", lambda: object())
    monkeypatch.setattr(app_module, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "user_name_cache", _StubUserNameCache())
    monkeypatch.setattr(app_module, "SlackMentionAgent", _StubAgent)
    monkeypatch.setattr(app_module, "StreamingMessage", _StubStream)

    event = {
        "channel": "C1",
        "ts": "1.1",
        "text": "hello",
        "user": "U1",
        "client_msg_id": "msg-meta-1",
        "team": "T-WORKSPACE",
    }
    app_module._process(
        event,
        client=_StubClient(),
        say=lambda **kw: None,
        is_dm=False,
        api_app_id="A1",
    )

    assert recorded == [{"app_id": "A1", "team_id": "T-WORKSPACE"}]


def test_process_skips_metadata_when_api_app_id_blank(app_module, monkeypatch):
    """Backwards-compat path (api_app_id="") must not write metadata —
    otherwise we'd pollute the registry with rows for api_app_id="" or
    legacy single-tenant invocations."""
    import dataclasses

    override = dataclasses.replace(
        app_module.settings,
        allowed_channel_ids=[],
        allowed_user_ids=[],
    )
    monkeypatch.setattr(app_module, "settings", override)
    monkeypatch.setattr(app_module, "_get_dedup", lambda: _FakeDedup())

    def boom_metadata():
        raise AssertionError("metadata must not be touched without api_app_id")

    monkeypatch.setattr(app_module, "_get_app_metadata", boom_metadata)
    monkeypatch.setattr(app_module, "_get_conversations", lambda: _StubConvo())
    monkeypatch.setattr(app_module, "_get_llm", lambda: object())
    monkeypatch.setattr(app_module, "set_thread_status", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "user_name_cache", _StubUserNameCache())
    monkeypatch.setattr(app_module, "SlackMentionAgent", _StubAgent)
    monkeypatch.setattr(app_module, "StreamingMessage", _StubStream)

    app_module._process(
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
