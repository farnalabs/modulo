"""Tests for backend error capture hooks: CatchAllMiddleware, log handler."""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.testclient import TestClient

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# =========================================================================
# CatchAllMiddleware
# =========================================================================


class _ErrorRaisingMiddleware(BaseHTTPMiddleware):
    """Middleware that always raises an exception — used to trigger CatchAllMiddleware."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        raise RuntimeError("test crash")


def _make_app(with_error_middleware: bool = False) -> FastAPI:
    from modulo.api.middleware.catch_all import CatchAllMiddleware

    app = FastAPI()
    if with_error_middleware:
        app.add_middleware(_ErrorRaisingMiddleware)
    app.add_middleware(CatchAllMiddleware)

    @app.get("/test")
    async def test_route() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/crash")
    async def crash_route() -> None:
        raise ValueError("boom")

    return app


class TestCatchAllMiddleware:
    @patch("modulo.api.middleware.catch_all._ingest_unhandled_error")
    def test_returns_500_on_unhandled_exception(self, mock_ingest: Any) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/crash")
        assert resp.status_code == 500
        body = resp.json()
        assert body["type"] == "urn:problem:modulo:internal_error"
        assert body["title"] == "Internal Error"
        assert body["status"] == 500

    @patch("modulo.api.middleware.catch_all._ingest_unhandled_error")
    def test_healthy_route_passes_through(self, mock_ingest: Any) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.status_code == 200
        assert resp.json() == {"ok": "yes"}

    @patch("modulo.api.middleware.catch_all._ingest_unhandled_error")
    def test_calls_ingest_on_error(self, mock_ingest: Any) -> None:
        app = _make_app()
        client = TestClient(app)
        client.get("/crash")
        mock_ingest.assert_awaited_once()

    @patch("modulo.api.middleware.catch_all._ingest_unhandled_error")
    def test_crash_in_ingest_does_not_crash_response(self, mock_ingest: Any) -> None:
        mock_ingest.side_effect = RuntimeError("ingest crashed too")
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/crash")
        assert resp.status_code == 500

    @patch("modulo.api.middleware.catch_all._ingest_unhandled_error")
    def test_ingest_receives_request_context(self, mock_ingest: Any) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/crash")
        assert resp.status_code == 500
        mock_ingest.assert_awaited_once()
        request = mock_ingest.await_args.args[0]
        assert request.method == "GET"
        assert request.url.path == "/crash"


# =========================================================================
# ErrorTrackingLogHandler
# =========================================================================


class TestErrorTrackingLogHandler:
    @pytest.fixture(autouse=True)
    def _reset_rate_limit_state(self) -> Any:
        from modulo.core.logging_config import ErrorTrackingLogHandler

        ErrorTrackingLogHandler._last_write_time.clear()
        yield
        ErrorTrackingLogHandler._last_write_time.clear()

    @staticmethod
    def _make_record(level: int, msg: str = "test msg") -> logging.LogRecord:
        return logging.LogRecord(
            name="test",
            level=level,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )

    @staticmethod
    async def _drain() -> None:
        for _ in range(3):
            await asyncio.sleep(0)

    def test_skips_below_error(self) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler

        handler = ErrorTrackingLogHandler()
        with patch.object(ErrorTrackingLogHandler, "_async_emit", new_callable=AsyncMock) as mock_async:
            handler.emit(self._make_record(logging.INFO, "info msg"))
        mock_async.assert_not_called()
        assert handler._pending_tasks == 0

    def test_skips_when_no_org_context(self) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler

        handler = ErrorTrackingLogHandler()
        with patch.object(ErrorTrackingLogHandler, "_async_emit", new_callable=AsyncMock) as mock_async:
            handler.emit(self._make_record(logging.ERROR, "error msg"))
        mock_async.assert_not_called()
        assert handler._pending_tasks == 0

    def test_skips_warning_level(self) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

        token = org_id_var.set(str(_ORG_ID))
        try:
            handler = ErrorTrackingLogHandler()
            with patch.object(ErrorTrackingLogHandler, "_async_emit", new_callable=AsyncMock) as mock_async:
                handler.emit(self._make_record(logging.WARNING, "warn msg"))
        finally:
            org_id_var.reset(token)

        mock_async.assert_not_awaited()
        mock_async.assert_not_called()

    async def test_captures_error_level(self) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

        record = self._make_record(logging.ERROR, "error msg")
        token = org_id_var.set(str(_ORG_ID))
        try:
            handler = ErrorTrackingLogHandler()
            with patch.object(ErrorTrackingLogHandler, "_async_emit", new_callable=AsyncMock) as mock_async:
                handler.emit(record)
                await self._drain()
        finally:
            org_id_var.reset(token)

        mock_async.assert_awaited_once()
        mock_async.assert_awaited_with(record)
        assert handler._pending_tasks == 0

    async def test_captures_critical_level(self) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

        record = self._make_record(logging.CRITICAL, "critical msg")
        token = org_id_var.set(str(_ORG_ID))
        try:
            handler = ErrorTrackingLogHandler()
            with patch.object(ErrorTrackingLogHandler, "_async_emit", new_callable=AsyncMock) as mock_async:
                handler.emit(record)
                await self._drain()
        finally:
            org_id_var.reset(token)

        mock_async.assert_awaited_once()
        mock_async.assert_awaited_with(record)

    async def test_drops_second_emit_during_rate_window(self) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

        first = self._make_record(logging.ERROR, "first")
        second = self._make_record(logging.ERROR, "second")
        token = org_id_var.set(str(_ORG_ID))
        try:
            handler = ErrorTrackingLogHandler()
            with patch.object(ErrorTrackingLogHandler, "_async_emit", new_callable=AsyncMock) as mock_async:
                handler.emit(first)
                await self._drain()
                handler.emit(second)
                await self._drain()
        finally:
            org_id_var.reset(token)

        mock_async.assert_awaited_once()
        mock_async.assert_awaited_with(first)

    def test_drops_emit_when_backlog_full(self) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

        token = org_id_var.set(str(_ORG_ID))
        try:
            handler = ErrorTrackingLogHandler()
            handler._pending_tasks = handler._backlog_limit
            with patch.object(ErrorTrackingLogHandler, "_async_emit", new_callable=AsyncMock) as mock_async:
                handler.emit(self._make_record(logging.ERROR, "dropped"))
        finally:
            org_id_var.reset(token)

        mock_async.assert_not_called()


class TestAsyncEmitEventData:
    """Verifies the real ``_async_emit`` builds the expected event data.

    The DB chain is mocked, so we can assert on the exact ``event_data`` dict
    that would be handed to ErrorIngestionService.
    """

    @pytest.fixture()
    def async_emit_chain(self, monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock, MagicMock]:
        """Install mock DB chain for ``_async_emit``; return ``(service, set_rls, session)`` mocks."""
        session = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock()
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin.return_value = begin_cm
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=cm)
        service = AsyncMock()
        set_rls = AsyncMock()

        monkeypatch.setattr("modulo.api.dependencies.get_or_create_engine", MagicMock())
        monkeypatch.setattr("modulo.api.dependencies.get_or_create_session_factory", MagicMock(return_value=factory))
        monkeypatch.setattr(
            "modulo.core.error_tracking.ErrorIngestionService",
            MagicMock(return_value=MagicMock(ingest=service)),
        )
        monkeypatch.setattr("modulo.db.rls.set_rls_org", set_rls)
        monkeypatch.setattr("modulo.settings.get_settings", MagicMock())
        monkeypatch.setattr("modulo.version.get_version", MagicMock(return_value="1.2.3"))

        return service, set_rls, session

    @staticmethod
    def _make_record(level: int, msg: str = "test msg") -> logging.LogRecord:
        return logging.LogRecord(
            name="test.logger",
            level=level,
            pathname="pipeline.py",
            lineno=42,
            msg=msg,
            args=(),
            exc_info=None,
        )

    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (logging.WARNING, "warning"),
            (logging.ERROR, "error"),
            (logging.CRITICAL, "critical"),
        ],
    )
    async def test_level_mapping(
        self,
        async_emit_chain: tuple[AsyncMock, AsyncMock, MagicMock],
        level: int,
        expected: str,
    ) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

        service, set_rls, _session = async_emit_chain
        token = org_id_var.set(str(_ORG_ID))
        try:
            handler = ErrorTrackingLogHandler()
            await handler._async_emit(self._make_record(level))
        finally:
            org_id_var.reset(token)

        service.assert_awaited_once()
        assert service.await_args.args[2]["level"] == expected
        assert service.await_args.args[1] == _ORG_ID
        set_rls.assert_awaited_once_with(_session, _ORG_ID)

    async def test_skips_emit_when_org_id_invalid(
        self,
        async_emit_chain: tuple[AsyncMock, AsyncMock, MagicMock],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Invalid org id in the context var must not reach the DB chain."""
        from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

        service, set_rls, _session = async_emit_chain
        token = org_id_var.set("not-a-valid-uuid")
        try:
            handler = ErrorTrackingLogHandler()
            await handler._async_emit(self._make_record(logging.ERROR, "boom"))
        finally:
            org_id_var.reset(token)

        service.assert_not_awaited()
        set_rls.assert_not_awaited()
        assert any("Ignoring log event with invalid organisation ID" in r.message for r in caplog.records)

    async def test_error_level_builds_event_data(
        self,
        async_emit_chain: tuple[AsyncMock, AsyncMock, MagicMock],
    ) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

        service, set_rls, _session = async_emit_chain
        token = org_id_var.set(str(_ORG_ID))
        try:
            handler = ErrorTrackingLogHandler()
            await handler._async_emit(self._make_record(logging.ERROR, "boom"))
        finally:
            org_id_var.reset(token)

        service.assert_awaited_once()
        event_data = service.await_args.args[2]
        assert event_data["message"] == "boom"
        assert event_data["source"] == "backend"
        assert event_data["environment"] == "development"
        assert event_data["version"] == "1.2.3"
        assert event_data["context_json"]["logger"] == "test.logger"
        assert event_data["context_json"]["module"] == "pipeline"
        assert event_data["context_json"]["line"] == 42
        set_rls.assert_awaited_once_with(_session, _ORG_ID)

    async def test_message_formatting_with_args(
        self,
        async_emit_chain: tuple[AsyncMock, AsyncMock, MagicMock],
    ) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

        service, _set_rls, _session = async_emit_chain
        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname="pipeline.py",
            lineno=7,
            msg="failed to process %s in %dms",
            args=("pipeline-a", 123),
            exc_info=None,
        )
        token = org_id_var.set(str(_ORG_ID))
        try:
            handler = ErrorTrackingLogHandler()
            await handler._async_emit(record)
        finally:
            org_id_var.reset(token)

        service.assert_awaited_once()
        assert service.await_args.args[2]["message"] == "failed to process pipeline-a in 123ms"

    async def test_environment_from_env_var(
        self,
        async_emit_chain: tuple[AsyncMock, AsyncMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

        monkeypatch.setenv("MODULO_ENV", "production")
        service, _set_rls, _session = async_emit_chain
        token = org_id_var.set(str(_ORG_ID))
        try:
            handler = ErrorTrackingLogHandler()
            await handler._async_emit(self._make_record(logging.ERROR))
        finally:
            org_id_var.reset(token)

        service.assert_awaited_once()
        assert service.await_args.args[2]["environment"] == "production"

    async def test_stacktrace_from_exc_info(
        self,
        async_emit_chain: tuple[AsyncMock, AsyncMock, MagicMock],
    ) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

        service, _set_rls, _session = async_emit_chain
        try:
            raise ValueError("kaboom")
        except ValueError:
            record = logging.LogRecord(
                name="test.logger",
                level=logging.ERROR,
                pathname="pipeline.py",
                lineno=7,
                msg="failed",
                args=(),
                exc_info=sys.exc_info(),
            )
        token = org_id_var.set(str(_ORG_ID))
        try:
            handler = ErrorTrackingLogHandler()
            await handler._async_emit(record)
        finally:
            org_id_var.reset(token)

        service.assert_awaited_once()
        assert "ValueError: kaboom" in service.await_args.args[2]["stacktrace"]


class TestEnrichment:
    @pytest.fixture()
    def ingest_chain(self, monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock, MagicMock]:
        """Install mock DB chain for ``_ingest_unhandled_error``; return ``(service, set_rls, session)`` mocks."""
        session = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock()
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin.return_value = begin_cm
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=cm)
        service = AsyncMock()
        set_rls = AsyncMock()

        monkeypatch.setattr("modulo.api.dependencies.get_or_create_engine", MagicMock())
        monkeypatch.setattr("modulo.api.dependencies.get_or_create_session_factory", MagicMock(return_value=factory))
        monkeypatch.setattr(
            "modulo.core.error_tracking.ErrorIngestionService",
            MagicMock(return_value=MagicMock(ingest=service)),
        )
        monkeypatch.setattr("modulo.db.rls.set_rls_org", set_rls)
        monkeypatch.setattr("modulo.settings.get_settings", MagicMock())
        monkeypatch.setattr("modulo.api.middleware.catch_all.get_version", MagicMock(return_value="1.2.3"))

        return service, set_rls, session

    @staticmethod
    def _crash_request(org_id: uuid.UUID = _ORG_ID) -> Request:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/crash",
                "headers": [],
                "query_string": b"",
                "client": ("127.0.0.1", 50000),
                "server": ("testserver", 80),
                "scheme": "http",
                "state": {},
            }
        )
        request.state.organisation_id = str(org_id)
        return request

    async def test_ingest_receives_version_and_environment(
        self,
        ingest_chain: tuple[AsyncMock, AsyncMock, MagicMock],
    ) -> None:
        from modulo.api.middleware.catch_all import _ingest_unhandled_error

        service, set_rls, session = ingest_chain
        await _ingest_unhandled_error(self._crash_request())

        service.assert_awaited_once()
        event_data = service.await_args.args[2]
        assert event_data["version"] == "1.2.3"
        assert event_data["environment"] == "development"
        assert event_data["message"] == "Unhandled exception: GET /crash"
        assert event_data["context_json"]["method"] == "GET"
        assert event_data["context_json"]["path"] == "/crash"
        assert service.await_args.args[1] == str(_ORG_ID)
        set_rls.assert_awaited_once()
        assert set_rls.await_args.args[0] is session
        assert set_rls.await_args.args[1] == str(_ORG_ID)

    async def test_ingest_environment_reflects_env_var(
        self,
        ingest_chain: tuple[AsyncMock, AsyncMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modulo.api.middleware.catch_all import _ingest_unhandled_error

        monkeypatch.setenv("MODULO_ENV", "staging")
        service, _set_rls, _session = ingest_chain
        await _ingest_unhandled_error(self._crash_request())

        service.assert_awaited_once()
        assert service.await_args.args[2]["environment"] == "staging"
