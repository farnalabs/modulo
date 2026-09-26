"""FAR-1233 cancellation transparency: vocabulary + write-site parity tests.

``runs.cancel_reason`` is a CLOSED vocabulary (``ck_runs_cancel_reason``, a
migration-owned CHECK) behind a nullable column, so this file pins:

* the Python vocabulary (``db.models.run.CANCEL_REASON_VALUES``) and the
  migration's CHECK list stay in sync — a value written by a cancellation
  site but missing from the CHECK fails the write on a fresh DB, and a CHECK
  value with no constant invites an untyped literal;
* the migration chains off the current head (0259) and carries both columns;
* ``request_cancellation`` — the operator/agent cancel path — records WHY
  (reason) and WHO (actor) in the same write as ``status='cancelled'``, and
  refuses a reason outside the vocabulary;
* a reason/actor-less call still lands on a legal value (defaults), so every
  caller of the legacy two-arg signature stays correct.

Mirrors the sibling ``test_run_status_vocabulary.py`` pattern.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.run import request_cancellation
from modulo.db.models.run import (
    CANCEL_REASON_AGENT_REQUESTED,
    CANCEL_REASON_HITL_GATE_EXPIRED,
    CANCEL_REASON_HITL_GATE_MISSING,
    CANCEL_REASON_USER_REQUESTED,
    CANCEL_REASON_VALUES,
    CANCELLED_BY_SYSTEM,
)

_MIGRATION_NAME = "0260_run_cancel_reason"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_check_values() -> frozenset[str]:
    """The literal values inside the migration's ``cancel_reason IN (...)``."""
    module = _load_migration()
    sql: str = module._ADD_CHECK_NOT_VALID
    marker = "cancel_reason IN ("
    start = sql.index(marker) + len(marker)
    end = sql.index(")", start)
    return frozenset(chunk.strip().strip("'") for chunk in sql[start:end].split(","))


def _fake_run(status: str = "running") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        cancellation_requested=False,
        cancel_reason=None,
        cancelled_by=None,
        completed_at=None,
    )


def _session_returning(run: Any) -> AsyncMock:
    """Async session whose first SELECT returns *run* (FOR UPDATE shape)."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = run
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()
    return session


class TestCancelReasonVocabulary:
    """Python constants <-> migration CHECK <-> migration chain."""

    def test_migration_check_matches_python_vocabulary(self) -> None:
        """Bidirectional: a written reason missing from the CHECK breaks the
        write on a fresh DB; a CHECK value with no constant is an untyped
        literal someone will render raw."""
        assert _migration_check_values() == CANCEL_REASON_VALUES

    def test_migration_chains_off_current_head(self) -> None:
        module = _load_migration()
        assert module.revision == _MIGRATION_NAME
        assert module.down_revision == "0259_pipeline_snapshot_max_autonomy_check"

    def test_migration_adds_both_columns(self) -> None:
        module = _load_migration()
        assert "cancel_reason" in module._ADD_CANCEL_REASON_COLUMN
        assert "cancelled_by" in module._ADD_CANCELLED_BY_COLUMN

    def test_every_reason_constant_is_in_the_vocabulary(self) -> None:
        constants = (
            CANCEL_REASON_USER_REQUESTED,
            CANCEL_REASON_AGENT_REQUESTED,
            CANCEL_REASON_HITL_GATE_EXPIRED,
            CANCEL_REASON_HITL_GATE_MISSING,
        )
        for reason in constants:
            assert reason in CANCEL_REASON_VALUES, f"{reason!r} missing from CANCEL_REASON_VALUES"

    def test_vocabulary_size_matches_the_four_cancellation_sites(self) -> None:
        assert len(CANCEL_REASON_VALUES) == 4


class TestRequestCancellation:
    """The operator/agent cancel path records WHY/WHO with the status flip."""

    @pytest.mark.asyncio
    async def test_records_default_reason_and_system_actor(self) -> None:
        run = _fake_run()
        session = _session_returning(run)

        with patch("modulo.db.crud.run._classify_terminal_run", new_callable=AsyncMock) as classify:
            out = await request_cancellation(session, run.id)

        assert out is run
        assert run.status == "cancelled"
        assert run.cancellation_requested is True
        assert run.cancel_reason == CANCEL_REASON_USER_REQUESTED
        assert run.cancelled_by == CANCELLED_BY_SYSTEM
        assert run.completed_at is not None
        classify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_records_explicit_reason_and_actor(self) -> None:
        run = _fake_run()
        session = _session_returning(run)
        actor = str(uuid.uuid4())

        with patch("modulo.db.crud.run._classify_terminal_run", new_callable=AsyncMock):
            await request_cancellation(
                session,
                run.id,
                reason=CANCEL_REASON_AGENT_REQUESTED,
                actor=actor,
            )

        assert run.cancel_reason == CANCEL_REASON_AGENT_REQUESTED
        assert run.cancelled_by == actor

    @pytest.mark.asyncio
    async def test_reason_outside_vocabulary_is_rejected_before_any_write(self) -> None:
        run = _fake_run()
        session = _session_returning(run)

        with pytest.raises(ValueError, match="invalid cancel reason"):
            await request_cancellation(session, run.id, reason="cost_budget")

        session.execute.assert_not_awaited()
        assert run.cancellation_requested is False
        assert run.cancel_reason is None

    @pytest.mark.asyncio
    async def test_missing_run_returns_none_without_writing(self) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)

        out = await request_cancellation(session, uuid.uuid4())

        assert out is None
