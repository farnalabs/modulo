"""Unit tests for ModelBackendHub lifecycle, lookup errors, health checks.

Requires no DB — uses StubModelBackend and AsyncMock-backed doubles.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest

from modulo.core.model_backend_hub import (
    BackendDecryptError,
    BackendNotFoundError,
    BackendUnavailableError,
    ModelBackendHub,
)
from modulo.model_backends.base import HealthResult
from modulo.model_backends.stub import StubModelBackend


@pytest.fixture
async def hub() -> AsyncGenerator[ModelBackendHub, None]:
    async with ModelBackendHub() as h:
        yield h


@pytest.fixture
def backend() -> StubModelBackend:
    return StubModelBackend()


# ---------------------------------------------------------------------------
# Exception constructors — messages carry the backend id
# ---------------------------------------------------------------------------


def test_backend_not_found_error_message() -> None:
    bid = uuid.uuid4()
    err = BackendNotFoundError(bid)
    assert str(err) == f"Backend {bid} not found"
    assert err.backend_id == bid


def test_backend_unavailable_error_message() -> None:
    bid = uuid.uuid4()
    err = BackendUnavailableError(bid)
    assert str(err) == f"No healthy backend available; requested {bid}"
    assert err.backend_id == bid


def test_backend_decrypt_error_message() -> None:
    bid = uuid.uuid4()
    err = BackendDecryptError(bid)
    assert str(err) == f"Failed to decrypt credentials for model backend {bid}"
    assert err.backend_id == bid


# ---------------------------------------------------------------------------
# Context manager lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_aexit_clears_all_backend_state(
    hub: ModelBackendHub,
    backend: StubModelBackend,
) -> None:
    bid = uuid.uuid4()
    hub.register(bid, backend)
    hub._fallbacks[bid] = [uuid.uuid4()]
    await hub.__aexit__(None, None, None)
    assert not hub._backends
    assert not hub._healthy
    assert not hub._fallbacks


@pytest.mark.anyio
async def test_aexit_with_exception_logs_and_clears(
    caplog: pytest.LogCaptureFixture,
    backend: StubModelBackend,
) -> None:
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    with pytest.raises(RuntimeError, match="boom"):
        async with hub:
            hub.register(bid, backend)
            raise RuntimeError("boom")
    assert not hub._backends
    assert "ModelBackendHub exiting due to error" in caplog.text


def test_register_overwrite_warns_and_replaces(
    backend: StubModelBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = ModelBackendHub()
    bid = uuid.uuid4()
    replacement = StubModelBackend()
    hub.register(bid, backend)
    hub.register(bid, replacement)
    assert hub._backends[bid] is replacement
    assert "Overwriting already registered backend" in caplog.text


def test_backend_ids_property(hub: ModelBackendHub, backend: StubModelBackend) -> None:
    bid = uuid.uuid4()
    assert not hub.backend_ids
    hub.register(bid, backend)
    assert hub.backend_ids == frozenset({bid})


# ---------------------------------------------------------------------------
# get() / get_with_rotation() — unknown backend
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_unknown_backend_raises_not_found(hub: ModelBackendHub) -> None:
    bid = uuid.uuid4()
    with pytest.raises(BackendNotFoundError):
        await hub.get(bid)


@pytest.mark.anyio
async def test_get_with_rotation_unknown_backend_raises_unavailable(hub: ModelBackendHub) -> None:
    bid = uuid.uuid4()
    with pytest.raises(BackendUnavailableError):
        await hub.get_with_rotation(bid)


@pytest.mark.anyio
async def test_get_with_rotation_no_healthy_backend_anywhere(
    hub: ModelBackendHub,
    backend: StubModelBackend,
) -> None:
    bid = uuid.uuid4()
    other = uuid.uuid4()
    hub.register(bid, backend)
    hub.register(other, StubModelBackend())
    hub.mark_unhealthy(bid)
    hub.mark_unhealthy(other)
    with pytest.raises(BackendUnavailableError):
        await hub.get_with_rotation(bid)


# ---------------------------------------------------------------------------
# mark_unhealthy()
# ---------------------------------------------------------------------------


def test_mark_unhealthy_unknown_backend_raises(hub: ModelBackendHub) -> None:
    with pytest.raises(BackendNotFoundError):
        hub.mark_unhealthy(uuid.uuid4())


@pytest.mark.anyio
async def test_mark_unhealthy_then_get_uses_fallback(
    hub: ModelBackendHub,
    backend: StubModelBackend,
) -> None:
    primary, fallback = uuid.uuid4(), uuid.uuid4()
    hub.register(primary, backend)
    hub.register(fallback, StubModelBackend())
    hub.mark_unhealthy(primary)
    hub._fallbacks[primary] = [fallback]
    result = await hub.get(primary)
    assert result is hub._backends[fallback]


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_health_check_unknown_backend_returns_not_ok(hub: ModelBackendHub) -> None:
    result = await hub.health_check(uuid.uuid4())
    assert result == HealthResult(ok=False, detail="Backend not registered")


@pytest.mark.anyio
async def test_health_check_marks_backend_healthy(hub: ModelBackendHub) -> None:
    bid = uuid.uuid4()
    fake = AsyncMock()
    fake.health_check = AsyncMock(return_value=HealthResult(ok=True, detail="ok"))
    hub.register(bid, fake)
    result = await hub.health_check(bid)
    assert result.ok is True
    assert hub._healthy[bid] is True


@pytest.mark.anyio
async def test_health_check_marks_backend_unhealthy_on_failure(hub: ModelBackendHub) -> None:
    bid = uuid.uuid4()
    fake = AsyncMock()
    fake.health_check = AsyncMock(side_effect=RuntimeError("backend on fire"))
    hub.register(bid, fake)
    result = await hub.health_check(bid)
    assert result.ok is False
    assert result.detail == "backend on fire"
    assert hub._healthy[bid] is False


@pytest.mark.anyio
async def test_health_check_truncates_long_error_detail(hub: ModelBackendHub) -> None:
    bid = uuid.uuid4()
    fake = AsyncMock()
    fake.health_check = AsyncMock(side_effect=RuntimeError("x" * 2000))
    hub.register(bid, fake)
    result = await hub.health_check(bid)
    assert result.ok is False
    assert len(result.detail) == 500
    assert hub._healthy[bid] is False


@pytest.mark.anyio
async def test_health_check_timeout_marks_unhealthy(
    hub: ModelBackendHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bid = uuid.uuid4()
    fake = AsyncMock()

    async def _slow() -> HealthResult:
        await asyncio.sleep(5)
        return HealthResult(ok=True, detail="late")

    fake.health_check = _slow
    hub.register(bid, fake)
    monkeypatch.setattr("modulo.core.model_backend_hub._HEALTH_CHECK_TIMEOUT", 0.05)
    result = await hub.health_check(bid)
    assert result == HealthResult(ok=False, detail="Health check timed out")
    assert hub._healthy[bid] is False


@pytest.mark.anyio
async def test_health_check_propagates_cancellation(hub: ModelBackendHub) -> None:
    bid = uuid.uuid4()
    fake = AsyncMock()
    fake.health_check = AsyncMock(side_effect=asyncio.CancelledError())
    hub.register(bid, fake)
    with pytest.raises(asyncio.CancelledError):
        await hub.health_check(bid)


# ---------------------------------------------------------------------------
# Failover audit event cancellation propagation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_propagates_audit_logger_cancellation(
    hub: ModelBackendHub,
    backend: StubModelBackend,
) -> None:
    primary, fallback = uuid.uuid4(), uuid.uuid4()
    hub.register(primary, backend)
    hub.register(fallback, StubModelBackend())
    hub.mark_unhealthy(primary)
    hub._fallbacks[primary] = [fallback]

    async def _cancel(_event: dict) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await hub.get(primary, audit_logger=_cancel)
