"""Tests for src.router (receiver path + worker path + per-app Bolt cache).

Covers _route_request, _get_bolt_app, _enqueue_worker, _process_worker.
External dependencies (boto3, slack_sdk.WebClient, Bolt, SSM) are mocked.
"""
import base64
import json

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


# --------------------------------------------------------------------------- #
# _route_request — multi-tenant dispatch
# --------------------------------------------------------------------------- #


def _stub_route_dependencies(monkeypatch, app_module, creds_map):
    """Replace _get_credentials + _get_bolt_app + SlackRequestHandler so
    _route_request can be exercised without touching SSM / Bolt."""
    fake_creds = _FakeCreds(creds_map)
    monkeypatch.setattr(_runtime, "_get_credentials", lambda: fake_creds)

    bolt_calls = {}

    def fake_get_bolt(api_app_id, signing_secret, bot_token):
        bolt_calls.setdefault("seen", []).append((api_app_id, signing_secret, bot_token))
        return f"bolt:{api_app_id}"

    monkeypatch.setattr(_router, "_get_bolt_app", fake_get_bolt)

    handle_calls = []

    class FakeHandler:
        def __init__(self, bolt_app):
            handle_calls.append(("init", bolt_app))

        def handle(self, event, context):
            handle_calls.append(("handle", event, context))
            return {"statusCode": 200, "body": "bolt-handled"}

    monkeypatch.setattr(_router, "SlackRequestHandler", FakeHandler)
    return fake_creds, bolt_calls, handle_calls


def test_route_request_url_verification_echoes_challenge_without_creds_lookup(app_module, monkeypatch):
    """URL verification has no api_app_id in the body, so we cannot look up
    a signing_secret. Echo the challenge directly — it's a setup-time ping
    with no actionable payload."""
    fake_creds, _, handle_calls = _stub_route_dependencies(monkeypatch, app_module, {})

    body = json.dumps({"type": "url_verification", "challenge": "abc-123"})
    event = {"body": body}

    result = _router._route_request(event, None)
    assert result == {"statusCode": 200, "body": "abc-123"}
    # Crucially, we did NOT call SSM or Bolt.
    assert fake_creds.calls == []
    assert handle_calls == []


def test_route_request_unparseable_body_returns_400(app_module, monkeypatch):
    fake_creds, _, _ = _stub_route_dependencies(monkeypatch, app_module, {})
    event = {"body": "<<not json>>"}
    assert _router._route_request(event, None) == {"statusCode": 400, "body": ""}
    assert fake_creds.calls == []


def test_route_request_empty_body_returns_400(app_module, monkeypatch):
    _stub_route_dependencies(monkeypatch, app_module, {})
    assert _router._route_request({}, None) == {"statusCode": 400, "body": ""}


def test_route_request_missing_api_app_id_returns_200(app_module, monkeypatch):
    """Some Slack request shapes lack api_app_id (e.g. unknown event types).
    We can't route, but we don't want Slack to retry — return 200."""
    fake_creds, _, handle_calls = _stub_route_dependencies(monkeypatch, app_module, {})

    body = json.dumps({"type": "event_callback", "event": {"type": "unknown"}})
    result = _router._route_request({"body": body}, None)

    assert result == {"statusCode": 200, "body": ""}
    assert fake_creds.calls == []
    assert handle_calls == []


def test_route_request_unknown_app_returns_200_without_dispatch(app_module, monkeypatch):
    """An app that hasn't been provisioned in SSM yet gets a structured
    log + 200. No Bolt invocation (no signing_secret to verify with)."""
    fake_creds, _, handle_calls = _stub_route_dependencies(monkeypatch, app_module, {})

    body = json.dumps({"api_app_id": "A-NEW", "type": "event_callback", "event": {}})
    result = _router._route_request({"body": body}, None)

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
    result = _router._route_request(event, "ctx-x")

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

    result = _router._route_request(event, None)
    assert result == {"statusCode": 200, "body": "bolt-handled"}


# --------------------------------------------------------------------------- #
# _get_bolt_app — per-app cache + rotation detection
# --------------------------------------------------------------------------- #


