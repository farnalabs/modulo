"""FAR-1274: the one-PR-per-run delivery contract, enforced OUTSIDE the sandbox.

Covers two layers:

1. The pure detector (``find_duplicate_pr_urls`` / ``collect_delivery_pr_urls``)
   over the run's platform-captured delivery evidence — including the FAR-1254
   shape that motivates the ticket: a run whose SECOND ``gh pr create`` did not
   re-echo the delivery sentinel, so a sentinel-counting guard sees nothing.
   The detector reads the captured transcript (telemetry ``agent_stdout`` /
   marker ``raw_output``) plus the agent-declared ``pr_url`` fields instead.
2. The terminal-write enforcement hook: a real ``update_run_status(...,
   "complete")`` against an in-memory SQLite store alerts loudly (error log +
   an admin-scoped notification written in the SAME transaction) on a breach,
   stays silent on the single-PR happy path, never stacks duplicate alerts on
   re-terminalization, and never blocks the terminal status write.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from sqlalchemy import StaticPool, Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run import (
    NOTIFICATION_CATEGORY_DUPLICATE_PR,
    collect_delivery_pr_urls,
    find_duplicate_pr_urls,
    update_run_status,
)
from modulo.db.models.base import Base
from modulo.db.models.notification import Notification
from modulo.db.models.run import Run
from modulo.db.models.run_node_outputs import RunNodeOutput
from tests.unit._store_seed import seed_run_blobs

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

_PR_1 = "https://github.com/farnalabs/modulo/pull/1001"
_PR_2 = "https://github.com/farnalabs/modulo/pull/1002"

#: What ``gh pr create`` prints for a created PR (the URL alone on a line).
#: FAR-1254: the SECOND create did NOT re-echo the pipeline's delivery
#: sentinel — only the URL — which is exactly what this transcript carries.
_TWO_PR_STDOUT = f"""Creating pull request for fix/duplicate into main in farnalabs/modulo

{_PR_1}
Creating pull request for fix/duplicate-again into main in farnalabs/modulo

{_PR_2}
"""

_ONE_PR_STDOUT = f"""Creating pull request for fix/single into main in farnalabs/modulo

{_PR_1}
"""


def _far1254_blobs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The FAR-1254 incident shape: two PRs created, ONE sentinel echo.

    The agent declared only the first PR in its ``output.json`` ``pr_url``, the
    retention marker's single ``pr_url`` is the first raw-output match, and no
    marker carries the sentinel a second time — every single-value signal
    collapses onto ``_PR_1``. Only the captured transcript holds both URLs.
    """
    outputs = {"deliver": {"pr_url": _PR_1, "summary": "delivered"}}
    telemetry = {
        "deliver": {
            "agent_status": "completed",
            "agent_outcome": "success",
            "agent_stdout": _TWO_PR_STDOUT,
        }
    }
    markers = {
        "run:11111111-1111-1111-1111-111111111111:node:deliver:1": {
            "_modulo_marker": True,
            "status": "completed",
            "raw_output": _TWO_PR_STDOUT,
            "pr_url": _PR_1,
            "parse_error": "",
            "attempt_key": "run:11111111-1111-1111-1111-111111111111:node:deliver:1",
            "node_id": "deliver",
        }
    }
    return outputs, telemetry, markers


def _one_pr_blobs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    outputs = {"deliver": {"pr_url": _PR_1, "summary": "delivered"}}
    telemetry = {
        "deliver": {
            "agent_status": "completed",
            "agent_outcome": "success",
            "agent_stdout": _ONE_PR_STDOUT,
        }
    }
    markers = {
        "run:11111111-1111-1111-1111-111111111111:node:deliver:1": {
            "_modulo_marker": True,
            "status": "completed",
            "raw_output": _ONE_PR_STDOUT,
            "pr_url": _PR_1,
            "parse_error": "",
            "attempt_key": "run:11111111-1111-1111-1111-111111111111:node:deliver:1",
            "node_id": "deliver",
        }
    }
    return outputs, telemetry, markers


