"""Unit tests for the FAR-132 legacy-shape straggler sweep.

Covers:

* ``count_legacy_agent_return_rows`` against a real (SQLite) ``run_node_outputs``
  table — a seeded legacy-shaped row is counted and sampled, a pure row (and the
  metadata / marker row kinds) are not;
* the shared ``stored_row_is_legacy`` predicate pinned against the sweep's SQL
  form so the two cannot drift;
* the ``legacy_agent_return_sweep`` cron wrapper's fail-loud contract: a loud
  structured warning on findings, a clean INFO line on zero, and a re-raise on a
  scan failure (never a silent zero).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.agent_return_legacy import (
    LegacyAgentReturnReport,
    count_legacy_agent_return_rows,
)
from modulo.core.node_output_split import stored_row_is_legacy
from modulo.db.models.run_node_outputs import FINAL_ATTEMPT_KEY, META_NODE_ID, RunNodeOutput

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_RUN = uuid.UUID("00000000-0000-0000-0000-0000000000bb")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(RunNodeOutput.__table__.create)
    try:
        yield eng
    finally:
        await eng.dispose()


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A sessionmaker over the seeded engine (plain helper, not a fixture)."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *row_values: dict[str, object],
) -> None:
    async with sessionmaker() as session, session.begin():
        for values in row_values:
            await session.execute(insert(RunNodeOutput).values(**values))


#: Sentinel for an ABSENT side — the production writer omits the column for an
#: absent side (SQL NULL), whereas passing Python ``None`` through the generic
#: JSON type would store a JSON ``null`` (present). The SQL NULL / jsonb-null
#: distinction is load bearing, so the seed mirrors the writer: omit the column.
_ABSENT = object()


def _row(
    node_id: str,
    *,
    attempt_key: str = FINAL_ATTEMPT_KEY,
    outputs: object = _ABSENT,
    telemetry: object = _ABSENT,
) -> dict[str, object]:
    values: dict[str, object] = {
        "organisation_id": _ORG,
        "run_id": _RUN,
        "node_id": node_id,
        "attempt_key": attempt_key,
    }
    if outputs is not _ABSENT:
        values["outputs_json"] = outputs
    if telemetry is not _ABSENT:
        values["node_telemetry_json"] = telemetry
    return values


@pytest.mark.asyncio
async def test_seeded_legacy_row_is_counted_and_sampled(
    engine: AsyncEngine,
) -> None:
    """A legacy-shaped row reports >0; structural sibling rows are excluded."""
    maker = _maker(engine)
    await _seed(
        maker,
        # The legacy shape: mixed envelope output, no telemetry side.
        _row("legacy", outputs={"output": {"answer": 1}}),
        # A P1+ pure row: telemetry side present -> not legacy.
        _row("pure", outputs={"answer": 1}, telemetry={"status": "completed"}),
        # The run-metadata sentinel row must never be counted.
        _row(META_NODE_ID, outputs={"empty_outputs": True, "empty_telemetry": True}),
        # A marker row (attempt_key != '__final__') must never be counted.
        _row("marked", attempt_key="run:x:node:marked:1", outputs={"output": {"a": 1}}),
    )
    async with maker() as session:
        report = await count_legacy_agent_return_rows(session)

    assert report.legacy_count == 1
    assert report.sample == ((str(_RUN), "legacy"),)
    assert report.sample_truncated is False


@pytest.mark.asyncio
async def test_pure_rows_report_zero(
    engine: AsyncEngine,
) -> None:
    """A table with only P1+ rows reports a clean zero."""
    maker = _maker(engine)
    await _seed(
        maker,
        _row("a", outputs={"answer": 1}, telemetry={"status": "completed"}),
        _row("b", telemetry={"skipped": True, "recovered": True}),
    )
    async with maker() as session:
        report = await count_legacy_agent_return_rows(session)

    assert report.legacy_count == 0
    assert not report.sample
    assert report.sample_truncated is False


@pytest.mark.asyncio
async def test_sample_is_bounded_and_marked_truncated(
    engine: AsyncEngine,
) -> None:
    maker = _maker(engine)
    await _seed(
        maker,
        *[_row(f"n{i}", outputs={"output": {"i": i}}) for i in range(5)],
    )
    async with maker() as session:
        report = await count_legacy_agent_return_rows(session, sample_limit=2)

    assert report.legacy_count == 5
    assert len(report.sample) == 2
    assert report.sample_truncated is True


@pytest.mark.asyncio
async def test_sql_predicate_matches_shared_row_predicate(
    engine: AsyncEngine,
) -> None:
    """The sweep's SQL form and ``stored_row_is_legacy`` agree on seeded rows.

    Every inserted row's presence flags are re-derived and compared against the
    shared predicate, so the set-based twin cannot silently drift from the
    accessor dispatch.
    """
    maker = _maker(engine)
    await _seed(
        maker,
        _row("legacy", outputs={"output": {"a": 1}}),
        _row("pure", outputs={"answer": True}, telemetry={}),
        _row("no_out", telemetry={"skipped": True}),
    )
    async with maker() as session:
        report = await count_legacy_agent_return_rows(session)
        rows = (
            await session.execute(
                select(
                    RunNodeOutput.node_id,
                    RunNodeOutput.outputs_json.is_(None).label("outputs_absent"),
                    RunNodeOutput.node_telemetry_json.is_(None).label("telemetry_absent"),
                )
            )
        ).all()

    flagged = {
        node_id
        for node_id, outputs_absent, telemetry_absent in rows
        if stored_row_is_legacy(outputs_present=not outputs_absent, telemetry_present=not telemetry_absent)
    }
    assert flagged == {"legacy"}
    assert report.legacy_count == len(flagged)


# ---------------------------------------------------------------------------
# cron wrapper (legacy_agent_return_sweep) — fail-loud contract
# ---------------------------------------------------------------------------


def _sessionmaker_mock() -> MagicMock:
    """A sessionmaker usable as ``async with factory() as s, s.begin():``."""
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = begin_cm
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = session_cm
    return factory


@pytest.mark.asyncio
async def test_cron_warns_loudly_when_legacy_rows_remain(caplog: pytest.LogCaptureFixture) -> None:
    from modulo.core import cron_helpers as ch

    report = LegacyAgentReturnReport(legacy_count=3, sample=((str(_RUN), "n1"),), sample_truncated=True)
    with (
        patch.object(ch, "_open_system_factory", return_value=_sessionmaker_mock()),
        patch(
            "modulo.core.agent_return_legacy.count_legacy_agent_return_rows",
            new=AsyncMock(return_value=report),
        ),
        caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"),
    ):
        stats = await ch.legacy_agent_return_sweep()

    assert stats["legacy_count"] == 3
    assert "agent_return.legacy_straggler_detected" in caplog.text


@pytest.mark.asyncio
async def test_cron_logs_clean_zero(caplog: pytest.LogCaptureFixture) -> None:
    from modulo.core import cron_helpers as ch

    report = LegacyAgentReturnReport(legacy_count=0)
    with (
        patch.object(ch, "_open_system_factory", return_value=_sessionmaker_mock()),
        patch(
            "modulo.core.agent_return_legacy.count_legacy_agent_return_rows",
            new=AsyncMock(return_value=report),
        ),
        caplog.at_level(logging.INFO, logger="modulo.core.cron_helpers"),
    ):
        stats = await ch.legacy_agent_return_sweep()

    assert stats["legacy_count"] == 0
    assert "agent_return.legacy_sweep_clean" in caplog.text
    assert "agent_return.legacy_straggler_detected" not in caplog.text


@pytest.mark.asyncio
async def test_cron_reraises_a_failed_scan_never_a_silent_zero() -> None:
    from modulo.core import cron_helpers as ch

    with (
        patch.object(ch, "_open_system_factory", return_value=_sessionmaker_mock()),
        patch(
            "modulo.core.agent_return_legacy.count_legacy_agent_return_rows",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        pytest.raises(RuntimeError, match="db down"),
    ):
        await ch.legacy_agent_return_sweep()
