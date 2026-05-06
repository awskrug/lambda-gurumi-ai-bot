"""Tests for app.lambda_handler — the Lambda entrypoint that dispatches to src.router."""
import pytest

from src import router as _router


@pytest.fixture
def app_module():
    """Import the app module fresh."""
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

    monkeypatch.setattr(_router, "_process_worker", fake_worker)

    def boom_route(event, context):
        raise AssertionError("_route_request must not be called on the worker path")

    monkeypatch.setattr(_router, "_route_request", boom_route)

    event = {"_worker": True, "slack_event": {"channel": "C1", "text": "hi"}, "is_dm": False, "api_app_id": "A1"}
    result = app_module.lambda_handler(event, None)

    assert result == {"statusCode": 200, "body": ""}
    assert received["payload"] is event


def test_lambda_handler_short_circuits_slack_retry_without_routing(app_module, monkeypatch):
    """Receiver path: an X-Slack-Retry-Num header means Slack is re-delivering.
    We already dispatched the first try to a worker; swallow the retry."""

    def boom_route(event, context):
        raise AssertionError("_route_request must not be invoked on a retried delivery")

    monkeypatch.setattr(_router, "_route_request", boom_route)

    event = {"headers": {"X-Slack-Retry-Num": "1"}, "body": "..."}
    assert app_module.lambda_handler(event, None) == {"statusCode": 200, "body": ""}


def test_lambda_handler_short_circuits_slack_retry_lowercase_header(app_module, monkeypatch):
    """API Gateway may lowercase the header name. Our guard normalizes."""
    monkeypatch.setattr(
        _router,
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

    monkeypatch.setattr(_router, "_route_request", fake_route)

    event = {"headers": {"Content-Type": "application/json"}, "body": "{}"}
    result = app_module.lambda_handler(event, "ctx")

    assert result == {"statusCode": 200, "body": "routed"}
    assert calls == [("route", event, "ctx")]


def test_lambda_handler_worker_flag_false_takes_receiver_path(app_module, monkeypatch):
    """`_worker=False` should fall through to the receiver path, not be
    treated as a worker marker."""

    def boom_worker(payload):
        raise AssertionError("_process_worker must not run when _worker is falsy")

    monkeypatch.setattr(_router, "_process_worker", boom_worker)
    monkeypatch.setattr(_router, "_route_request", lambda *_: {"statusCode": 202, "body": ""})

    assert app_module.lambda_handler({"_worker": False, "headers": {}}, None) == {"statusCode": 202, "body": ""}


# --------------------------------------------------------------------------- #
# _route_request — multi-tenant dispatch
