import asyncio
import io
import json
import logging
import sys
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pythonjsonlogger.json import JsonFormatter
from sqlalchemy.exc import ProgrammingError

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
    assert not redact_sensitive({})


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
    assert not record.correlation_id


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


# ── Helpers ──────────────────────────────────────────────────────────────────


def _record(
    level: int = logging.ERROR,
    msg: str = "boom",
    exc_info: tuple | None = None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.logging_config",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )


class _AsyncCtx:
    """Minimal async context manager returning a fixed value from __aenter__."""

    def __init__(self, aenter_value: object) -> None:
        self._value = aenter_value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class TestResolveLogLevel:
    def test_defaults_to_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_LOG_LEVEL", raising=False)
        monkeypatch.delenv("MODULO_LOG_LEVEL_MODULO_CORE_FOO", raising=False)
        assert _resolve_log_level("modulo.core.foo") == "INFO"

    def test_global_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_LOG_LEVEL", "DEBUG")
        monkeypatch.delenv("MODULO_LOG_LEVEL_MODULO_CORE_FOO", raising=False)
        assert _resolve_log_level("modulo.core.foo") == "DEBUG"

    def test_per_module_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_LOG_LEVEL", "WARNING")
        monkeypatch.setenv("MODULO_LOG_LEVEL_MODULO_CORE_FOO", "ERROR")
        assert _resolve_log_level("modulo.core.foo") == "ERROR"

    def test_dotted_module_path_becomes_underscored_env_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_LOG_LEVEL", raising=False)
        monkeypatch.setenv("MODULO_LOG_LEVEL_MODULO_API_ROUTES_RUNS", "WARNING")
        assert _resolve_log_level("modulo.api.routes.runs") == "WARNING"


def _completed_future(exc: BaseException | None = None) -> asyncio.Future[None]:
    """Build a finished ``asyncio.Future``.

    Constructing an ``asyncio.Future`` needs a running event loop on Python
    3.12+ (``get_event_loop`` no longer fabricates one), so build it inside
    ``asyncio.run`` and hand back the finished future. The callback under test
    only calls ``future.exception()``, which is loop-independent.
    """

    async def _build() -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.Future()
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(None)
        return future

    return asyncio.run(_build())


class TestLogAsyncEmitError:
    def test_logs_exception_when_present(self, caplog: pytest.LogCaptureFixture) -> None:
        future: asyncio.Future[None] = _completed_future(exc=ValueError("boom"))
        with caplog.at_level(logging.ERROR, logger="modulo.core.logging_config"):
            _log_async_emit_error(future)
        assert any("async_emit_failed" in r.getMessage() for r in caplog.records)

    def test_no_exception_no_log(self, caplog: pytest.LogCaptureFixture) -> None:
        future: asyncio.Future[None] = _completed_future()
        with caplog.at_level(logging.ERROR, logger="modulo.core.logging_config"):
            _log_async_emit_error(future)
        assert not caplog.records


class TestErrorTrackingLogHandlerEmit:
    @pytest.fixture(autouse=True)
    def _clean_last_write_times(self) -> None:
        ErrorTrackingLogHandler._last_write_time.clear()
        yield
        ErrorTrackingLogHandler._last_write_time.clear()

    def test_filters_records_below_level(self) -> None:
        handler = ErrorTrackingLogHandler(level=logging.ERROR)
        handler.emit(_record(level=logging.WARNING))
        assert handler._pending_tasks == 0

    def test_no_org_id_returns_without_task(self) -> None:
        handler = ErrorTrackingLogHandler()
        token = org_id_var.set(None)
        try:
            handler.emit(_record(level=logging.ERROR))
        finally:
            org_id_var.reset(token)
        assert handler._pending_tasks == 0

    def test_backlog_full_drops_record(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = ErrorTrackingLogHandler()
        handler._pending_tasks = handler._backlog_limit
        token = org_id_var.set("org-1")
        try:
            with caplog.at_level(logging.WARNING, logger="modulo.core.logging_config"):
                handler.emit(_record(level=logging.ERROR))
        finally:
            org_id_var.reset(token)
        assert handler._pending_tasks == handler._backlog_limit
        assert any("backlog_full" in r.getMessage() for r in caplog.records)

    def test_rate_limited_within_window(self) -> None:
        handler = ErrorTrackingLogHandler()
        ErrorTrackingLogHandler._last_write_time["org-1"] = time.time()
        token = org_id_var.set("org-1")
        try:
            handler.emit(_record(level=logging.ERROR))
        finally:
            org_id_var.reset(token)
        assert handler._pending_tasks == 0

    async def test_creates_task_and_manages_pending_count(self) -> None:
        handler = ErrorTrackingLogHandler()
        emitted = False

        async def fake_async_emit(_record: logging.LogRecord) -> None:
            nonlocal emitted
            emitted = True

        handler._async_emit = fake_async_emit  # type: ignore[method-assign]
        token = org_id_var.set("org-1")
        try:
            handler.emit(_record(level=logging.ERROR))
            await asyncio.sleep(0)
            assert emitted is True
            assert handler._pending_tasks == 1
            await asyncio.sleep(0)
            assert handler._pending_tasks == 0
        finally:
            org_id_var.reset(token)

    def test_no_running_loop_is_swallowed(self) -> None:
        """Outside an event loop, create_task raises RuntimeError — emit must not crash."""
        handler = ErrorTrackingLogHandler()
        token = org_id_var.set("org-1")
        try:
            handler.emit(_record(level=logging.ERROR))
        finally:
            org_id_var.reset(token)
        assert handler._pending_tasks == 0


class TestErrorTrackingLogHandlerAsyncEmit:
    _ORG = "11111111-2222-3333-4444-555555555555"

    def _patch_deps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        service: MagicMock,
        session: MagicMock,
    ) -> None:
        from modulo import version as version_mod
        from modulo.api import dependencies as deps_mod
        from modulo.core import error_tracking as et_mod
        from modulo.db import rls as rls_mod

        monkeypatch.setattr(deps_mod, "get_or_create_engine", MagicMock(return_value=AsyncMock()))
        monkeypatch.setattr(
            deps_mod,
            "get_or_create_session_factory",
            MagicMock(return_value=MagicMock(return_value=_AsyncCtx(session))),
        )
        monkeypatch.setattr(et_mod, "ErrorIngestionService", MagicMock(return_value=service))
        monkeypatch.setattr(rls_mod, "set_rls_org", AsyncMock())
        monkeypatch.setattr(version_mod, "get_version", MagicMock(return_value="9.9.9"))

    def _make_session(self) -> MagicMock:
        session = MagicMock()
        session.begin.return_value = _AsyncCtx("tx")
        return session

    async def test_happy_path_ingests_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = MagicMock()
        service.ingest = AsyncMock()
        session = self._make_session()
        self._patch_deps(monkeypatch, service, session)
        handler = ErrorTrackingLogHandler()
        token = org_id_var.set(self._ORG)
        try:
            await handler._async_emit(_record(level=logging.CRITICAL, msg="boom"))
        finally:
            org_id_var.reset(token)

        service.ingest.assert_awaited_once()
        args = service.ingest.await_args.args
        assert args[1] == uuid.UUID(self._ORG)
        event = args[2]
        assert event["level"] == "critical"
        assert event["message"] == "boom"
        assert event["source"] == "backend"
        assert event["stacktrace"] is None
        assert event["version"] == "9.9.9"
        assert event["context_json"]["logger"] == "test.logging_config"
        assert event["context_json"]["line"] == 1

    @pytest.mark.parametrize(
        ("record_level", "expected"),
        [
            (logging.ERROR, "error"),
            (logging.WARNING, "warning"),
            (logging.CRITICAL, "critical"),
        ],
    )
    async def test_level_mapping(
        self,
        monkeypatch: pytest.MonkeyPatch,
        record_level: int,
        expected: str,
    ) -> None:
        service = MagicMock()
        service.ingest = AsyncMock()
        session = self._make_session()
        self._patch_deps(monkeypatch, service, session)
        handler = ErrorTrackingLogHandler()
        token = org_id_var.set(self._ORG)
        try:
            await handler._async_emit(_record(level=record_level))
        finally:
            org_id_var.reset(token)
        assert service.ingest.await_args.args[2]["level"] == expected

    async def test_stacktrace_formatted_from_exc_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()

        service = MagicMock()
        service.ingest = AsyncMock()
        session = self._make_session()
        self._patch_deps(monkeypatch, service, session)
        handler = ErrorTrackingLogHandler()
        token = org_id_var.set(self._ORG)
        try:
            await handler._async_emit(_record(level=logging.ERROR, exc_info=exc_info))
        finally:
            org_id_var.reset(token)
        assert "ValueError" in service.ingest.await_args.args[2]["stacktrace"]

    async def test_stacktrace_uses_preformatted_exc_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A record with exc_text already set must use it instead of re-formatting."""
        service = MagicMock()
        service.ingest = AsyncMock()
        session = self._make_session()
        self._patch_deps(monkeypatch, service, session)
        handler = ErrorTrackingLogHandler()
        record = _record(level=logging.ERROR)
        record.exc_text = "preformatted traceback"
        token = org_id_var.set(self._ORG)
        try:
            await handler._async_emit(record)
        finally:
            org_id_var.reset(token)
        assert service.ingest.await_args.args[2]["stacktrace"] == "preformatted traceback"

    async def test_invalid_org_id_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        service = MagicMock()
        service.ingest = AsyncMock()
        session = self._make_session()
        self._patch_deps(monkeypatch, service, session)
        handler = ErrorTrackingLogHandler()
        token = org_id_var.set("not-a-uuid")
        try:
            with caplog.at_level(logging.WARNING, logger="modulo.core.logging_config"):
                await handler._async_emit(_record(level=logging.ERROR))
        finally:
            org_id_var.reset(token)
        assert any("invalid organisation ID" in r.getMessage() for r in caplog.records)
        service.ingest.assert_not_awaited()

    async def test_none_org_id_returns_early(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = MagicMock()
        service.ingest = AsyncMock()
        session = self._make_session()
        self._patch_deps(monkeypatch, service, session)
        handler = ErrorTrackingLogHandler()
        token = org_id_var.set(None)
        try:
            await handler._async_emit(_record(level=logging.ERROR))
        finally:
            org_id_var.reset(token)
        service.ingest.assert_not_awaited()

    async def test_programming_error_is_logged_and_re_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from modulo.db import rls as rls_mod

        service = MagicMock()
        service.ingest = AsyncMock()
        session = self._make_session()
        self._patch_deps(monkeypatch, service, session)
        monkeypatch.setattr(
            rls_mod, "set_rls_org", AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception("db down")))
        )
        handler = ErrorTrackingLogHandler()
        token = org_id_var.set(self._ORG)
        try:
            with caplog.at_level(logging.ERROR, logger="modulo.core.logging_config"):
                await handler._async_emit(_record(level=logging.ERROR))
        finally:
            org_id_var.reset(token)
        assert any("Database unavailable" in r.getMessage() for r in caplog.records)
        assert any("ingest_failed" in r.getMessage() for r in caplog.records)
        service.ingest.assert_not_awaited()

    async def test_unexpected_exception_is_logged_not_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from modulo import version as version_mod

        service = MagicMock()
        service.ingest = AsyncMock()
        session = self._make_session()
        self._patch_deps(monkeypatch, service, session)
        monkeypatch.setattr(version_mod, "get_version", MagicMock(side_effect=RuntimeError("boom")))
        handler = ErrorTrackingLogHandler()
        token = org_id_var.set(self._ORG)
        try:
            with caplog.at_level(logging.ERROR, logger="modulo.core.logging_config"):
                await handler._async_emit(_record(level=logging.ERROR))
        finally:
            org_id_var.reset(token)
        assert any("ingest_failed" in r.getMessage() for r in caplog.records)
        service.ingest.assert_not_awaited()


class TestApplyPerModuleLevels:
    def test_sets_level_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_LOG_LEVEL_MODULO_CORE_FOO", "WARNING")
        logger = logging.getLogger("modulo.core.foo")
        logger.setLevel(logging.NOTSET)
        try:
            _apply_per_module_levels()
            assert logger.level == logging.WARNING
        finally:
            logger.setLevel(logging.NOTSET)

    def test_unknown_level_falls_back_to_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_LOG_LEVEL_MODULO_CORE_FOO", "BOGUSLEVEL")
        logger = logging.getLogger("modulo.core.foo")
        logger.setLevel(logging.NOTSET)
        try:
            _apply_per_module_levels()
            assert logger.level == logging.INFO
        finally:
            logger.setLevel(logging.NOTSET)

    def test_duplicate_module_paths_deduped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_LOG_LEVEL_MODULO_CORE_FOO_BAR", "ERROR")
        monkeypatch.setenv("MODULO_LOG_LEVEL_MODULO_CORE_FOO.BAR", "DEBUG")
        logger = logging.getLogger("modulo.core.foo.bar")
        logger.setLevel(logging.NOTSET)
        try:
            _apply_per_module_levels()
            assert logger.level == logging.ERROR
        finally:
            logger.setLevel(logging.NOTSET)


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
