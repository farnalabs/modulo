"""Unit tests for FAR-189: run-outcome classification persisted at terminalization.

Covers the pure classifier decision table, the pr_url validity + extraction
matrix (node returns via the node-return accessors AND the FAR-188
raw_output_markers column), and the persistence hook wired into the shared
terminal write (``db.crud.run.update_run_status`` / the fenced variant /
``request_cancellation``) — including failure injection (classifier/persist
failures never block terminalization; an ``unclassified`` marker is written),
idempotency (UNIQUE(run_id)), and re-terminalization refresh (upsert).
"""

import builtins
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import StaticPool, Table, event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session as SASession

from modulo.core.pipeline_engine.classify import (
    REASON_BUDGET_EXCEEDED,
    REASON_CANCELLED,
    REASON_DELIVERED,
    REASON_NEEDS_HUMAN,
    REASON_NO_WORK,
    REASON_PARSE_ERROR,
    REASON_SOURCE_ERROR,
    ClassificationResult,
    RunClassificationValue,
    classify_and_persist_run,
    classify_run,
    collect_pr_urls,
    persist_classification,
    reconcile_missing_classifications,
)
from modulo.db.crud.run import update_run_status
from modulo.db.models.base import Base
from modulo.db.models.run import TERMINAL_STATUSES, Run

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

_PR = "https://github.com/farnalabs/modulo/pull/123"
_PR_2 = "https://github.com/farnalabs/modulo/pull/456"


def _node_return_with_pr(pr_url: str) -> dict[str, Any]:
    """A P1 sandbox_agent node return — the pure output_json carrying pr_url."""
    return {"pr_url": pr_url, "summary": "done", "changed_files": ["a.py"]}


def _legacy_envelope_with_pr(pr_url: str) -> dict[str, Any]:
    """A legacy mixed envelope with the pr_url inside ``output``."""
    return {"output": {"pr_url": pr_url, "status": "completed"}, "summary": "done"}


def _artifacts_envelope_with_pr(pr_url: str) -> dict[str, Any]:
    return {"artifacts": [{"output": {"output_json": {"pr_url": pr_url}, "status": "completed"}}]}


def _markers(*pr_urls: str) -> dict[str, dict[str, Any]]:
    """raw_output_markers keyed by attempt_key, each carrying a pr_url."""
    return {
        f"attempt-{i}": {
            "_modulo_marker": True,
            "status": "failed",
            "pr_url": pr_url,
            "parse_error": "",
            "attempt_key": f"attempt-{i}",
        }
        for i, pr_url in enumerate(pr_urls)
    }


# ---------------------------------------------------------------------------
# Pure decision-table tests
# ---------------------------------------------------------------------------