class TestDuplicatePrDetector:
    """Pure detector over the platform-captured delivery evidence."""

    def test_far1254_second_create_without_sentinel_is_caught(self) -> None:
        """The FAR-1254 shape: two creates, ONE sentinel echo.

        Every single-value signal (declared ``pr_url``, marker ``pr_url``,
        ``delivery_done``) reports exactly one PR; only a scan of the CAPTURED
        transcript sees the second ``gh pr create`` output.
        """
        outputs, telemetry, markers = _far1254_blobs()
        urls = find_duplicate_pr_urls(outputs, telemetry, markers)
        assert urls == [_PR_1, _PR_2]

    def test_single_pr_happy_path_is_not_a_breach(self) -> None:
        outputs, telemetry, markers = _one_pr_blobs()
        assert not find_duplicate_pr_urls(outputs, telemetry, markers)

    def test_no_evidence_is_not_a_breach(self) -> None:
        assert not find_duplicate_pr_urls(None, None, None)

    def test_same_url_from_every_source_stays_one_pr(self) -> None:
        """Output + telemetry + marker all naming the SAME PR collapse to one
        distinct URL — three sightings of one PR is not a breach."""
        outputs = {"deliver": {"pr_url": _PR_1}}
        telemetry = {"deliver": {"agent_stdout": f"created {_PR_1}\n"}}
        markers = {"attempt": {"pr_url": _PR_1, "raw_output": f"created {_PR_1}\n"}}
        assert collect_delivery_pr_urls(outputs, telemetry, markers) == [_PR_1]
        assert not find_duplicate_pr_urls(outputs, telemetry, markers)

    def test_transcript_only_second_url_still_breaches(self) -> None:
        """A second URL that appears ONLY in captured stdout (the agent never
        declared it) is still a breach — detection needs no agent cooperation."""
        outputs = {"deliver": {"pr_url": _PR_1}}
        telemetry = {"deliver": {"agent_stdout": f"{_PR_1}\n{_PR_2}\n"}}
        markers: dict[str, Any] | None = None
        urls = find_duplicate_pr_urls(outputs, telemetry, markers)
        assert urls == [_PR_1, _PR_2]

    def test_declared_only_second_url_still_breaches(self) -> None:
        """The transcript may be truncated away; two DECLARED urls still breach."""
        outputs = {"deliver": {"pr_url": _PR_1}, "review": {"pr_url": _PR_2}}
        urls = find_duplicate_pr_urls(outputs, None, None)
        assert urls == [_PR_1, _PR_2]

    def test_non_pr_github_urls_do_not_count(self) -> None:
        """A repo/issue link is not a pull request URL."""
        outputs = {
            "deliver": {
                "pr_url": _PR_1,
                "summary": "see https://github.com/farnalabs/modulo/issues/7 and the repo home",
            }
        }
        assert not find_duplicate_pr_urls(outputs, None, None)

    def test_collection_deduplicates_preserving_first_seen_order(self) -> None:
        outputs = {"a": {"pr_url": _PR_2}}
        telemetry = {"deliver": {"agent_stdout": f"{_PR_1}\n{_PR_2}\n{_PR_1}\n"}}
        urls = collect_delivery_pr_urls(outputs, telemetry, None)
        assert urls == [_PR_2, _PR_1]

    def test_cyclic_blob_terminates(self) -> None:
        """A self-referential blob must not recurse forever."""
        cyclic: dict[str, Any] = {"pr_url": _PR_1}
        cyclic["self"] = cyclic
        telemetry = {"deliver": {"agent_stdout": f"{_PR_2}\n", "cycle": cyclic}}
        urls = collect_delivery_pr_urls(None, telemetry, None)
        assert urls == [_PR_2, _PR_1]

    def test_depth_bound_terminates_on_pathological_nesting(self) -> None:
        """Nesting past the scan depth is ignored rather than walked unbounded."""
        deep: dict[str, Any] = {"pr_url": _PR_2}
        for _ in range(12):
            deep = {"nested": deep}
        urls = collect_delivery_pr_urls(deep, None, None)
        assert not urls


# ---------------------------------------------------------------------------
# Terminal-write enforcement — in-memory SQLite (real Run/Notification tables)
# ---------------------------------------------------------------------------


_TABLES: list[Table] = cast(
    list[Table],
    [
        Run.__table__,
        # The terminalization hook's blob reads go through the run_node_outputs
        # repo reader — the table must exist on this engine.
        RunNodeOutput.__table__,
        # FAR-1274: the breach alert is written in the terminal transaction.
        Notification.__table__,
    ],
)


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    # StaticPool: an in-memory SQLite DB is per-connection; the pool shares ONE
    # connection so sessions AND independent re-read connections all observe the
    # same database.
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    # autobegin=False matches the production DI factory: every DB operation must
    # sit inside an explicit ``async with session.begin():`` block.
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s:
        yield s


async def _seed_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    outputs: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    markers: dict[str, Any] | None = None,
) -> Run:
    run = Run(
        id=run_id,
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        status="running",
        run_number=int(run_id.int % 10**9) + 1,
        input_hash="ih",
        input_payload={},
        langgraph_thread_id=f"thread-{run_id}",
        claim_token="tok-a",
        cancellation_requested=False,
    )
    session.add(run)
    await session.flush()
    await seed_run_blobs(
        session,
        run.id,
        outputs=outputs,
        telemetry=telemetry,
        markers=markers,
        organisation_id=_ORG,
    )
    return run