def test_get_bolt_app_caches_per_app_id(app_module, monkeypatch):
    """Same (app_id, signing_secret, bot_token) returns the same App object."""
    monkeypatch.setattr(_runtime, "_bolt_apps", {})

    constructed = []

    class FakeApp:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def event(self, _name):
            def deco(fn):
                return fn
            return deco

        def command(self, _name):
            def deco(fn):
                return fn
            return deco

    monkeypatch.setattr(_router, "App", FakeApp)

    a1 = _router._get_bolt_app("A1", "sig", "tok")
    a2 = _router._get_bolt_app("A1", "sig", "tok")
    assert a1 is a2
    assert len(constructed) == 1


def test_get_bolt_app_isolates_apps(app_module, monkeypatch):
    """Different api_app_ids get different App instances."""
    monkeypatch.setattr(_runtime, "_bolt_apps", {})

    constructed = []

    class FakeApp:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def event(self, _name):
            def deco(fn):
                return fn
            return deco

        def command(self, _name):
            def deco(fn):
                return fn
            return deco

    monkeypatch.setattr(_router, "App", FakeApp)

    a1 = _router._get_bolt_app("A1", "sig1", "tok1")
    a2 = _router._get_bolt_app("A2", "sig2", "tok2")
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
    monkeypatch.setattr(_runtime, "_bolt_apps", {})

    constructed = []

    class FakeApp:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def event(self, _name):
            def deco(fn):
                return fn
            return deco

        def command(self, _name):
            def deco(fn):
                return fn
            return deco

    monkeypatch.setattr(_router, "App", FakeApp)

    first = _router._get_bolt_app("A1", "sig-old", "tok-old")
    second = _router._get_bolt_app("A1", "sig-new", "tok-new")
    assert first is not second
    assert constructed[1]["signing_secret"] == "sig-new"
    assert constructed[1]["token"] == "tok-new"


def test_get_bolt_app_disables_token_verification(app_module, monkeypatch):
    """We don't want Bolt calling auth.test on every cold container × per
    app_id — that's a needless Slack roundtrip on the request path."""
    monkeypatch.setattr(_runtime, "_bolt_apps", {})

    constructed = []

    class FakeApp:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def event(self, _name):
            def deco(fn):
                return fn
            return deco

        def command(self, _name):
            def deco(fn):
                return fn
            return deco

    monkeypatch.setattr(_router, "App", FakeApp)
    _router._get_bolt_app("A1", "sig", "tok")
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

    monkeypatch.setattr(_runtime, "_get_lambda_client", boom_client)

    captured = []
    monkeypatch.setattr(_router, "_process_worker", lambda payload: captured.append(payload))

    event = {"channel": "C1", "text": "hello"}
    _router._enqueue_worker(event, is_dm=False, api_app_id="A1")

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

    monkeypatch.setattr(_runtime, "_get_lambda_client", lambda: FakeLambdaClient())

    def boom_inline(payload):
        raise AssertionError("inline worker must not run when invoke succeeds")

    monkeypatch.setattr(_router, "_process_worker", boom_inline)

    event = {"channel": "D1", "text": "안녕", "user": "U1"}
    _router._enqueue_worker(event, is_dm=True, api_app_id="A-multi")

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

    monkeypatch.setattr(_runtime, "_get_lambda_client", lambda: FakeLambdaClient())
    monkeypatch.setattr(_router, "_process_worker", lambda _p: None)

    _router._enqueue_worker({"text": "hi"}, is_dm=False, api_app_id="A1")
    payload_bytes = invocations[0]["Payload"]
    assert b"xoxb" not in payload_bytes
    assert b"signing_secret" not in payload_bytes
    assert b"bot_token" not in payload_bytes


