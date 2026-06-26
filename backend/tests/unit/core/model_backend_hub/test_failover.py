"""Unit tests for ModelBackendHub failover logic.

Requires no DB — uses StubModelBackend as test double.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.model_backend_hub import BackendUnavailableError, ModelBackendHub
from modulo.model_backends.stub import StubModelBackend


@pytest.fixture()
async def hub() -> AsyncGenerator[ModelBackendHub, None]:
    async with ModelBackendHub() as h:
        yield h


@pytest.fixture()
def backend_a() -> StubModelBackend:
    return StubModelBackend()


@pytest.fixture()
def backend_b() -> StubModelBackend:
    return StubModelBackend()


# ---------------------------------------------------------------------------
# get() — primary healthy
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_returns_healthy_primary(hub: ModelBackendHub, backend_a: StubModelBackend) -> None:
    bid = uuid.uuid4()
    hub.register(bid, backend_a)
    result = await hub.get(bid)
    assert result is backend_a


# ---------------------------------------------------------------------------
# get() — unhealthy primary, no fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_raises_when_unhealthy_no_fallback(hub: ModelBackendHub, backend_a: StubModelBackend) -> None:
    bid = uuid.uuid4()
    hub.register(bid, backend_a)
    hub.mark_unhealthy(bid)
    with pytest.raises(BackendUnavailableError):
        await hub.get(bid)


# ---------------------------------------------------------------------------
# get() — healthy fallback
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_fails_over_to_healthy_fallback(
    hub: ModelBackendHub,
    backend_a: StubModelBackend,
    backend_b: StubModelBackend,
) -> None:
    primary_id = uuid.uuid4()
    fallback_id = uuid.uuid4()
    hub.register(primary_id, backend_a)
    hub.register(fallback_id, backend_b)
    hub.mark_unhealthy(primary_id)
    hub._fallbacks[primary_id] = [fallback_id]

    result = await hub.get(primary_id)
    assert result is backend_b


# ---------------------------------------------------------------------------
# get() — fallback order
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_tries_fallbacks_in_order(hub: ModelBackendHub) -> None:
    primary_id = uuid.uuid4()
    mid_id = uuid.uuid4()
    last_id = uuid.uuid4()
    primary = StubModelBackend()
    mid = StubModelBackend()
    last = StubModelBackend()
    hub.register(primary_id, primary)
    hub.register(mid_id, mid)
    hub.register(last_id, last)
    hub.mark_unhealthy(primary_id)
    hub.mark_unhealthy(mid_id)
    hub._fallbacks[primary_id] = [mid_id, last_id]

    result = await hub.get(primary_id)
    assert result is last


# ---------------------------------------------------------------------------
# get() — all fallbacks unhealthy
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_raises_when_all_fallbacks_unhealthy(
    hub: ModelBackendHub,
    backend_a: StubModelBackend,
    backend_b: StubModelBackend,
) -> None:
    primary_id = uuid.uuid4()
    fallback_id = uuid.uuid4()
    hub.register(primary_id, backend_a)
    hub.register(fallback_id, backend_b)
    hub.mark_unhealthy(primary_id)
    hub.mark_unhealthy(fallback_id)
    hub._fallbacks[primary_id] = [fallback_id]

    with pytest.raises(BackendUnavailableError):
        await hub.get(primary_id)


# ---------------------------------------------------------------------------
# get() — skip unregistered fallback
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_skips_unregistered_fallback(
    hub: ModelBackendHub,
    backend_a: StubModelBackend,
    backend_b: StubModelBackend,
) -> None:
    primary_id = uuid.uuid4()
    fallback_id = uuid.uuid4()
    phantom_id = uuid.uuid4()
    hub.register(primary_id, backend_a)
    hub.register(fallback_id, backend_b)
    hub.mark_unhealthy(primary_id)
    hub._fallbacks[primary_id] = [phantom_id, fallback_id]

    result = await hub.get(primary_id)
    assert result is backend_b


# ---------------------------------------------------------------------------
# get() — audit_logger called on failover
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_calls_audit_logger_on_failover(
    hub: ModelBackendHub,
    backend_a: StubModelBackend,
    backend_b: StubModelBackend,
) -> None:
    primary_id = uuid.uuid4()
    fallback_id = uuid.uuid4()
    hub.register(primary_id, backend_a)
    hub.register(fallback_id, backend_b)
    hub.mark_unhealthy(primary_id)
    hub._fallbacks[primary_id] = [fallback_id]

    audit_logger = AsyncMock()
    result = await hub.get(primary_id, audit_logger=audit_logger)
    assert result is backend_b
    audit_logger.assert_awaited_once_with({
        "event_type": "model_failover",
        "primary_id": str(primary_id),
        "fallback_id": str(fallback_id),
    })


# ---------------------------------------------------------------------------
# get() — audit_logger NOT called on healthy primary
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_does_not_call_audit_logger_when_healthy(
    hub: ModelBackendHub,
    backend_a: StubModelBackend,
) -> None:
    bid = uuid.uuid4()
    hub.register(bid, backend_a)
    audit_logger = AsyncMock()
    await hub.get(bid, audit_logger=audit_logger)
    audit_logger.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_with_rotation() — unrotated
# ---------------------------------------------------------------------------


def test_get_with_rotation_unrotated(
    hub: ModelBackendHub,
    backend_a: StubModelBackend,
) -> None:
    bid = uuid.uuid4()
    hub.register(bid, backend_a)
    result = hub.get_with_rotation(bid)
    assert result.backend is backend_a
    assert result.rotated is False
    assert result.original_id == bid
    assert result.used_fallback_id is None


# ---------------------------------------------------------------------------
# get_with_rotation() — rotated with fallback
# ---------------------------------------------------------------------------


def test_get_with_rotation_uses_fallback(
    hub: ModelBackendHub,
    backend_a: StubModelBackend,
    backend_b: StubModelBackend,
) -> None:
    primary_id = uuid.uuid4()
    fallback_id = uuid.uuid4()
    hub.register(primary_id, backend_a)
    hub.register(fallback_id, backend_b)
    hub.mark_unhealthy(primary_id)
    hub._fallbacks[primary_id] = [fallback_id]

    result = hub.get_with_rotation(primary_id)
    assert result.backend is backend_b
    assert result.rotated is True
    assert result.original_id == primary_id
    assert result.used_fallback_id == fallback_id


# ---------------------------------------------------------------------------
# get_with_rotation() — fallback list empty, scan all
# ---------------------------------------------------------------------------


def test_get_with_rotation_scans_all_when_no_fallback_configured(
    hub: ModelBackendHub,
    backend_a: StubModelBackend,
    backend_b: StubModelBackend,
) -> None:
    primary_id = uuid.uuid4()
    other_id = uuid.uuid4()
    hub.register(primary_id, backend_a)
    hub.register(other_id, backend_b)
    hub.mark_unhealthy(primary_id)
    result = hub.get_with_rotation(primary_id)
    assert result.rotated is True
    assert result.backend is backend_b
    assert result.used_fallback_id == other_id


# ---------------------------------------------------------------------------
# initialise() — reads fallback_backend_ids
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_initialise_reads_fallback_ids() -> None:
    """Simulate ORM rows with fallback_backend_ids."""
    primary_id = uuid.uuid4()
    fallback_id = uuid.uuid4()

    row1 = MagicMock()
    row1.id = primary_id
    row1.provider = "ollama"
    row1.model_id = "llama3"
    row1.credentials_ciphertext = b"{}"
    row1.default_params = {}
    row1.fallback_backend_ids = [str(fallback_id)]

    secrets_backend = AsyncMock()
    secrets_backend.get_secret = AsyncMock(return_value='{"api_key": "", "base_url": "http://localhost:11434/v1"}')

    async with ModelBackendHub() as hub:
        await hub.initialise([row1], secrets_backend=secrets_backend)
        assert hub._fallbacks.get(primary_id) == [fallback_id]


# ---------------------------------------------------------------------------
# initialise() — no fallback attribute on rows
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_initialise_missing_fallback_ids_does_not_crash() -> None:
    """Rows without fallback_backend_ids attribute should not crash."""
    backend_id = uuid.uuid4()

    row = MagicMock()
    row.id = backend_id
    row.provider = "ollama"
    row.model_id = "llama3"
    row.credentials_ciphertext = b"{}"
    row.default_params = {}
    # Simulate ORM attribute not set (before migration)
    del row.fallback_backend_ids

    secrets_backend = AsyncMock()
    secrets_backend.get_secret = AsyncMock(return_value='{"api_key": "", "base_url": "http://localhost:11434/v1"}')

    async with ModelBackendHub() as hub:
        await hub.initialise([row], secrets_backend=secrets_backend)
        assert backend_id in hub._backends
