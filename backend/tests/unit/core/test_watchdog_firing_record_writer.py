"""Unit tests for the FAR-1463 durable node-deadline watchdog-firing writer.

``pipeline_execution._record_node_deadline_watchdog_firing`` is the observability
half of FAR-1463: it increments ``runs.node_deadline_watchdog_fired_count`` on a
fresh connection, org-scoped and committed immediately, so a firing is durable
BEFORE the retry consult decides between re-dispatch and terminal fail.

The retry-hook tests (``test_pipeline_execution_watchdog_retry.py``) patch this
helper out to assert call ORDER; these tests exercise the helper itself, so its
body and both exception arms are covered rather than merely mocked:

- the happy path sets the RLS org context, runs the org-scoped increment and
  commits;
- a write failure is fail-soft WITH a log (a firing must never block the
  watchdog from reaching a terminal state);
- ``asyncio.CancelledError`` is re-raised (worker shutdown is not ours to
  swallow).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Self

import pytest

from modulo.core.pipeline_execution import (
    _SQL_SET_ORG_ID,
    _record_node_deadline_watchdog_firing,
)

_RUN_ID = "7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f"
_ORG_ID = "8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70"
_LOGGER = "modulo.core.pipeline_execution"


class _FakeResult:
    def first(self) -> None:
        return None


class _FakeConn:
    def __init__(self, engine: _FakeEngine) -> None:
        self._engine = engine

    def get_bind(self) -> SimpleNamespace:
        """Dialect gate input for ``set_mutation_row_lock_timeout`` (FAR-1601).

        Non-postgres double: the helper's documented safe no-op branch, so the
        recorded statement count stays at two (RLS set_config + increment).
        """
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        self._engine.statements.append((str(stmt), params))
        if self._engine.raise_on_execute is not None:
            raise self._engine.raise_on_execute
        return _FakeResult()

    async def commit(self) -> None:
        self._engine.commits += 1
        if self._engine.raise_on_commit is not None:
            raise self._engine.raise_on_commit


class _FakeEngine:
    """Async-engine stand-in recording every statement and commit."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, Any] | None]] = []
        self.commits = 0
        self.raise_on_execute: BaseException | None = None
        self.raise_on_commit: BaseException | None = None

    def connect(self) -> _FakeConn:
        return _FakeConn(self)


async def test_firing_record_sets_rls_scopes_update_and_commits() -> None:
    """Happy path: RLS context first, then the org-scoped increment, committed
    on its own connection so the value is durable before either outcome runs."""
    engine = _FakeEngine()

    await _record_node_deadline_watchdog_firing(engine, _RUN_ID, _ORG_ID)  # type: ignore[arg-type]

    assert len(engine.statements) == 2
    set_org_sql, set_org_params = engine.statements[0]
    assert set_org_sql == _SQL_SET_ORG_ID
    assert set_org_params == {"val": _ORG_ID}

    update_sql, update_params = engine.statements[1]
    assert "UPDATE runs SET node_deadline_watchdog_fired_count = node_deadline_watchdog_fired_count + 1" in update_sql
    assert "WHERE id=:rid AND organisation_id=:oid" in update_sql
    assert update_params == {"rid": _RUN_ID, "oid": _ORG_ID}
    assert engine.commits == 1


async def test_firing_record_is_fail_soft_with_log(caplog: pytest.LogCaptureFixture) -> None:
    """A write failure is swallowed WITH a log — the watchdog must still reach
    a terminal state (same contract as the durable phase writer)."""
    engine = _FakeEngine()
    engine.raise_on_execute = RuntimeError("simulated DB failure")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _record_node_deadline_watchdog_firing(engine, _RUN_ID, _ORG_ID)  # type: ignore[arg-type]

    assert "pipeline_execution.watchdog_firing_record_failed" in caplog.text
    assert engine.commits == 0


async def test_firing_record_commit_failure_is_also_fail_soft(caplog: pytest.LogCaptureFixture) -> None:
    """A commit failure (not just the UPDATE) is fail-soft too."""
    engine = _FakeEngine()
    engine.raise_on_commit = RuntimeError("simulated commit failure")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _record_node_deadline_watchdog_firing(engine, _RUN_ID, _ORG_ID)  # type: ignore[arg-type]

    assert "pipeline_execution.watchdog_firing_record_failed" in caplog.text


async def test_firing_record_reraises_cancelled_error() -> None:
    """``asyncio.CancelledError`` is re-raised, never swallowed as fail-soft."""
    engine = _FakeEngine()
    engine.raise_on_execute = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _record_node_deadline_watchdog_firing(engine, _RUN_ID, _ORG_ID)  # type: ignore[arg-type]