class TestDecisionTable:
    """Spec §6 — keyed on (status, error_code), never prose."""

    @pytest.mark.parametrize(
        "status,pr_urls,expected,expected_reason",
        [
            ("complete", (), RunClassificationValue.no_delivery, REASON_NO_WORK),
            ("complete", (_PR,), RunClassificationValue.delivered, REASON_DELIVERED),
            ("failed", (), RunClassificationValue.no_delivery, None),
            ("failed", (_PR,), RunClassificationValue.no_delivery, None),
            ("eval_failed", (), RunClassificationValue.no_delivery, None),
            ("eval_failed", (_PR,), RunClassificationValue.no_delivery, None),
            ("stalled", (), RunClassificationValue.no_delivery, None),
            ("stalled", (_PR,), RunClassificationValue.no_delivery, None),
            ("cancelled", (), RunClassificationValue.excluded, REASON_CANCELLED),
            ("cancelled", (_PR,), RunClassificationValue.excluded, REASON_CANCELLED),
            ("budget_exceeded", (), RunClassificationValue.excluded, REASON_BUDGET_EXCEEDED),
            ("budget_exceeded", (_PR,), RunClassificationValue.excluded, REASON_BUDGET_EXCEEDED),
        ],
    )
    def test_terminal_status_matrix(
        self,
        status: str,
        pr_urls: tuple[str, ...],
        expected: RunClassificationValue,
        expected_reason: str | None,
    ) -> None:
        outputs = {f"n{i}": _node_return_with_pr(url) for i, url in enumerate(pr_urls)}
        telemetry = {f"n{i}": {"agent_status": "completed", "agent_outcome": "success"} for i in range(len(pr_urls))}
        result = classify_run(status, None, outputs_json=outputs, telemetry_json=telemetry)
        assert result.value == expected
        if expected_reason is not None:
            assert result.reason == expected_reason

    def test_complete_with_invalid_pr_url_is_no_delivery(self) -> None:
        # A pr_url that does not parse as http(s) + netloc is NOT a delivery.
        outputs = {"n1": _node_return_with_pr("not a url")}
        result = classify_run("complete", None, outputs_json=outputs, telemetry_json={"n1": {}})
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_NO_WORK

    def test_non_terminal_status_is_guarded_excluded(self) -> None:
        result = classify_run("running", None)
        assert result.value == RunClassificationValue.excluded
        assert result.reason.startswith("unrecognized_status")

    def test_new_terminal_status_fails_loudly_not_complete(self) -> None:
        """FIX 6: a terminal status outside the excluded/countable buckets AND
        != complete classifies as excluded — a new status added to
        TERMINAL_STATUSES must fail loudly in tests, never silently inherit
        complete semantics."""
        with patch(
            "modulo.db.models.run.TERMINAL_STATUSES",
            frozenset({*TERMINAL_STATUSES, "expired"}),
        ):
            result = classify_run("expired", None)
        assert result.value == RunClassificationValue.excluded
        assert result.reason.startswith("unrecognized_status")


class TestPrUrlValidity:
    """Spec §2 — urlsplit with scheme http/https + non-empty netloc."""

    @pytest.mark.parametrize(
        "url,valid",
        [
            ("https://github.com/farnalabs/modulo/pull/1", True),
            ("https://github.com/farnalabs/modulo", True),
            ("http://example.com/x", True),
            ("https://", False),
            ("http://", False),
            ("ftp://github.com/farnalabs/modulo/pull/2", False),
            ("not a url", False),
            ("github.com/farnalabs/modulo/pull/3", False),
            ("", False),
        ],
    )
    def test_validity(self, url: str, valid: bool) -> None:
        result = classify_run(
            "complete",
            None,
            outputs_json={"n1": _node_return_with_pr(url)},
            telemetry_json={"n1": {}},
        )
        expected = RunClassificationValue.delivered if valid else RunClassificationValue.no_delivery
        assert result.value == expected