def test_enqueue_worker_drops_with_user_notice_on_invoke_failure(app_module, monkeypatch):
    """If boto3.invoke raises (IAM, throttle, network), the receiver does
    NOT run the agent inline — that would burn the API Gateway / Slack
    ack budget and trigger a retry storm. Instead, post a short notice
    via the Bolt-injected client and drop the request."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "gurumi-mention")

    class BrokenClient:
        def invoke(self, **_kwargs):
            raise RuntimeError("network unreachable")

    monkeypatch.setattr(_runtime, "_get_lambda_client", lambda: BrokenClient())

    def boom_inline(payload):
        raise AssertionError("inline worker must not run on invoke failure")

    monkeypatch.setattr(_router, "_process_worker", boom_inline)

    posts: list[dict] = []

    class _BoltClient:
        def chat_postMessage(self, **kwargs):
            posts.append(kwargs)

    event = {"channel": "C1", "ts": "1.1", "text": "hi"}
    _router._enqueue_worker(event, is_dm=False, api_app_id="A1", client=_BoltClient())

    assert len(posts) == 1
    assert posts[0]["channel"] == "C1"
    assert posts[0]["thread_ts"] == "1.1"
    assert "다시 시도" in posts[0]["text"]


def test_enqueue_worker_drops_silently_when_no_client_on_invoke_failure(
    app_module, monkeypatch
):
    """Reaction events have no natural reply surface (no `client` passed),
    so an invoke failure must drop silently instead of raising."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "gurumi-mention")

    class BrokenClient:
        def invoke(self, **_kwargs):
            raise RuntimeError("network unreachable")

    monkeypatch.setattr(_runtime, "_get_lambda_client", lambda: BrokenClient())
    monkeypatch.setattr(_router, "_process_worker", lambda _p: None)

    event = {"channel": "C1", "text": "hi"}
    # Should not raise.
    _router._enqueue_worker(event, is_dm=False, api_app_id="A1")


def test_enqueue_worker_payload_preserves_non_ascii(app_module, monkeypatch):
    """Korean / emoji in the Slack event must survive the JSON round-trip."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "gurumi-mention")

    invocations = []

    class FakeClient:
        def invoke(self, **kwargs):
            invocations.append(kwargs)

    monkeypatch.setattr(_runtime, "_get_lambda_client", lambda: FakeClient())
    monkeypatch.setattr(_router, "_process_worker", lambda _p: None)

    event = {"text": "이미지 그려줘 🎨", "channel": "C1"}
    _router._enqueue_worker(event, is_dm=False, api_app_id="A1")

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

    monkeypatch.setattr(_runtime, "_get_credentials", boom_creds)

    def boom_process(*args, **kwargs):
        raise AssertionError("_process must not run without credentials")

    monkeypatch.setattr(_message, "_process", boom_process)
    # Should not raise.
    _router._process_worker({"slack_event": {"channel": "C1", "text": "hi"}})


def test_process_worker_unknown_app_skips_processing(app_module, monkeypatch):
    """If an app's secrets disappear between receiver and worker (race with
    operator deletion), drop the work — there's no token to reply with."""
    monkeypatch.setattr(_runtime, "_get_credentials", lambda: _FakeCreds({}))

    def boom_process(*args, **kwargs):
        raise AssertionError("_process must not run without credentials")

    monkeypatch.setattr(_message, "_process", boom_process)

    _router._process_worker(
        {"slack_event": {"channel": "C1", "text": "hi"}, "is_dm": False, "api_app_id": "A-gone"}
    )


def test_process_worker_builds_webclient_from_ssm_bot_token(app_module, monkeypatch):
    """The worker must mint a fresh WebClient using the bot_token resolved
    from SSM for the carried api_app_id — not from any static settings field."""
    from src.credentials import SlackAppCredentials

    monkeypatch.setattr(
        _runtime,
        "_get_credentials",
        lambda: _FakeCreds({"A1": SlackAppCredentials(signing_secret="s", bot_token="xoxb-from-ssm")}),
    )

    created = []

    class FakeWeb:
        def __init__(self, token):
            created.append(token)
            self.token = token

    monkeypatch.setattr(_router, "WebClient", FakeWeb)

    captured = {}

    def fake_process(event, client, say, is_dm, api_app_id=""):
        captured["event"] = event
        captured["client"] = client
        captured["say"] = say
        captured["is_dm"] = is_dm
        captured["api_app_id"] = api_app_id

    monkeypatch.setattr(_message, "_process", fake_process)

    payload = {"slack_event": {"channel": "C1", "text": "hi"}, "is_dm": True, "api_app_id": "A1"}
    _router._process_worker(payload)

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
        _runtime,
        "_get_credentials",
        lambda: _FakeCreds({"A1": SlackAppCredentials(signing_secret="s", bot_token="t")}),
    )

    posts = []

    class FakeWeb:
        def __init__(self, token):
            pass

        def chat_postMessage(self, **kwargs):
            posts.append(kwargs)

    monkeypatch.setattr(_router, "WebClient", FakeWeb)

    captured_say = {}

    def fake_process(event, client, say, is_dm, api_app_id=""):
        captured_say["fn"] = say

    monkeypatch.setattr(_message, "_process", fake_process)

    payload = {"slack_event": {"channel": "C-origin", "text": "x"}, "is_dm": False, "api_app_id": "A1"}
    _router._process_worker(payload)

    say = captured_say["fn"]
    say("hello world")
    say("threaded reply", thread_ts="1700000000.000100")

    assert posts == [
        {"channel": "C-origin", "text": "hello world"},
        {"channel": "C-origin", "text": "threaded reply", "thread_ts": "1700000000.000100"},
    ]


