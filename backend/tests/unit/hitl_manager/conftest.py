"""Shared fixtures and helpers for hitl_manager unit tests."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modulo.db.models.hitl_claim import HitlClaim


def _session_decide(
    *,
    update_returns_id: uuid.UUID | None = None,
    diagnosis_gate: HitlClaim | None = None,
    session_get_gate: HitlClaim | None = None,
) -> AsyncMock:
    """Session mock for HITLManager._decide().

    Call sequence in _decide():
      1. UPDATE … RETURNING id  → scalar_one_or_none() returns update_returns_id (or None)
      2. If None: _get() SELECT → scalar_one_or_none() returns diagnosis_gate
      If update_returns_id is not None: session.get() returns session_get_gate
    """
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = update_returns_id

    diag_result = MagicMock()
    diag_result.scalar_one_or_none.return_value = diagnosis_gate

    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return update_result
        return diag_result

    session.execute = _execute
    session.get = AsyncMock(return_value=session_get_gate)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    return session
