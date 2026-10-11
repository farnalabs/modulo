"""FAR-1610 — the REST cancel path's caller-side lock bound.

``api.routes.runs._cancel_run`` is the caller of the shared
``request_cancellation``: its first lock is that function's ``SELECT ... FOR
UPDATE`` on the hot ``runs`` row. The transaction-scoped bound is issued in
this caller's transaction, before the lock, and a 55P03 maps to a visible 409
(the request path never waits unbounded and never fails silently).
"""

from __future__ import annotations

import uuid
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from modulo.api.routes.runs import _cancel_run, cancel_run


def _lock_timeout_error(statement: str = "SELECT runs") -> OperationalError:
    from asyncpg import exceptions as asyncpg_exceptions

    driver_error = asyncpg_exceptions.LockNotAvailableError("canceling statement due to lock timeout")
    return OperationalError(statement, {}, driver_error)


class _BeginCM:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _BeginSession:
    """Session double whose ``begin()`` is a proper async context manager."""

    async def execute(self, *_a: Any, **_k: Any) -> Any:
        return MagicMock()

    def begin(self) -> _BeginCM:
        return _BeginCM()


def _principal() -> MagicMock:
    principal = MagicMock()
    principal.organisation_id = uuid.uuid4()
    principal.account_id = uuid.uuid4()
    return principal


async def test_cancel_run_helper_bounds_before_request_cancellation() -> None:
    order: list[str] = []
    session = MagicMock()
    run = MagicMock()
    run.status = "running"

    async def _bound(_session: Any) -> None:
        order.append("bound")

    async def _get_run(*_a: Any, **_k: Any) -> Any:
        return run

    async def _request(*_a: Any, **_k: Any) -> Any:
        order.append("request")
        return run

    with (
        patch("modulo.api.routes.runs.set_mutation_row_lock_timeout", new=_bound),
        patch("modulo.api.routes.runs.get_run", new=_get_run),
        patch("modulo.api.routes.runs.request_cancellation", new=_request),
        patch("modulo.core.cost_controller.finalize.finalize_cancelled_run", new=AsyncMock()),
    ):
        await _cancel_run(session, _principal(), uuid.uuid4())

    assert order == ["bound", "request"]


async def test_cancel_run_route_maps_lock_timeout_to_409() -> None:
    """A 55P03 from the bounded row-lock wait is a busy-row conflict, not a DB
    outage — the route answers a visible 409, never the retry-inviting 503."""
    session = _BeginSession()
    principal = _principal()

    with (
        patch("modulo.api.routes.runs.set_rls_org", new=AsyncMock()),
        patch(
            "modulo.api.routes.runs._cancel_run",
            new=AsyncMock(side_effect=_lock_timeout_error()),
        ),
        pytest.raises(HTTPException) as excinfo,
    ):
        await cancel_run(uuid.uuid4(), session=session, principal=principal)

    assert excinfo.value.status_code == 409
    assert "lock" in str(excinfo.value.detail).lower()


async def test_cancel_run_route_non_lock_db_error_stays_503() -> None:
    """Any OTHER SQLAlchemy error keeps the existing 503 contract."""
    session = _BeginSession()
    principal = _principal()

    with (
        patch("modulo.api.routes.runs.set_rls_org", new=AsyncMock()),
        patch(
            "modulo.api.routes.runs._cancel_run",
            new=AsyncMock(side_effect=OperationalError("SELECT runs", {}, RuntimeError("db down"))),
        ),
        pytest.raises(HTTPException) as excinfo,
    ):
        await cancel_run(uuid.uuid4(), session=session, principal=principal)

    assert excinfo.value.status_code == 503