# --------------------------------------------------------------------------- #
# Slash command receiver — _route_command + _enqueue_command_worker
# --------------------------------------------------------------------------- #


def _urlencoded(fields: dict) -> str:
    import urllib.parse

    return urllib.parse.urlencode(fields)


def test_route_request_slash_command_path_decodes_form_and_dispatches(app_module, monkeypatch):
    """`POST /slack/command` carries application/x-www-form-urlencoded — the
    router extracts api_app_id from the form and hands the raw event to the
    per-app Bolt App for signature verification."""
    from src.credentials import SlackAppCredentials

    fake_creds, bolt_calls, handle_calls = _stub_route_dependencies(
        monkeypatch,
        app_module,
        {"A1": SlackAppCredentials(signing_secret="s", bot_token="t")},
    )

    body = _urlencoded(
        {
            "api_app_id": "A1",
            "command": "/img-xai",
            "text": "사과",
            "channel_id": "C1",
            "user_id": "U1",
            "trigger_id": "trig-1",
            "response_url": "https://hooks.slack.com/r1",
            "team_id": "T1",
        }
    )
    event = {"path": "/slack/command", "body": body}

    result = _router._route_request(event, "ctx-x")
    assert result == {"statusCode": 200, "body": "bolt-handled"}
    assert fake_creds.calls == ["A1"]
    assert bolt_calls["seen"] == [("A1", "s", "t")]
    assert handle_calls == [("init", "bolt:A1"), ("handle", event, "ctx-x")]


def test_route_request_slash_command_decodes_base64_body(app_module, monkeypatch):
    """API Gateway sometimes delivers form bodies as base64. The form parser
    must decode before parsing — otherwise api_app_id is invisible."""
    import base64

    from src.credentials import SlackAppCredentials

    _stub_route_dependencies(
        monkeypatch,
        app_module,
        {"A1": SlackAppCredentials(signing_secret="s", bot_token="t")},
    )

    raw = _urlencoded({"api_app_id": "A1", "command": "/img-gpt", "text": "x"})
    encoded = base64.b64encode(raw.encode()).decode()
    event = {"path": "/dev/slack/command", "body": encoded, "isBase64Encoded": True}

    result = _router._route_request(event, None)
    assert result == {"statusCode": 200, "body": "bolt-handled"}


def test_route_request_slash_command_unknown_app_returns_200(app_module, monkeypatch):
    fake_creds, _, handle_calls = _stub_route_dependencies(monkeypatch, app_module, {})

    body = _urlencoded({"api_app_id": "A-NEW", "command": "/img-xai", "text": "x"})
    event = {"path": "/slack/command", "body": body}

    result = _router._route_request(event, None)
    assert result == {"statusCode": 200, "body": ""}
    assert fake_creds.calls == ["A-NEW"]
    assert handle_calls == []


def test_route_request_slash_command_missing_app_id_returns_200(app_module, monkeypatch):
    """A form body without api_app_id can't be routed — log + 200 so Slack
    does not retry. Mirrors the events-side behaviour for the same shape."""
    fake_creds, _, handle_calls = _stub_route_dependencies(monkeypatch, app_module, {})

    body = _urlencoded({"command": "/img-xai", "text": "x"})
    event = {"path": "/slack/command", "body": body}

    result = _router._route_request(event, None)
    assert result == {"statusCode": 200, "body": ""}
    assert fake_creds.calls == []
    assert handle_calls == []