class TestPrUrlSources:
    """delivered signals recovered from node returns AND raw_output_markers."""

    def test_pr_url_in_node_return_direct(self) -> None:
        outputs = {"n1": _node_return_with_pr(_PR)}
        result = classify_run(
            "complete",
            None,
            outputs_json=outputs,
            telemetry_json={"n1": {"agent_status": "completed", "agent_outcome": "success"}},
        )
        assert result.value == RunClassificationValue.delivered
        assert _PR in result.delivered_pr_urls

    def test_pr_url_in_legacy_envelope(self) -> None:
        outputs = {"n1": _legacy_envelope_with_pr(_PR)}
        result = classify_run("complete", None, outputs_json=outputs, telemetry_json={})
        assert result.value == RunClassificationValue.delivered
        assert _PR in result.delivered_pr_urls

    def test_pr_url_in_artifacts_envelope(self) -> None:
        outputs = {"n1": _artifacts_envelope_with_pr(_PR)}
        result = classify_run("complete", None, outputs_json=outputs, telemetry_json={})
        assert result.value == RunClassificationValue.delivered

    def test_pr_url_nested_in_raw_output_markers_any_attempt_key(self) -> None:
        """A pr_url recovered from ANY attempt_key is a valid delivery signal —
        first-attempt PRs created before a sandbox stall/retry are real."""
        result = classify_run(
            "complete",
            None,
            outputs_json=None,
            telemetry_json=None,
            raw_output_markers=_markers(_PR, _PR_2),
        )
        assert result.value == RunClassificationValue.delivered
        assert result.delivered_pr_urls == (_PR, _PR_2)

    def test_pr_url_both_sources_deduplicated(self) -> None:
        outputs = {"n1": _node_return_with_pr(_PR)}
        markers = _markers(_PR)
        urls = collect_pr_urls(outputs, {"n1": {}}, markers)
        assert urls == [_PR]

    def test_pr_url_only_in_telemetry_value_is_delivered(self) -> None:
        """FIX 2: a pr_url carried ONLY in a node telemetry VALUE (not the node
        return) is a real delivery signal — telemetry VALUES are scanned, not
        just keys."""
        outputs = {"n1": {"summary": "no pr_url here"}}
        telemetry = {"n1": {"agent_status": "completed", "agent_outcome": "success", "pr_url": _PR}}
        result = classify_run("complete", None, outputs_json=outputs, telemetry_json=telemetry)
        assert result.value == RunClassificationValue.delivered
        assert _PR in result.delivered_pr_urls

    def test_pr_url_in_both_node_and_telemetry_deduplicated(self) -> None:
        outputs = {"n1": _node_return_with_pr(_PR)}
        telemetry = {"n1": {"agent_status": "completed", "agent_outcome": "success", "pr_url": _PR}}
        urls = collect_pr_urls(outputs, telemetry, None)
        assert urls == [_PR]

    def test_failed_with_pr_url_from_markers_is_still_no_delivery(self) -> None:
        # A failed run is COUNTABLE no_delivery regardless of any pr_url
        # evidence (the pr_url matters only for the complete verdict).
        result = classify_run(
            "failed",
            "node.cancelled",
            outputs_json=None,
            telemetry_json=None,
            raw_output_markers=_markers(_PR),
        )
        assert result.value == RunClassificationValue.no_delivery


class TestReasons:
    """Spec §7 — reason on no_delivery: no_work / needs_human / source_error /
    parse_error when derivable, else no_delivery."""

    def test_complete_no_pr_url_reason_no_work(self) -> None:
        result = classify_run("complete", None)
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_NO_WORK

    def test_failed_plain_reason_no_delivery(self) -> None:
        result = classify_run("failed", None)
        assert result.reason == "no_delivery"

    def test_failed_source_error(self) -> None:
        # infra/sandbox crash elevated to failed — source_error (PO: counts).
        result = classify_run("failed", "node.cancelled")
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_SOURCE_ERROR

    def test_failed_legacy_code_resolved_to_source_error(self) -> None:
        result = classify_run("stalled", "node_timeout")
        assert result.reason == REASON_SOURCE_ERROR

    def test_failed_needs_human(self) -> None:
        result = classify_run("failed", "harness.gate_creation_failed")
        assert result.reason == REASON_NEEDS_HUMAN

    def test_failed_parse_error_from_marker(self) -> None:
        markers = {
            "attempt-0": {
                "_modulo_marker": True,
                "status": "failed",
                "pr_url": "",
                "parse_error": "JSONDecodeError: Expecting value",
                "attempt_key": "attempt-0",
            }
        }
        result = classify_run("failed", "sandbox.no_output_json", raw_output_markers=markers)
        assert result.reason == REASON_PARSE_ERROR

    def test_cancelled_unparseable_reason_is_excluded(self) -> None:
        # Spec: unparseable-reason default for status=cancelled is excluded,
        # never countable.
        result = classify_run("cancelled", "junk_error_code", raw_output_markers=_markers(_PR))
        assert result.value == RunClassificationValue.excluded
        assert result.reason == REASON_CANCELLED

    def test_declared_success_nodes_recorded(self) -> None:
        outputs = {
            "n1": _node_return_with_pr(_PR),
            "n2": {"summary": "no agent_outcome"},
        }
        telemetry = {
            "n1": {"agent_status": "completed", "agent_outcome": "success"},
            "n2": {"agent_status": "completed", "agent_outcome": "failed"},
        }
        result = classify_run("complete", None, outputs_json=outputs, telemetry_json=telemetry)
        assert result.declared_success_nodes == 1

    def test_work_intact_recorded_as_metadata(self) -> None:
        result = classify_run("complete", None, work_intact=True)
        assert result.work_intact is True