async def _notifications_for(engine: AsyncEngine, run_id: uuid.UUID) -> list[Notification]:
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s, s.begin():
        rows = await s.execute(select(Notification).where(Notification.action_url == f"/runs/{run_id}"))
        return list(rows.scalars().all())


class TestTerminalWriteEnforcement:
    async def test_breach_alerts_loudly_on_terminal_complete(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A FAR-1254-shaped run terminalizes ``complete`` -> ERROR log + one
        admin-scoped, error-level notification naming BOTH PR URLs."""
        run_id = uuid.uuid4()
        outputs, telemetry, markers = _far1254_blobs()
        with caplog.at_level(logging.ERROR, logger="modulo.db.crud.run"):
            async with session.begin():
                await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
                updated = await update_run_status(session, run_id, "complete")

        assert updated is not None
        assert updated.status == "complete"
        assert "delivery_contract.duplicate_pr" in caplog.text

        rows = await _notifications_for(engine, run_id)
        assert len(rows) == 1
        alert = rows[0]
        assert alert.category == NOTIFICATION_CATEGORY_DUPLICATE_PR
        assert alert.scope == "admin"
        assert alert.level == "error"
        assert alert.action_url == f"/runs/{run_id}"
        assert _PR_1 in alert.body
        assert _PR_2 in alert.body

    async def test_single_pr_happy_path_stays_silent(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """Control: the normal one-PR delivery terminalizes with NO alert and
        still classifies as a delivered run."""
        run_id = uuid.uuid4()
        outputs, telemetry, markers = _one_pr_blobs()
        async with session.begin():
            await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
            updated = await update_run_status(session, run_id, "complete")

        assert updated is not None
        assert updated.status == "complete"
        rows = await _notifications_for(engine, run_id)
        assert not rows

        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as s, s.begin():
            stored = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        classification = stored.run_classification
        assert isinstance(classification, dict)
        assert classification["value"] == "delivered"

    async def test_alert_is_atomic_with_the_terminal_write(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """The alert rides the terminalization TRANSACTION: rolling that
        transaction back removes the alert, so a phantom breach can never
        survive a terminal write that did not commit."""

        async def _terminalize_then_abort(run_id: uuid.UUID, blobs: tuple[Any, Any, Any]) -> None:
            outputs, telemetry, markers = blobs
            async with session.begin():
                await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
                await update_run_status(session, run_id, "complete")
                raise RuntimeError("terminalization rolled back")

        run_id = uuid.uuid4()
        with pytest.raises(RuntimeError, match="terminalization rolled back"):
            await _terminalize_then_abort(run_id, _far1254_blobs())

        rows = await _notifications_for(engine, run_id)
        assert not rows

    async def test_reterminalization_does_not_stack_alerts(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """A retry policy re-terminalizing the run must not stack a second
        identical alert for the same run."""
        run_id = uuid.uuid4()
        outputs, telemetry, markers = _far1254_blobs()
        async with session.begin():
            await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
            await update_run_status(session, run_id, "complete")
        async with session.begin():
            await update_run_status(session, run_id, "complete")

        rows = await _notifications_for(engine, run_id)
        assert len(rows) == 1

    async def test_failed_run_with_two_prs_still_alerts(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """Detection covers every terminal writer that funnels through the hook,
        not only ``complete``: a run that created two PRs then FAILED is a
        breach too."""
        run_id = uuid.uuid4()
        outputs, telemetry, markers = _far1254_blobs()
        async with session.begin():
            await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
            updated = await update_run_status(session, run_id, "failed", error_code="node.cancelled")

        assert updated is not None
        assert updated.status == "failed"
        rows = await _notifications_for(engine, run_id)
        assert len(rows) == 1

    async def test_evidence_read_failure_never_blocks_terminalization(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Detection is best-effort: a failing blob read degrades to a logged
        miss and the terminal status write still lands."""
        from unittest.mock import AsyncMock

        run_id = uuid.uuid4()
        outputs, telemetry, markers = _far1254_blobs()
        monkeypatch.setattr(
            "modulo.db.crud.run.read_run_node_outputs_raw",
            AsyncMock(side_effect=RuntimeError("store unreachable")),
        )
        async with session.begin():
            await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
            updated = await update_run_status(session, run_id, "complete")

        assert updated is not None
        assert updated.status == "complete"
        rows = await _notifications_for(engine, run_id)
        assert not rows