def test_route_request_path_dispatches_events_not_commands(app_module, monkeypatch):
    """Sanity: a JSON event body on `/slack/events` still flows through the
    existing JSON-decode path, not the form-decode command path."""
    from src.credentials import SlackAppCredentials

    fake_creds, _, handle_calls = _stub_route_dependencies(
        monkeypatch,
        app_module,
        {"A1": SlackAppCredentials(signing_secret="s", bot_token="t")},
    )

    body = json.dumps({"api_app_id": "A1", "type": "event_callback", "event": {}})
    event = {"path": "/slack/events", "body": body}

    result = _router._route_request(event, "ctx")
    assert result == {"statusCode": 200, "body": "bolt-handled"}
    assert fake_creds.calls == ["A1"]
    assert len(handle_calls) == 2


# --------------------------------------------------------------------------- #
# _enqueue_command_worker — payload shape + secret-free + invoke fallback
# --------------------------------------------------------------------------- #


def test_enqueue_command_worker_runs_inline_when_not_in_lambda(app_module, monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setattr(
        _runtime,
        "_get_lambda_client",
        lambda: (_ for _ in ()).throw(AssertionError("invoke must not run off-Lambda")),
    )

    captured = []
    monkeypatch.setattr(_router, "_process_worker", lambda p: captured.append(p))

    body = {
        "api_app_id": "A1",
        "command": "/img-xai",
        "text": "사과",
        "channel_id": "C1",
        "user_id": "U1",
        "trigger_id": "trig-1",
        "response_url": "https://r",
        "team_id": "T1",
    }
    _router._enqueue_command_worker(body, api_app_id="A1")

    assert captured == [
        {
            "kind": "command",
            "command_payload": {
                "command": "/img-xai",
                "text": "사과",
                "channel_id": "C1",
                "user_id": "U1",
                "trigger_id": "trig-1",
                "response_url": "https://r",
                "team_id": "T1",
            },
            "api_app_id": "A1",
        }
    ]


def test_enqueue_command_worker_fires_async_invoke_in_lambda(app_module, monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "gurumi-mention")

    invocations = []

    class FakeLambdaClient:
        def invoke(self, **kwargs):
            invocations.append(kwargs)
            return {"StatusCode": 202}

    monkeypatch.setattr(_runtime, "_get_lambda_client", lambda: FakeLambdaClient())
    monkeypatch.setattr(
        _router,
        "_process_worker",
        lambda _p: (_ for _ in ()).throw(AssertionError("inline must not run")),
    )

    _router._enqueue_command_worker(
        {
            "api_app_id": "A-multi",
            "command": "/img-gpt",
            "text": "sky",
            "channel_id": "C9",
            "user_id": "U9",
            "trigger_id": "trig-9",
            "response_url": "https://r",
            "team_id": "T1",
        },
        api_app_id="A-multi",
    )

    assert len(invocations) == 1
    call = invocations[0]
    payload = json.loads(call["Payload"].decode("utf-8"))
    assert payload == {
        "_worker": True,
        "kind": "command",
        "command_payload": {
            "command": "/img-gpt",
            "text": "sky",
            "channel_id": "C9",
            "user_id": "U9",
            "trigger_id": "trig-9",
            "response_url": "https://r",
            "team_id": "T1",
        },
        "api_app_id": "A-multi",
    }


def test_enqueue_command_worker_does_not_ship_secrets(app_module, monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "gurumi-mention")

    invocations = []

    class FakeLambdaClient:
        def invoke(self, **kwargs):
            invocations.append(kwargs)

    monkeypatch.setattr(_runtime, "_get_lambda_client", lambda: FakeLambdaClient())
    monkeypatch.setattr(_router, "_process_worker", lambda _p: None)

    # Body deliberately includes a `token` field (Slack's deprecated verification
    # token still shows up in form payloads). The enqueue path must NOT
    # forward it into the worker payload.
    _router._enqueue_command_worker(
        {
            "api_app_id": "A1",
            "command": "/img-gpt",
            "text": "x",
            "channel_id": "C1",
            "user_id": "U1",
            "trigger_id": "t",
            "response_url": "https://r",
            "team_id": "T1",
            "token": "deprecated-verification-token",
        },
        api_app_id="A1",
    )

    bytes_payload = invocations[0]["Payload"]
    assert b"deprecated-verification-token" not in bytes_payload
    assert b"signing_secret" not in bytes_payload
    assert b"xoxb" not in bytes_payload


def test_enqueue_command_worker_notifies_via_response_url_on_invoke_failure(
    app_module, monkeypatch
):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "gurumi-mention")

    class BrokenClient:
        def invoke(self, **_kwargs):
            raise RuntimeError("network unreachable")

    monkeypatch.setattr(_runtime, "_get_lambda_client", lambda: BrokenClient())
    monkeypatch.setattr(_router, "_process_worker", lambda _p: None)

    posted: list[dict] = []

    def fake_urlopen(req, timeout=5):
        posted.append({"url": req.full_url, "data": req.data})

        class _R:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        return _R()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    _router._enqueue_command_worker(
        {
            "api_app_id": "A1",
            "command": "/img-gpt",
            "text": "x",
            "channel_id": "C1",
            "user_id": "U1",
            "trigger_id": "t",
            "response_url": "https://hooks.slack.com/r",
            "team_id": "T1",
        },
        api_app_id="A1",
    )

    assert len(posted) == 1
    assert posted[0]["url"] == "https://hooks.slack.com/r"
    payload = json.loads(posted[0]["data"].decode("utf-8"))
    assert payload["response_type"] == "ephemeral"
    assert "다시 시도" in payload["text"]


