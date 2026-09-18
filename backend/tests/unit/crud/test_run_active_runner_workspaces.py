"""FAR-771: org-scoped active-workspace count backing the Runners profile detail.

Mock/fake based — no Postgres. The count
(``modulo.db.crud.run.count_active_runner_dispatches_for_org``) is a SQL
SELECT over ``runs`` with the live-marker / status / org predicates applied.
These tests pin the POPULATION contract that FAR-771's count shares with the
D8 capacity reader:

* a running run with a live marker is counted (org-scoped)
* a terminal run is NEVER counted (its marker can only be a crash leak, never
  a live workspace)
* a marker from another org is excluded (the ``organisation_id`` predicate is
  bound to the caller's org — RLS is the second fence, this predicate the
  first)

The SELECT is rendered with ``literal_binds`` so the assertions operate on the
real SQL string rather than brittle AST introspection.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from modulo.db.crud.run import count_active_runner_dispatches_for_org
from modulo.db.models.run import TERMINAL_STATUSES

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _compile_with_literal_binds(stmt: Any) -> str:
    """Render the SELECT with literal values baked in (assertion-friendly)."""
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


async def _count_sql_and_value(
    flag_on: bool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    counted: int = 0,
) -> tuple[str, int]:
    """Run the count, capturing and rendering its SELECT, returning the raw
    SELECT text plus the value the fake session reported."""
    statements: list[Any] = []

    class _Settings:
        runner_capacity_gate_enabled = flag_on

    monkeypatch.setattr("modulo.settings.get_settings", lambda: _Settings())

    session = MagicMock()

    async def _execute(stmt: Any, *_: Any, **_kw: Any) -> Any:
        statements.append(stmt)
        result = MagicMock()
        result.scalar_one.return_value = counted
        return result

    session.execute = AsyncMock(side_effect=_execute)
    value = await count_active_runner_dispatches_for_org(session, _ORG)
    assert len(statements) == 1
    return _compile_with_literal_binds(statements[0]), value


class TestActiveWorkspaceCountPopulation:
    async def test_running_run_with_live_marker_counts_and_returns_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sql, value = await _count_sql_and_value(flag_on=True, monkeypatch=monkeypatch, counted=5)
        # The count is org-scoped — a foreign marker never reaches the figure
        # (the explicit organisation_id predicate; RLS is the second fence).
        assert str(_ORG) in sql
        assert str(_OTHER_ORG) not in sql
        assert "sandbox_dispatch_state IS NOT NULL" in sql
        # D8 flag-on population: RUNNING runs only — a live marker can only
        # legally exist on a running run.
        assert "runs.status = 'running'" in sql
        # The query result is surfaced verbatim.
        assert value == 5

    async def test_terminal_runs_never_counted_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The flag-on population is `status = 'running'` exactly — a terminal
        # run (its marker is a crash leak awaiting the sweep) can never
        # increment the count.
        sql, _ = await _count_sql_and_value(flag_on=True, monkeypatch=monkeypatch)
        assert "runs.status = 'running'" in sql
        for terminal in TERMINAL_STATUSES:
            assert f"'{terminal}'" not in sql

    async def test_terminal_runs_never_counted_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Flag-off window: the pre-D8 ACTIVE_RUN_STATUSES population — none of
        # which is terminal, so a terminal marker never feeds the figure.
        sql, _ = await _count_sql_and_value(flag_on=False, monkeypatch=monkeypatch)
        assert "IN (" in sql
        for terminal in TERMINAL_STATUSES:
            assert f"'{terminal}'" not in sql

    async def test_hitl_tombstone_markers_excluded_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A HITL-boundary tombstone marker holds no slot — it must not feed
        # the active-workspace count (the same fragment the D8 reader uses).
        sql, _ = await _count_sql_and_value(flag_on=True, monkeypatch=monkeypatch)
        assert "cleared_at_hitl" in sql
