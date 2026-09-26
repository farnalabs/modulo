"""FAR-1233: the HITL zombie terminalizers record WHY they cancelled.

``_terminalize_expired_hitl_gates`` (FAR-648) and
``_terminalize_hitl_gate_missing`` (FAR-721) are the only two WATCHDOG-owned
cancellation sites: they flip ``status='cancelled'`` from raw SQL, so the
cancellation reason has to ride the SAME UPDATE — there is no ORM object to
annotate afterwards. These tests capture the SQL + bound params each terminalizer
executes and pin the ``(error_code, cancel_reason)`` pairing, so dropping either
binding (or swapping the reason) fails here rather than shipping a cancelled
run with no recorded cause.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from modulo.core import cron_helpers as ch
from modulo.db.models.run import (
    CANCEL_REASON_HITL_GATE_EXPIRED,
    CANCEL_REASON_HITL_GATE_MISSING,
    CANCELLED_BY_SYSTEM,
)

ORG = uuid.uuid4()


class _EmptyResult:
    def all(self) -> list[Any]:
        return []


class _CaptureSession:
    """Session stand-in recording every ``(sql, params)`` execute call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        stmt: Any,
        params: dict[str, Any] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> _EmptyResult:
        self.calls.append((str(stmt), dict(params or {})))
        return _EmptyResult()


def _single_call(session: _CaptureSession) -> tuple[str, dict[str, Any]]:
    assert len(session.calls) == 1
    return session.calls[0]


class TestExpiredHitlGateTerminalizer:
    @pytest.mark.asyncio
    async def test_records_cancel_reason_on_the_status_flip(self) -> None:
        session = _CaptureSession()

        await ch._terminalize_expired_hitl_gates(session, ORG, grace_seconds=60)

        sql, params = _single_call(session)
        assert "cancel_reason=:reason" in sql
        assert "cancelled_by=:actor" in sql
        assert params["reason"] == CANCEL_REASON_HITL_GATE_EXPIRED
        assert params["actor"] == CANCELLED_BY_SYSTEM

    @pytest.mark.asyncio
    async def test_reason_pairs_with_the_error_code(self) -> None:
        """The run's ``error_code`` and ``cancel_reason`` name the same cause
        — a drift between them reads as two different failures in the UI."""
        session = _CaptureSession()

        await ch._terminalize_expired_hitl_gates(session, ORG, grace_seconds=60)

        _, params = _single_call(session)
        assert params["code"] == "hitl_gate_expired"
        assert params["reason"] == CANCEL_REASON_HITL_GATE_EXPIRED


class TestMissingHitlGateTerminalizer:
    @pytest.mark.asyncio
    async def test_records_cancel_reason_on_the_status_flip(self) -> None:
        session = _CaptureSession()

        await ch._terminalize_hitl_gate_missing(session, ORG, grace_seconds=60)

        sql, params = _single_call(session)
        assert "cancel_reason=:reason" in sql
        assert "cancelled_by=:actor" in sql
        assert params["reason"] == CANCEL_REASON_HITL_GATE_MISSING
        assert params["actor"] == CANCELLED_BY_SYSTEM

    @pytest.mark.asyncio
    async def test_reason_pairs_with_the_error_code(self) -> None:
        session = _CaptureSession()

        await ch._terminalize_hitl_gate_missing(session, ORG, grace_seconds=60)

        _, params = _single_call(session)
        assert params["code"] == "hitl_gate_missing"
        assert params["reason"] == CANCEL_REASON_HITL_GATE_MISSING
