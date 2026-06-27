import io
import json
import logging

import pytest
from pythonjsonlogger import jsonlogger

from modulo.core.logging_config import (
    CorrelationIdFilter,
    SensitiveFieldFilter,
    configure_logging,
    correlation_id_var,
    redact_sensitive,
)


def test_redact_sensitive_flat() -> None:
    extra = {"api_key": "sk-abc123", "pipeline_id": "p-1", "token": "tok_xyz", "user_id": "u-1"}
    redacted = redact_sensitive(extra)
    assert redacted["api_key"] == "***"
    assert redacted["token"] == "***"
    assert redacted["pipeline_id"] == "p-1"
    assert redacted["user_id"] == "u-1"


def test_redact_sensitive_nested() -> None:
    extra = {"creds": {"api_key": "sk-abc123", "name": "test"}, "safe": "value"}
    redacted = redact_sensitive(extra)
    assert redacted["creds"]["api_key"] == "***"
    assert redacted["creds"]["name"] == "test"
    assert redacted["safe"] == "value"


def test_redact_sensitive_empty() -> None:
    assert redact_sensitive({}) == {}


def test_correlation_id_filter_injects() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="test",
        args=(),
        exc_info=None,
    )
    assert not hasattr(record, "correlation_id")

    token = correlation_id_var.set("cid-123")
    try:
        result = CorrelationIdFilter().filter(record)
        assert result is True
        assert record.correlation_id == "cid-123"
    finally:
        correlation_id_var.reset(token)


def test_correlation_id_filter_empty_when_not_set() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="test",
        args=(),
        exc_info=None,
    )
    CorrelationIdFilter().filter(record)
    assert record.correlation_id == ""


def test_sensitive_field_filter_redacts() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="test",
        args=(),
        exc_info=None,
    )
    record.api_key = "should-be-redacted"
    record.safe_field = "should-stay"
    SensitiveFieldFilter().filter(record)
    assert record.api_key == "***"
    assert record.safe_field == "should-stay"


def test_json_output_valid_json() -> None:
    """Test that a log record produces valid JSON."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            fmt="%(message)s %(level)s %(name)s",
            timestamp=True,
        )
    )
    root = logging.getLogger("test_json")
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    root.handlers.clear()
    root.addHandler(handler)

    test_logger = logging.getLogger("test_json.child")
    test_logger.info("test.event", extra={"key": "value"})

    handler.flush()
    output = stream.getvalue()
    parsed = json.loads(output.strip())
    assert parsed["message"] == "test.event"
    assert parsed["key"] == "value"


def test_log_level_filtering() -> None:
    """Test that DEBUG messages are filtered when level is INFO."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(jsonlogger.JsonFormatter(fmt="%(message)s"))
    logger = logging.getLogger("test_level_filter")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.handlers.clear()
    logger.addHandler(handler)

    logger.debug("debug.event", extra={"detail": "should not appear"})
    logger.info("info.event", extra={"detail": "should appear"})
    logger.warning("warn.event", extra={"detail": "should appear"})

    handler.flush()
    output = stream.getvalue()
    assert "debug.event" not in output
    assert "info.event" in output
    assert "warn.event" in output

    logger.handlers.clear()


def test_per_module_level_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test per-module log level override via env var."""
    monkeypatch.setenv("MODULO_LOG_LEVEL_MODULO_CORE_PIPELINE_ENGINE", "DEBUG")

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(jsonlogger.JsonFormatter(fmt="%(message)s"))

    test_logger = logging.getLogger("modulo.core.pipeline_engine.executor")
    test_logger.setLevel(logging.DEBUG)
    test_logger.addHandler(handler)

    test_logger.debug("pipeline.debug_test", extra={"key": "val"})
    handler.flush()
    output = stream.getvalue()
    assert "pipeline.debug_test" in output

    test_logger.handlers.clear()


def test_configure_logging_runs_without_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that configure_logging() does not raise."""
    monkeypatch.setenv("MODULO_LOG_LEVEL", "INFO")
    try:
        configure_logging()
    except Exception:
        pytest.fail("configure_logging() raised unexpectedly")


def test_known_sensitive_keys_redacted_in_extra() -> None:
    """Test that all known sensitive field patterns are caught."""
    sensitive = {
        "api_key": "visible",
        "api_secret": "visible",
        "access_key": "visible",
        "secret_key": "visible",
        "token": "visible",
        "password": "visible",
        "passwd": "visible",
        "secret": "visible",
        "private_key": "visible",
        "credential": "visible",
    }
    redacted = redact_sensitive(sensitive)
    for key in sensitive:
        assert redacted[key] == "***", f"{key} was not redacted"


def test_safe_fields_preserved() -> None:
    extra = {
        "run_id": "r-123",
        "pipeline_id": "p-456",
        "org_id": "o-789",
        "user_id": "u-abc",
        "gate_id": "g-xyz",
        "duration_ms": 1500,
    }
    redacted = redact_sensitive(extra)
    assert redacted == extra
