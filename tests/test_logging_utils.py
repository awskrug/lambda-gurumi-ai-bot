import json
import logging
from io import StringIO

from src.logging_utils import JsonFormatter, get_logger, log_event, set_request_id


def test_json_formatter_includes_request_id():
    set_request_id("req-abc")
    rec = logging.LogRecord("x", logging.INFO, "f.py", 1, "hello", None, None)
    formatted = JsonFormatter().format(rec)
    payload = json.loads(formatted)
    assert payload["request_id"] == "req-abc"
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"


def test_json_formatter_includes_extra_fields():
    rec = logging.LogRecord("x", logging.INFO, "f.py", 1, "evt", None, None)
    rec.extra_fields = {"key": "value", "n": 3}
    payload = json.loads(JsonFormatter().format(rec))
    assert payload["key"] == "value"
    assert payload["n"] == 3


def test_json_formatter_renders_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord("x", logging.ERROR, "f.py", 1, "err", None, sys.exc_info())
    payload = json.loads(JsonFormatter().format(rec))
    assert "boom" in payload["exc"]


def test_log_event_attaches_fields():
    """log_event should emit a LogRecord whose extra_fields match the kwargs."""
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    underlying = logging.getLogger("test.log_event")
    underlying.addHandler(_Capture())
    underlying.setLevel(logging.INFO)
    try:
        adapter = logging.LoggerAdapter(underlying, extra={})
        log_event(adapter, "my.event", tool="x", count=5)
    finally:
        underlying.handlers = [h for h in underlying.handlers if not isinstance(h, _Capture)]

    assert captured, "no log record emitted"
    extras = getattr(captured[0], "extra_fields", None)
    assert extras == {"tool": "x", "count": 5}
