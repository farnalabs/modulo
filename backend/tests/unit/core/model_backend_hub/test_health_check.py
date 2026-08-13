"""Unit tests for ModelBackendHub lifecycle and health-check behaviour.

Covers ``health_check`` (registered/unregistered, timeout, exception
truncation, cancellation), ``mark_unhealthy``, ``backend_ids``, the error
classes, ``__aexit__`` cleanup, register-overwrite, and the not-registered
paths of ``get`` / ``get_with_rotation`` / ``_emit_failover_event``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.model_backend_hub import (
    BackendDecryptError,
    BackendNotFoundError,
    BackendUnavailableError,
    ModelBackendHub,
)
from modulo.model_backends.base import HealthResult


def _registered_hub(*backends: tuple[uuid.UUID, MagicMock]) -> ModelBackendHub:
    hub = ModelBackendHub()
    for bid, backend in backends:
        hub.register(bid, backend)
    return hub


class TestErrorClasses:
    def test_backend_not_found_error(self) -> None:
        bid = uuid.uuid4()
        exc = BackendNotFoundError(bid)
        assert exc.backend_id == bid
        assert str(bid) in str(exc)

    def test_backend_unavailable_error(self) -> None:
        bid = uuid.uuid4()
        exc = BackendUnavailableError(bid)
        assert str(bid) in str(exc)

    def test_backend_decrypt_error(self) -> None:
        bid = uuid.uuid4()
        exc = BackendDecryptError(bid)
        assert exc.backend_id == bid
        assert str(bid) in str(exc)


class TestHealthCheck:
    async def test_unregistered_backend_reports_not_registered(self) -> None:
        hub = ModelBackendHub()
        result = await hub.health_check(uuid.uuid4())
        assert result.ok is False
        assert result.detail == "Backend not registered"

    async def test_healthy_backend_updates_state(self) -> None:
        bid = uuid.uuid4()
        backend = MagicMock()
        backend.health_check = AsyncMock(return_value=HealthResult(ok=True, detail="ok"))
        hub = _registered_hub((bid, backend))

        result = await hub.health_check(bid)

        assert result.ok is True
        assert hub._healthy[bid] is True

    async def test_unhealthy_backend_updates_state(self) -> None:
        bid = uuid.uuid4()
        backend = MagicMock()
        backend.health_check = AsyncMock(return_value=HealthResult(ok=False, detail="degraded"))
        hub = _registered_hub((bid, backend))

        result = await hub.health_check(bid)

        assert result.ok is False
        assert hub._healthy[bid] is False

    async def test_timeout_marks_unhealthy(self) -> None:
        bid = uuid.uuid4()
        backend = MagicMock()
        backend.health_check = AsyncMock(side_effect=TimeoutError())
        hub = _registered_hub((bid, backend))

        result = await hub.health_check(bid)

        assert result.ok is False
        assert result.detail == "Health check timed out"
        assert hub._healthy[bid] is False

    async def test_exception_marks_unhealthy_and_truncates_detail(self) -> None:
        bid = uuid.uuid4()
        backend = MagicMock()
        backend.health_check = AsyncMock(side_effect=RuntimeError("x" * 1000))
        hub = _registered_hub((bid, backend))

        result = await hub.health_check(bid)

        assert result.ok is False
        assert len(result.detail) <= 500
        assert hub._healthy[bid] is False

    async def test_cancellation_propagates(self) -> None:
        bid = uuid.uuid4()
        backend = MagicMock()
        backend.health_check = AsyncMock(side_effect=asyncio.CancelledError())
        hub = _registered_hub((bid, backend))

        with pytest.raises(asyncio.CancelledError):
            await hub.health_check(bid)


class TestMarkUnhealthy:
    async def test_unknown_backend_raises(self) -> None:
        hub = ModelBackendHub()
        with pytest.raises(BackendNotFoundError):
            hub.mark_unhealthy(uuid.uuid4())

    async def test_marks_healthy_backend_unhealthy(self) -> None:
        bid = uuid.uuid4()
        hub = _registered_hub((bid, MagicMock()))
        assert hub._healthy[bid] is True

        hub.mark_unhealthy(bid)

        assert hub._healthy[bid] is False


class TestBackendIds:
    def test_returns_registered_ids(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        hub = _registered_hub((a, MagicMock()), (b, MagicMock()))
        assert hub.backend_ids == frozenset({a, b})

    def test_empty_when_nothing_registered(self) -> None:
        assert ModelBackendHub().backend_ids == frozenset()


class TestLifecycle:
    async def test_aexit_clears_state(self) -> None:
        hub = _registered_hub((uuid.uuid4(), MagicMock()), (uuid.uuid4(), MagicMock()))
        hub._fallbacks[uuid.uuid4()] = [uuid.uuid4()]

        await hub.__aexit__(None, None, None)

        assert hub._backends == {}
        assert hub._healthy == {}
        assert hub._fallbacks == {}

    async def test_aexit_with_error_logs_and_clears(self, caplog) -> None:
        hub = _registered_hub((uuid.uuid4(), MagicMock()))

        with caplog.at_level(logging.ERROR, logger="modulo.core.model_backend_hub"):
            await hub.__aexit__(RuntimeError, RuntimeError("boom"), None)

        assert hub._backends == {}
        assert any("exiting due to error" in r.message for r in caplog.records)

    def test_register_overwrite_warns(self, caplog) -> None:
        hub = ModelBackendHub()
        bid = uuid.uuid4()
        hub.register(bid, MagicMock())

        with caplog.at_level(logging.WARNING, logger="modulo.core.model_backend_hub"):
            hub.register(bid, MagicMock())

        assert any("Overwriting already registered backend" in r.message for r in caplog.records)

    async def test_register_marks_backend_healthy(self) -> None:
        hub = ModelBackendHub()
        bid = uuid.uuid4()
        backend = MagicMock()
        hub.register(bid, backend)
        assert hub._healthy[bid] is True


class TestGetNotFound:
    async def test_get_unregistered_raises_not_found(self) -> None:
        hub = ModelBackendHub()
        with pytest.raises(BackendNotFoundError):
            await hub.get(uuid.uuid4())

    async def test_get_with_rotation_unregistered_raises_unavailable(self) -> None:
        hub = ModelBackendHub()
        with pytest.raises(BackendUnavailableError):
            await hub.get_with_rotation(uuid.uuid4())

    async def test_get_with_rotation_no_healthy_scan_raises(self) -> None:
        primary = uuid.uuid4()
        hub = _registered_hub((primary, MagicMock()))
        hub.mark_unhealthy(primary)

        with pytest.raises(BackendUnavailableError):
            await hub.get_with_rotation(primary)

    async def test_get_with_rotation_skips_unhealthy_scan_candidates(self) -> None:
        primary = uuid.uuid4()
        unhealthy = uuid.uuid4()
        hub = _registered_hub((primary, MagicMock()), (unhealthy, MagicMock()))
        hub.mark_unhealthy(primary)
        hub.mark_unhealthy(unhealthy)

        with pytest.raises(BackendUnavailableError):
            await hub.get_with_rotation(primary)


class TestEmitFailoverEvent:
    async def test_cancel_during_failover_event_propagates(self) -> None:
        primary = uuid.uuid4()
        fallback = uuid.uuid4()
        hub = _registered_hub((primary, MagicMock()), (fallback, MagicMock()))
        hub.mark_unhealthy(primary)
        hub._fallbacks[primary] = [fallback]

        async def _cancel(_event: dict) -> None:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await hub.get(primary, audit_logger=_cancel)

    async def test_audit_logger_failure_is_isolated(self) -> None:
        primary = uuid.uuid4()
        fallback = uuid.uuid4()
        backend = MagicMock()
        hub = _registered_hub((primary, MagicMock()), (fallback, backend))
        hub.mark_unhealthy(primary)
        hub._fallbacks[primary] = [fallback]

        async def _boom(_event: dict) -> None:
            raise RuntimeError("audit down")

        result = await hub.get(primary, audit_logger=_boom)

        assert result is backend
