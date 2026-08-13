import io
import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from pythonjsonlogger.json import JsonFormatter

from modulo.core.logging_config import (
    CorrelationIdFilter,
    ErrorTrackingLogHandler,
    SensitiveFieldFilter,
    _apply_per_module_levels,
    _log_async_emit_error,
    _resolve_log_level,
    configure_logging,
    correlation_id_var,
    org_id_var,
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
        JsonFormatter(
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
    handler.setFormatter(JsonFormatter(fmt="%(message)s"))
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
    handler.setFormatter(JsonFormatter(fmt="%(message)s"))

    test_logger = logging.getLogger("modulo.core.pipeline_engine.executor")
    test_logger.setLevel(logging.DEBUG)
    test_logger.addHandler(handler)

    test_logger.debug("pipeline.debug_test", extra={"key": "val"})
    handler.flush()
    output = stream.getvalue()
    assert "pipeline.debug_test" in output

    test_logger.handlers.clear()


def test_configure_logging_runs_without_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """configure_logging() wires the root logger for structured JSON output."""
    monkeypatch.setenv("MODULO_LOG_LEVEL", "INFO")
    root_logger = logging.getLogger()
    before_level = root_logger.level
    before_handlers = set(root_logger.handlers)
    try:
        configure_logging()
        assert root_logger.level == logging.DEBUG
        added = [h for h in root_logger.handlers if h not in before_handlers]
        assert added, "configure_logging() must attach handlers to the root logger"
        stream_handler = next(h for h in added if isinstance(h, logging.StreamHandler))
        assert isinstance(stream_handler.formatter, JsonFormatter)
        assert any(isinstance(f, (CorrelationIdFilter, SensitiveFieldFilter)) for f in stream_handler.filters)
    finally:
        for handler in list(root_logger.handlers):
            if handler not in before_handlers:
                root_logger.removeHandler(handler)
        root_logger.setLevel(before_level)


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


def test_resolve_log_level_per_module_override_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-module MODULO_LOG_LEVEL_<MODULE> override beats the global default."""
    monkeypatch.setenv("MODULO_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("MODULO_LOG_LEVEL_MODULO_CORE_PIPELINE_ENGINE", "WARNING")
    assert _resolve_log_level("modulo.core.pipeline_engine") == "WARNING"


def test_resolve_log_level_falls_back_to_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a per-module override the global MODULO_LOG_LEVEL applies."""
    monkeypatch.setenv("MODULO_LOG_LEVEL", "DEBUG")
    assert _resolve_log_level("modulo.core.unrelated") == "DEBUG"


def test_resolve_log_level_defaults_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """With neither env var set the level defaults to INFO."""
    monkeypatch.delenv("MODULO_LOG_LEVEL", raising=False)
    monkeypatch.delenv("MODULO_LOG_LEVEL_MODULO_UNKNOWN", raising=False)
    assert _resolve_log_level("modulo.unknown") == "INFO"


def test_log_async_emit_error_reports_exception(caplog: pytest.LogCaptureFixture) -> None:
    """A failing async-emit task surfaces its exception through the module logger."""
    future = MagicMock()
    future.exception.return_value = RuntimeError("boom")
    with caplog.at_level(logging.ERROR):
        _log_async_emit_error(future)
    assert any("ErrorTrackingLogHandler.async_emit_failed" in r.getMessage() for r in caplog.records)


def test_log_async_emit_error_ignores_clean_future(caplog: pytest.LogCaptureFixture) -> None:
    """A healthy async-emit task logs nothing."""
    future = MagicMock()
    future.exception.return_value = None
    with caplog.at_level(logging.ERROR):
        _log_async_emit_error(future)
    assert not [r for r in caplog.records if "async_emit_failed" in r.getMessage()]


def test_apply_per_module_levels_sets_logger_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MODULO_LOG_LEVEL_<MODULE> env vars map to the matching logger's level."""
    monkeypatch.setenv("MODULO_LOG_LEVEL_MODULO_CORE_PIPELINE_ENGINE", "WARNING")
    _apply_per_module_levels()
    assert logging.getLogger("modulo.core.pipeline.engine").level == logging.WARNING
    logging.getLogger("modulo.core.pipeline.engine").setLevel(logging.NOTSET)


def test_apply_per_module_levels_invalid_level_falls_back_to_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognised level string falls back to INFO rather than raising."""
    monkeypatch.setenv("MODULO_LOG_LEVEL_MODULO_CORE_PIPELINE_ENGINE", "VERBOSE")
    _apply_per_module_levels()
    assert logging.getLogger("modulo.core.pipeline.engine").level == logging.INFO
    logging.getLogger("modulo.core.pipeline.engine").setLevel(logging.NOTSET)


def test_apply_per_module_levels_ignores_dup_module_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate module paths (case/underscore variants) are applied only once."""
    monkeypatch.setenv("MODULO_LOG_LEVEL_A_B", "WARNING")
    monkeypatch.setenv("MODULO_LOG_LEVEL_a.b", "WARNING")
    _apply_per_module_levels()
    assert logging.getLogger("a.b").level == logging.WARNING
    logging.getLogger("a.b").setLevel(logging.NOTSET)


def test_emit_swallows_missing_event_loop() -> None:
    """emit() must not raise when no asyncio event loop is running."""
    token = org_id_var.set("00000000-0000-0000-0000-000000000001")
    try:
        handler = ErrorTrackingLogHandler()
        with patch("asyncio.get_running_loop", side_effect=RuntimeError("no running loop")):
            handler.emit(
                logging.LogRecord(
                    name="test",
                    level=logging.ERROR,
                    pathname=__file__,
                    lineno=1,
                    msg="boom",
                    args=(),
                    exc_info=None,
                )
            )
        assert handler._pending_tasks == 0
    finally:
        org_id_var.reset(token)
