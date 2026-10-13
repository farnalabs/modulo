"""Unit tests for RunTeardownSeal — the joint revoke+destroy terminal step (FAR-1690 A3).

The seal is the single choke point that, in order (revoke-first, fail-closed),
revokes a run's credentials, destroys every workspace labelled
``modulo.run.id = <run_id>``, and writes ONE ``run.teardown_sealed`` fact. These
tests pin the ORDER, the fail-closed stop (a revoke failure must NOT reach the
destroy leg), the best-effort destroy leg, and the single-fact contract.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core import run_teardown
from modulo.core.run_teardown import (
    TEARDOWN_SEALED_EVENT_TYPE,
    RunTeardownSealError,
    seal_run_teardown,
)


class _Begin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[Any] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return _Begin()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append(stmt)
        return MagicMock()


def _session_factory() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def seal_fact(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Capture the ONE ``run.teardown_sealed`` append (patch it at its source)."""
    mock = AsyncMock()
    monkeypatch.setattr("modulo.core.system_audit_logger.append_system_audit_event", mock)
    return mock


@pytest.mark.asyncio
async def test_seal_revokes_then_destroys_then_writes_one_fact(
    monkeypatch: pytest.MonkeyPatch,
    seal_fact: AsyncMock,
) -> None:
    order: list[str] = []

    async def _revoke(**_kwargs: Any) -> int:
        order.append("revoke")
        return 2

    async def _destroy(_run_id: uuid.UUID) -> dict[str, int]:
        order.append("destroy")
        return {"scanned": 3, "destroyed": 3, "errors": 0}

    monkeypatch.setattr(run_teardown, "_revoke_credentials", _revoke)
    monkeypatch.setattr(run_teardown, "_destroy_run_workspaces", _destroy)

    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    result = await seal_run_teardown(run_id=run_id, org_id=org_id, session_factory=_session_factory)

    assert order == ["revoke", "destroy"]
    assert result.credentials_revoked is True
    assert result.keys_revoked == 2
    assert result.workspaces_scanned == 3
    assert result.workspaces_destroyed == 3
    assert result.sealed is True
    seal_fact.assert_awaited_once()
    kwargs = seal_fact.await_args.kwargs
    assert kwargs["event_type"] == TEARDOWN_SEALED_EVENT_TYPE
    assert kwargs["resource_type"] == "run"
    assert kwargs["resource_id"] == run_id
    payload = kwargs["payload_json"]
    assert payload["credentials_revoked"] is True
    assert payload["keys_revoked"] == 2
    assert payload["workspaces_destroyed"] == 3
    assert payload["sealed"] is True


@pytest.mark.asyncio
async def test_seal_fail_closed_skips_destroy_and_surfaces_a_revoke_failure(
    monkeypatch: pytest.MonkeyPatch,
    seal_fact: AsyncMock,
) -> None:
    destroy_mock = AsyncMock(return_value={"scanned": 0, "destroyed": 0, "errors": 0})

    async def _revoke(**_kwargs: Any) -> int:
        raise RuntimeError("revoke db down")

    monkeypatch.setattr(run_teardown, "_revoke_credentials", _revoke)
    monkeypatch.setattr(run_teardown, "_destroy_run_workspaces", destroy_mock)

    with pytest.raises(RunTeardownSealError):
        await seal_run_teardown(run_id=uuid.uuid4(), org_id=uuid.uuid4(), session_factory=_session_factory)

    # Fail-closed: the destroy leg is NEVER reached while the credential may
    # still be live.
    destroy_mock.assert_not_awaited()
    # The one fact still records the fail-closed stop — never silent.
    seal_fact.assert_awaited_once()
    payload = seal_fact.await_args.kwargs["payload_json"]
    assert payload["credentials_revoked"] is False
    assert payload["workspaces_destroyed"] == 0
    assert payload["sealed"] is False


@pytest.mark.asyncio
async def test_seal_destroy_failure_is_best_effort_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
    seal_fact: AsyncMock,
) -> None:
    async def _revoke(**_kwargs: Any) -> int:
        return 1

    async def _destroy(_run_id: uuid.UUID) -> dict[str, int]:
        raise RuntimeError("engine unreachable")

    monkeypatch.setattr(run_teardown, "_revoke_credentials", _revoke)
    monkeypatch.setattr(run_teardown, "_destroy_run_workspaces", _destroy)

    result = await seal_run_teardown(run_id=uuid.uuid4(), org_id=uuid.uuid4(), session_factory=_session_factory)

    assert result.credentials_revoked is True
    assert result.workspaces_destroyed == 0
    assert result.sealed is False
    assert "engine unreachable" in (result.error or "")
    payload = seal_fact.await_args.kwargs["payload_json"]
    assert payload["credentials_revoked"] is True
    assert payload["sealed"] is False