# ---------------------------------------------------------------------------
# Persistence hook — in-memory SQLite (real Run table + real update_run_status)
# ---------------------------------------------------------------------------


_TABLES: list[Table] = cast(list[Table], [Run.__table__])


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    # StaticPool: an in-memory SQLite DB is per-connection; the pool shares ONE
    # connection so sessions AND the independent _read_classification
    # connections all observe the same database.
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
    status: str = "running",
    outputs: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    markers: dict[str, Any] | None = None,
    error_code: str | None = None,
    work_intact: bool | None = None,
) -> Run:
    run = Run(
        id=run_id,
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        status=status,
        run_number=int(run_id.int % 10**9) + 1,
        input_hash="ih",
        input_payload={},
        langgraph_thread_id=f"thread-{run_id}",
        claim_token="tok-a",
        cancellation_requested=False,
        raw_output_markers=markers,
        outputs_json=outputs,
        node_telemetry_json=telemetry,
        error_code=error_code,
        work_intact=work_intact,
    )
    session.add(run)
    await session.flush()
    return run


async def _read_classification(engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, Any] | None:
    # ORM select (not raw text): SQLAlchemy applies the Uuid() type conversion
    # — a raw ``str(uuid)`` bind silently misses SQLite's CHAR(32) id storage.
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s, s.begin():
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    return run.run_classification if run is not None else None