# --------------------------------------------------------------------------- #
# _process_worker — kind=command routes to commands._process_command
# --------------------------------------------------------------------------- #


def test_process_worker_kind_command_dispatches_to_commands(app_module, monkeypatch):
    from src.credentials import SlackAppCredentials
    from src.handlers import commands as _commands_mod

    monkeypatch.setattr(
        _runtime,
        "_get_credentials",
        lambda: _FakeCreds({"A1": SlackAppCredentials(signing_secret="s", bot_token="xoxb-1")}),
    )

    created_tokens = []

    class FakeWeb:
        def __init__(self, token):
            created_tokens.append(token)
            self.token = token

    monkeypatch.setattr(_router, "WebClient", FakeWeb)

    def boom_message(*_args, **_kwargs):
        raise AssertionError("message._process must not run for command payload")

    def boom_reaction(*_args, **_kwargs):
        raise AssertionError("reactions._process_reaction must not run for command payload")

    monkeypatch.setattr(_message, "_process", boom_message)
    monkeypatch.setattr(_reactions, "_process_reaction", boom_reaction)

    captured: dict = {}

    def fake_process_command(payload, client, api_app_id=""):
        captured["payload"] = payload
        captured["client"] = client
        captured["api_app_id"] = api_app_id

    monkeypatch.setattr(_commands_mod, "_process_command", fake_process_command)

    payload = {
        "kind": "command",
        "command_payload": {
            "command": "/img-xai",
            "text": "x",
            "channel_id": "C1",
            "trigger_id": "t",
        },
        "api_app_id": "A1",
    }
    _router._process_worker(payload)

    assert created_tokens == ["xoxb-1"]
    assert captured["payload"] == payload["command_payload"]
    assert captured["api_app_id"] == "A1"


def test_process_worker_kind_command_skips_when_app_id_missing(app_module, monkeypatch):
    """Defensive: a malformed command payload with no api_app_id must not crash."""

    def boom_creds():
        raise AssertionError("credentials lookup must not run without api_app_id")

    monkeypatch.setattr(_runtime, "_get_credentials", boom_creds)

    from src.handlers import commands as _commands_mod

    def boom_process(*_args, **_kwargs):
        raise AssertionError("_process_command must not run without credentials")

    monkeypatch.setattr(_commands_mod, "_process_command", boom_process)

    # Should not raise.
    _router._process_worker(
        {"kind": "command", "command_payload": {"command": "/img-gpt"}, "api_app_id": ""}
    )

