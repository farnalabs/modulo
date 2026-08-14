"""Unit tests for FAR-189: run-outcome classification persisted at terminalization.

Covers the pure classifier decision table, the pr_url validity + extraction
matrix (node returns via the node-return accessors AND the FAR-188
raw_output_markers column), and the persistence hook wired into the shared
terminal write (``db.crud.run.update_run_status`` / the fenced variant /
``request_cancellation``) — including failure injection (classifier/persist
failures never block terminalization; an ``unclassified`` marker is written),
idempotency (UNIQUE(run_id)), and re-terminalization refresh (upsert).
"""

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import StaticPool, Table, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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
    reconcile_missing_classifications,
)
from modulo.db.crud.run import update_run_status
from modulo.db.models.base import Base
from modulo.db.models.run import Run

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

    async def test_result_to_json_roundtrip(self) -> None:
        result = ClassificationResult(
            RunClassificationValue.delivered,
            REASON_DELIVERED,
            delivered_pr_urls=(_PR,),
        )
        parsed = json.loads(result.to_json())
        assert parsed["value"] == "delivered"
        assert parsed["delivered_pr_urls"] == [_PR]
        assert "computed_at" in parsed


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
