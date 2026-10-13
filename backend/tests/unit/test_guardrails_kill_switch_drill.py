"""Unit tests for the org guardrail kill-switch drill (FAR-1707).

The drill is a NON-MUTATING self-test of the existing org guardrail
kill-switch's two invariants — DOWNGRADE (every bound guardrail resolves to
``observe``, non-vacuously) and FAIL-CLOSED READ (a read failure resolves to
OFF). These tests pin the branches on an in-memory SQLite engine: the
``system_audit_events`` row is a real insert, so "exactly one record" is
observed, not mocked. Interacting with a production Postgres is out of scope
(the ledger schema is dialect-independent).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core import saq_worker as sw
from modulo.core.guardrails import kill_switch_drill as drill_module
from modulo.core.guardrails.kill_switch_drill import (
    run_kill_switch_drill,
    verify_kill_switch_invariants,
)
from modulo.db.models.base import Base
from modulo.db.models.system_audit_event import SystemAuditEvent

_TABLES: list[Any] = [SystemAuditEvent.__table__]


def _settings() -> MagicMock:
    return MagicMock(modulo_library_sync_interval_seconds=300)


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    # autobegin=False matches the app DI default: the caller owns transaction
    # start, exactly as the cron path's ``async with factory(), begin()`` does.
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s:
        yield s


async def _run_and_read_rows(session: AsyncSession) -> tuple[dict[str, Any], list[SystemAuditEvent]]:
    """Run the drill on *session*, then read every ledger row back."""
    async with session.begin():
        result = await run_kill_switch_drill(session)
    async with session.begin():
        rows = (await session.execute(select(SystemAuditEvent))).scalars().all()
    return dict(result), list(rows)


def test_pure_invariants_pass_and_are_non_vacuous() -> None:
    outcome = verify_kill_switch_invariants()

    assert outcome["downgrade_applied"] is True
    assert outcome["downgrade_non_vacuous"] is True


async def test_drill_records_exactly_one_pass_row(session: AsyncSession) -> None:
    result, rows = await _run_and_read_rows(session)

    assert result["result"] == "pass"
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "kill_switch_drill"
    assert row.resource_type == "guardrail_kill_switch"
    assert row.payload_json["result"] == "pass"
    assert row.payload_json["checks"]["downgrade_applied"] is True
    assert row.payload_json["checks"]["downgrade_non_vacuous"] is True
    assert row.payload_json["checks"]["fail_closed_read"] is True
    assert row.payload_json["duration_ms"] >= 0.0


async def test_noop_downgrade_helper_records_fail(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2 — monkeypatching the downgrade helper to a no-op makes the drill FAIL.

    Proves the check can fail (it is not an always-green assertion): the
    pre-downgrade set still holds ``block``/``redact``, so ``downgrade_applied``
    is False.
    """
    monkeypatch.setattr("modulo.db.crud.run._downgrade_guardrails_to_observe", lambda defs: list(defs))

    result, rows = await _run_and_read_rows(session)

    assert result["result"] == "fail"
    assert result["checks"]["downgrade_applied"] is False
    assert len(rows) == 1
    assert rows[0].payload_json["result"] == "fail"


async def test_reader_fails_closed_to_off_on_read_failure() -> None:
    """AC4 — the real reader resolves a raising session to OFF (enforcement on)."""
    from modulo.db.crud.run import _read_guardrails_kill_switch

    faulty = cast(AsyncSession, drill_module._FailingReadSession())

    assert await _read_guardrails_kill_switch(faulty, uuid.UUID(int=0)) is False


async def test_read_check_uses_the_failing_stub_not_a_real_org(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6 — the only session handed to the reader is the failing stub.

    A real org's session is never passed to ``_read_guardrails_kill_switch``, so
    no live ``guardrails_kill_switch`` value can be read or written by the drill.
    """
    seen: list[Any] = []

    async def _spy(session_arg: Any, org_id: uuid.UUID) -> bool:
        seen.append(session_arg)
        return False

    monkeypatch.setattr("modulo.db.crud.run._read_guardrails_kill_switch", _spy)

    result, _rows = await _run_and_read_rows(session)

    assert result["result"] == "pass"
    assert len(seen) == 1
    assert isinstance(seen[0], drill_module._FailingReadSession)


async def test_cron_ctx_path_opens_owned_session_and_commits_one_row(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1/AC5 — the SAQ cron path (a plain ctx dict) owns its transaction.

    The cron scheduler invokes ``run_kill_switch_drill(ctx)`` with a context
    dict, not a session; the drill must then open a cross-org system session and
    commit exactly one evidence row rather than appending in a caller's session.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    monkeypatch.setattr(sw, "_make_system_session_factory", lambda: maker)

    result = await run_kill_switch_drill({"job": "kill_switch_drill"})

    assert result["result"] == "pass"
    async with maker() as s, s.begin():
        rows = (await s.execute(select(SystemAuditEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "kill_switch_drill"
    assert rows[0].payload_json["result"] == "pass"


def test_module_never_reaches_for_the_organisations_model() -> None:
    """AC6 — no code path imports the org model (no real switch read/write)."""
    source = Path(drill_module.__file__).read_text(encoding="utf-8")

    assert "Organisation" not in source


def test_drill_registered_as_system_cron_and_function() -> None:
    """AC5 — the drill is a registered system function AND a registered cron job."""
    function_names = {fn.__name__ for fn in sw._system_functions()}
    assert "run_kill_switch_drill" in function_names

    with patch.object(sw, "get_settings", return_value=_settings()):
        jobs = {c.function.__name__: c for c in sw._system_cron_jobs()}
    assert "run_kill_switch_drill" in jobs
    job = jobs["run_kill_switch_drill"]
    assert job.cron == "0 3 * * *"
    assert job.unique is True
    assert job.timeout == 120
    assert job.retries == 1