@pytest.mark.asyncio
async def test_seal_without_session_factory_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    seal_fact: AsyncMock,
) -> None:
    revoke_mock = AsyncMock(return_value=1)
    destroy_mock = AsyncMock(return_value={"scanned": 0, "destroyed": 0, "errors": 0})
    monkeypatch.setattr(run_teardown, "_revoke_credentials", revoke_mock)
    monkeypatch.setattr(run_teardown, "_destroy_run_workspaces", destroy_mock)

    with pytest.raises(RunTeardownSealError):
        await seal_run_teardown(run_id=uuid.uuid4(), org_id=uuid.uuid4(), session_factory=None)

    revoke_mock.assert_not_awaited()
    destroy_mock.assert_not_awaited()
    seal_fact.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_seal_fact_uses_the_run_teardown_sealed_event_type(seal_fact: AsyncMock) -> None:
    run_id = uuid.uuid4()
    org_id = uuid.uuid4()

    await run_teardown._write_seal_fact(
        _session_factory,
        run_id=run_id,
        org_id=org_id,
        payload={"sealed": True},
    )

    seal_fact.assert_awaited_once()
    kwargs = seal_fact.await_args.kwargs
    assert kwargs["event_type"] == TEARDOWN_SEALED_EVENT_TYPE
    assert kwargs["org_id"] == org_id
    assert kwargs["resource_type"] == "run"
    assert kwargs["resource_id"] == run_id
    assert kwargs["payload_json"] == {"sealed": True}


@pytest.mark.asyncio
async def test_seal_result_survives_a_fact_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    seal_fact: AsyncMock,
) -> None:
    async def _revoke(**_kwargs: Any) -> int:
        return 1

    async def _destroy(_run_id: uuid.UUID) -> dict[str, int]:
        return {"scanned": 1, "destroyed": 1, "errors": 0}

    monkeypatch.setattr(run_teardown, "_revoke_credentials", _revoke)
    monkeypatch.setattr(run_teardown, "_destroy_run_workspaces", _destroy)
    seal_fact.side_effect = RuntimeError("ledger down")

    result = await seal_run_teardown(run_id=uuid.uuid4(), org_id=uuid.uuid4(), session_factory=_session_factory)

    # A ledger failure is logged, not fatal: the legs succeeded, so the result
    # still reports success rather than re-running a completed teardown.
    assert result.credentials_revoked is True
    assert result.workspaces_destroyed == 1
    assert result.sealed is True


@pytest.mark.asyncio
async def test_seal_propagates_a_cancellation_from_the_revoke_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation is a control-flow signal — never folded into ``sealed``."""

    async def _revoke(**_kwargs: Any) -> int:
        raise asyncio.CancelledError

    monkeypatch.setattr(run_teardown, "_revoke_credentials", _revoke)

    with pytest.raises(asyncio.CancelledError):
        await seal_run_teardown(run_id=uuid.uuid4(), org_id=uuid.uuid4(), session_factory=_session_factory)


@pytest.mark.asyncio
async def test_seal_propagates_a_cancellation_from_the_destroy_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation during the destroy leg is re-raised, not recorded as a
    best-effort destroy failure."""

    async def _revoke(**_kwargs: Any) -> int:
        return 1

    async def _destroy(_run_id: uuid.UUID) -> dict[str, int]:
        raise asyncio.CancelledError

    monkeypatch.setattr(run_teardown, "_revoke_credentials", _revoke)
    monkeypatch.setattr(run_teardown, "_destroy_run_workspaces", _destroy)

    with pytest.raises(asyncio.CancelledError):
        await seal_run_teardown(run_id=uuid.uuid4(), org_id=uuid.uuid4(), session_factory=_session_factory)


@pytest.mark.asyncio
async def test_seal_propagates_a_cancellation_from_the_fact_write(
    monkeypatch: pytest.MonkeyPatch,
    seal_fact: AsyncMock,
) -> None:
    """A cancellation during the ledger append is re-raised (the fact write is
    best-effort only for real failures, not for cancellation)."""

    async def _revoke(**_kwargs: Any) -> int:
        return 1

    async def _destroy(_run_id: uuid.UUID) -> dict[str, int]:
        return {"scanned": 0, "destroyed": 0, "errors": 0}

    monkeypatch.setattr(run_teardown, "_revoke_credentials", _revoke)
    monkeypatch.setattr(run_teardown, "_destroy_run_workspaces", _destroy)
    seal_fact.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await seal_run_teardown(run_id=uuid.uuid4(), org_id=uuid.uuid4(), session_factory=_session_factory)