class TestPersistenceHook:
    async def test_update_run_status_writes_classification_atomically(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            updated = await update_run_status(
                session,
                run_id,
                "complete",
                outputs_json={"n1": _node_return_with_pr(_PR)},
                node_telemetry_json={"n1": {"agent_status": "completed", "agent_outcome": "success"}},
            )
        assert updated is not None
        assert updated.status == "complete"

        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "delivered"
        assert _PR in record["delivered_pr_urls"]

    async def test_failed_run_classifies_no_delivery(self, engine: AsyncEngine, session: AsyncSession) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            await update_run_status(session, run_id, "failed", error_code="node.cancelled")
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "no_delivery"
        assert record["reason"] == REASON_SOURCE_ERROR

    async def test_cancelled_via_request_cancellation_classifies_excluded(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        from modulo.db.crud.run import request_cancellation

        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            run = await request_cancellation(session, run_id)
        assert run is not None
        assert run.status == "cancelled"
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "excluded"
        assert record["reason"] == REASON_CANCELLED

    async def test_non_terminal_write_leaves_no_record(self, engine: AsyncEngine, session: AsyncSession) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            await update_run_status(session, run_id, "running", claimed_by="worker")
        assert await _read_classification(engine, run_id) is None

    async def test_work_intact_flows_through_orm_persist(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 3: work_intact is read from the MAPPED ORM column (migration 0091)
        and recorded as metadata — the terminalization write observes it."""
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id, status="complete", work_intact=True)
            await update_run_status(session, run_id, "complete")
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "no_delivery"
        assert record["work_intact"] is True


class TestCrossTenantIsolation:
    async def test_cross_tenant_terminalization_never_classifies_other_org(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 9: org A terminalizing a run must not classify org B's run.

        Exercises the generic-backend tenant filter path (the SQLite analogue
        of Postgres RLS): with ``session.info["org_id"] = org_a`` the terminal
        select is scoped to org A, so org B's run is invisible and no
        classification record can be written for it.
        """
        from modulo.db.rls import _inject_tenant_filter

        org_a = uuid.UUID("00000000-0000-0000-0000-0000000000a9")
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)  # organisation_id = _ORG (org B)

        event.listen(SASession, "do_orm_execute", _inject_tenant_filter)
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
            async with maker() as org_a_session:
                org_a_session.info["org_id"] = org_a
                async with org_a_session.begin():
                    updated = await update_run_status(
                        org_a_session,
                        run_id,
                        "complete",
                        outputs_json={"n1": _node_return_with_pr(_PR)},
                        node_telemetry_json={"n1": {"agent_status": "completed", "agent_outcome": "success"}},
                    )
                # The tenant filter injects WHERE organisation_id = org_a, so org A
                # cannot even see org B's run — nothing is terminalized, nothing
                # classified.
                assert updated is None
        finally:
            event.remove(SASession, "do_orm_execute", _inject_tenant_filter)
        assert await _read_classification(engine, run_id) is None


class TestFailureAndIdempotency:
    async def test_classifier_failure_persists_unclassified_and_terminalization_survives(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            with patch("modulo.core.pipeline_engine.classify.classify_run", side_effect=RuntimeError("boom")):
                updated = await update_run_status(session, run_id, "complete")
        assert updated is not None
        assert updated.status == "complete"
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "unclassified"

    async def test_persist_failure_never_blocks_terminalization(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            with patch(
                "modulo.core.pipeline_engine.classify.persist_classification",
                side_effect=RuntimeError("boom"),
            ):
                updated = await update_run_status(session, run_id, "complete")
        assert updated is not None
        assert updated.status == "complete"

    async def test_classifier_run_twice_is_one_record(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            run = await _seed_run(
                session,
                run_id,
                status="complete",
                outputs={"n1": _node_return_with_pr(_PR)},
                telemetry={"n1": {"agent_status": "completed", "agent_outcome": "success"}},
            )
            await classify_and_persist_run(session, run)
            await classify_and_persist_run(session, run)
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "delivered"
        assert record["delivered_pr_urls"] == [_PR]

    async def test_re_terminalization_refreshes_classification(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """Retry policy re-flips a classified failed run to pending, then a
        re-run terminalizes with new evidence — the record is UPSERTED (refreshed),
        not duplicated and not frozen at the stale verdict."""
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
            await update_run_status(session, run_id, "failed", error_code="node.cancelled")
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "no_delivery"

        async with session.begin():
            await update_run_status(session, run_id, "pending", clear_error_code=True)
        # The pending flip is non-terminal — the stale record must be untouched.
        assert (await _read_classification(engine, run_id))["value"] == "no_delivery"

        async with session.begin():
            await update_run_status(
                session,
                run_id,
                "complete",
                outputs_json={"n1": _node_return_with_pr(_PR)},
                node_telemetry_json={"n1": {"agent_status": "completed", "agent_outcome": "success"}},
            )
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "delivered"
        assert _PR in record["delivered_pr_urls"]

    async def test_persist_failure_fallback_returns_false(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        async with session.begin():
            run = await _seed_run(session, uuid.uuid4(), status="complete")
            with patch(
                "modulo.core.pipeline_engine.classify.persist_classification",
                new=AsyncMock(return_value=False),
            ):
                ok = await classify_and_persist_run(session, run)
        assert ok is False

    async def test_terminalization_survives_classifier_import_failure(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 4: an unguarded classifier import raising inside the terminal write
        must NOT roll back the terminal status write — an unclassified marker is
        written directly instead."""
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id)
        real_import = builtins.__import__

        def _failing_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "modulo.core.pipeline_engine.classify":
                raise ImportError("simulated classifier import failure")
            return real_import(name, *args, **kwargs)

        async with session.begin():
            with patch("builtins.__import__", side_effect=_failing_import):
                updated = await update_run_status(session, run_id, "failed", error_code="node.cancelled")
        assert updated is not None
        assert updated.status == "failed"
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "unclassified"

    async def test_status_guarded_persist_cannot_overwrite_stale_record(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 5/10: a stale verdict persisted with a status guard that no longer
        matches writes 0 rows and returns failure — the fresh record survives."""
        run_id = uuid.uuid4()
        async with session.begin():
            run = await _seed_run(session, run_id, status="complete")
            await classify_and_persist_run(session, run)
        existing = await _read_classification(engine, run_id)
        assert existing is not None and existing["value"] == "no_delivery"

        async with session.begin():
            run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            stale = ClassificationResult(
                RunClassificationValue.delivered,
                REASON_DELIVERED,
                delivered_pr_urls=(_PR,),
            )
            ok = await persist_classification(session, run, stale, expected_status="failed")
            assert ok is False
        after = await _read_classification(engine, run_id)
        assert after == existing

    async def test_persist_zero_rows_reports_failure_not_success(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 10: a silently RLS-filtered / status-guarded 0-row UPDATE must never
        report success with no record."""
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id, status="complete")
        async with session.begin():
            run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            result = classify_run("complete", None)
            ok = await persist_classification(session, run, result, expected_status="failed")
        assert ok is False
        assert await _read_classification(engine, run_id) is None

    async def test_persist_failure_never_leaves_record_missing(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """FIX 11: on persist failure the simpler unclassified marker is written —
        a terminal run NEVER commits with run_classification = NULL."""
        run_id = uuid.uuid4()
        async with session.begin():
            run = await _seed_run(session, run_id, status="complete")
            with patch(
                "modulo.core.pipeline_engine.classify.persist_classification",
                new=AsyncMock(return_value=False),
            ):
                ok = await classify_and_persist_run(session, run)
        assert ok is False
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "unclassified"

    async def test_result_to_dict_roundtrip(self) -> None:
        result = ClassificationResult(
            RunClassificationValue.delivered,
            REASON_DELIVERED,
            delivered_pr_urls=(_PR,),
        )
        payload = result.to_dict()
        assert payload["value"] == "delivered"
        assert payload["delivered_pr_urls"] == [_PR]
        assert "computed_at" in payload


class TestSweep:
    async def test_reconcile_backfills_terminal_runs_without_record(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            await _seed_run(session, run_id, status="failed", error_code="task_failure")

        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        summary = await reconcile_missing_classifications(maker, org_ids=[_ORG], max_runs=10, budget_seconds=30.0)

        assert summary["scanned"] >= 1
        assert summary["classified"] == 1
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "no_delivery"

    async def test_reconcile_skips_already_classified(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        run_id = uuid.uuid4()
        async with session.begin():
            run = await _seed_run(session, run_id, status="failed")
            await classify_and_persist_run(session, run)

        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        summary = await reconcile_missing_classifications(maker, org_ids=[_ORG], max_runs=10, budget_seconds=30.0)
        assert summary["scanned"] == 0


class TestPeriodicWiring:
    """FIX 1: the reconciliation sweep is wired into a periodic production path."""

    async def test_reconcile_sweep_wired_into_periodic_cron_path(self) -> None:
        """The cron_helpers entrypoint (invoked by dispatcher_reconcile every
        60s) actually calls the sweep — proven by patching the sweep."""
        from modulo.core import cron_helpers
        from modulo.core.pipeline_engine import classify as classify_module

        with (
            patch.object(
                classify_module,
                "reconcile_missing_classifications",
                new=AsyncMock(return_value={"scanned": 0, "classified": 0, "unclassified": 0, "errors": 0}),
            ) as sweep_mock,
            patch.object(cron_helpers, "_open_factory"),
        ):
            summary = await cron_helpers.run_classification_reconcile()
        assert sweep_mock.await_count == 1
        assert summary == {"scanned": 0, "classified": 0, "unclassified": 0, "errors": 0}

    async def test_run_classification_reconcile_classifies_through_periodic_path(
        self,
        engine: AsyncEngine,
    ) -> None:
        """End-to-end: the periodic entrypoint backfills an unclassified terminal
        run (raw-SQL-terminalizer shape) against a real DB."""
        from modulo.core import cron_helpers

        run_id = uuid.uuid4()
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as s, s.begin():
            await _seed_run(s, run_id, status="failed", error_code="task_failure")

        with patch.object(cron_helpers, "_open_factory", return_value=maker):
            summary = await cron_helpers.run_classification_reconcile()
        assert summary["scanned"] >= 1
        assert summary["classified"] == 1
        record = await _read_classification(engine, run_id)
        assert record is not None
        assert record["value"] == "no_delivery"
