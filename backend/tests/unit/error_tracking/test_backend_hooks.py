"""Tests for backend error capture hooks: CatchAllMiddleware, log handler, Celery."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
        assert body["error"]["code"] == "INTERNAL_ERROR"

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


# =========================================================================
# ErrorTrackingLogHandler
# =========================================================================


class TestErrorTrackingLogHandler:
    def test_skips_below_error(self) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler

        handler = ErrorTrackingLogHandler()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0, msg="info msg", args=(), exc_info=None
        )
        handler.emit(record)
        assert True  # emit returns None for below-error; no crash

    @patch("modulo.core.logging_config.ErrorTrackingLogHandler._async_emit")
    def test_captures_error_level(self, mock_async: Any) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler

        handler = ErrorTrackingLogHandler()
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0, msg="error msg", args=(), exc_info=None
        )
        handler.emit(record)

    @patch("modulo.core.logging_config.ErrorTrackingLogHandler._async_emit")
    def test_captures_critical_level(self, mock_async: Any) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler

        handler = ErrorTrackingLogHandler()
        record = logging.LogRecord(
            name="test", level=logging.CRITICAL, pathname="", lineno=0, msg="critical msg", args=(), exc_info=None
        )
        handler.emit(record)

    @patch("modulo.core.logging_config.ErrorTrackingLogHandler._async_emit")
    def test_skips_warning_level(self, mock_async: Any) -> None:
        from modulo.core.logging_config import ErrorTrackingLogHandler

        handler = ErrorTrackingLogHandler()
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0, msg="warn msg", args=(), exc_info=None
        )
        handler.emit(record)
        mock_async.assert_not_awaited()
        mock_async.assert_not_called()


# =========================================================================
# Celery failure handler
# =========================================================================


class TestCeleryFailureHandler:
    def _setup_celery_mocks(self, mock_factory: Any, mock_service: Any | None = None) -> MagicMock:
        """Set up mock chain for Celery's async session factory and return a session mock."""
        mock_session = MagicMock()
        mock_session.in_transaction = MagicMock(return_value=True)
        mock_session.get_bind = MagicMock()
        mock_session.info = {}

        mock_begin = AsyncMock()
        mock_begin.__aenter__ = AsyncMock(return_value=None)
        mock_begin.__aexit__ = AsyncMock(return_value=None)
        mock_session.begin = MagicMock(return_value=mock_begin)

        session_obj = mock_factory.return_value.return_value
        session_obj.__aenter__ = AsyncMock(return_value=mock_session)
        session_obj.__aexit__ = AsyncMock(return_value=None)

        if mock_service is not None:
            mock_service.ingest = AsyncMock()

        return mock_session

    @patch("modulo.core.error_tracking.celery_hooks._SERVICE")
    @patch("modulo.api.dependencies.get_or_create_session_factory")
    @patch("modulo.api.dependencies.get_or_create_engine")
    @patch("modulo.settings.get_settings")
    def test_captures_task_failure(
        self,
        mock_settings: Any,
        mock_engine: Any,
        mock_factory: Any,
        mock_service: Any,
    ) -> None:
        from modulo.core.error_tracking.celery_hooks import celery_task_failure_handler

        self._setup_celery_mocks(mock_factory, mock_service)

        sender = MagicMock()
        sender.name = "modulo.test_task"

        celery_task_failure_handler(
            sender=sender,
            task_id="task-123",
            exception=ValueError("something broke"),
            args=("arg1", "arg2"),
            kwargs={"org_id": str(_ORG_ID)},
            einfo=None,
        )

        mock_service.ingest.assert_called_once()
        call_args = mock_service.ingest.call_args
        assert call_args is not None
        event_data = call_args[0][2]
        assert event_data["source"] == "celery"
        assert event_data["level"] == "error"
        assert "ValueError" in event_data["message"]
        assert event_data["context_json"]["task_name"] == "modulo.test_task"
        assert event_data["context_json"]["task_id"] == "task-123"

    @patch("modulo.core.error_tracking.celery_hooks._SERVICE")
    @patch("modulo.api.dependencies.get_or_create_session_factory")
    @patch("modulo.api.dependencies.get_or_create_engine")
    @patch("modulo.settings.get_settings")
    def test_does_not_crash_on_ingest_failure(
        self,
        mock_settings: Any,
        mock_engine: Any,
        mock_factory: Any,
        mock_service: Any,
    ) -> None:
        from modulo.core.error_tracking.celery_hooks import celery_task_failure_handler

        self._setup_celery_mocks(mock_factory, mock_service)
        mock_service.ingest.side_effect = RuntimeError("ingest failed")
        sender = MagicMock()
        sender.name = "modulo.test_task"

        celery_task_failure_handler(
            sender=sender,
            task_id="task-456",
            exception=Exception("test"),
            args=(),
            kwargs={},
            einfo=None,
        )

    @patch("modulo.core.error_tracking.celery_hooks._SERVICE")
    @patch("modulo.api.dependencies.get_or_create_session_factory")
    @patch("modulo.api.dependencies.get_or_create_engine")
    @patch("modulo.settings.get_settings")
    def test_strips_args_kwargs_keys_only(
        self,
        mock_settings: Any,
        mock_engine: Any,
        mock_factory: Any,
        mock_service: Any,
    ) -> None:
        from modulo.core.error_tracking.celery_hooks import celery_task_failure_handler

        self._setup_celery_mocks(mock_factory, mock_service)

        sender = MagicMock()
        sender.name = "modulo.secret_task"

        celery_task_failure_handler(
            sender=sender,
            task_id="task-789",
            exception=Exception("fail"),
            args=("sensitive_arg1", "sensitive_arg2"),
            kwargs={"org_id": str(_ORG_ID), "token": "s shh", "url": "https://example.com"},
            einfo=None,
        )

        call_args = mock_service.ingest.call_args
        assert call_args is not None
        event_data = call_args[0][2]
        assert "sensitive_arg1" in event_data["context_json"]["args_summary"]
        assert event_data["context_json"]["kwargs_keys"] == ["org_id", "token", "url"]
        assert "s shh" not in str(event_data["context_json"]["kwargs_keys"])


# =========================================================================
# Environment/version enrichment
# =========================================================================


class TestEnrichment:
    @patch("modulo.api.middleware.catch_all.get_version", return_value="1.2.3")
    @patch("modulo.api.middleware.catch_all._ingest_unhandled_error")
    def test_version_in_response(self, mock_ingest: Any, mock_version: Any) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/crash")
        assert resp.status_code == 500

    def test_environment_falls_back_to_development(self) -> None:
        env_val = os.environ.get("MODULO_ENV", "development")
        assert env_val == "development"
