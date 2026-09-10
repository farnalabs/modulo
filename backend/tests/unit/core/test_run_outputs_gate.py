"""Unit tests for the FAR-583 run-outputs chokepoint gates (qa iteration 1).

Covers the chokepoint-side fixes that the repo/migration fix Worker did not
own:

* qa rider (f) — a transaction-aborting savepoint failure (deadlock /
  shutdown / connection loss) logs ``legacy_marker_also_lost`` (the legacy
  write is rolled back too) instead of claiming the 0177 repair migrates;
* qa M4/M5 — the FAR-228 gate read and the connector rewrite read consume the
  repo's SINGLE fenced markers reader (``read_run_markers_fenced``): no
  predicate SELECT outside the repo, one statement, lock-preserving;
* qa M15's sibling contract pins live in ``tests/unit/api/test_runs_endpoint.py``;
* qa M16 — the guard obligation: every blobs-passing ``update_run_status``
  call site in ``backend/src`` is wrapped in ``guard_dual_write`` (AST pin),
  and the docstrings document the Raises contract;
* qa M18 — the recovery failure path carries the run's ``claim_token`` into
  the orchestration (a successor's re-claim must never be terminalized).

The marker savepoint's kill-switch gating was removed at B2a (FAR-694) with
the switch machinery — the new-table write is unconditional — so the
switch-gated tests went with it.

DB-backed cases run on in-memory SQLite with ``Base.metadata.create_all`` over
the involved tables only (the ``test_run_outputs_dualwrite`` harness).
"""

from __future__ import annotations

import ast
import inspect
import logging
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, ClassVar, Self
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from tests.unit._legacy_seed import seed_legacy_blobs

from modulo.core.pipeline_engine.node_runner import (
    _read_connector_idempotency_gate_state,
    _read_run_raw_output_markers_for_gate,
    _write_raw_output_marker,
)
from modulo.db.crud.run_node_outputs import DualWriteError, read_run_markers_fenced
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import Run
from modulo.db.models.run_node_outputs import RunNodeOutput
from modulo.db.rls import set_rls_org

_SRC = Path(__file__).resolve().parent.parent.parent.parent / "src" / "modulo"

_ORG = uuid.uuid4()
_RUN_ID = uuid.uuid4()

_RUN_AND_ORG_TABLES = (Organisation.__table__, Run.__table__, RunNodeOutput.__table__)


# ---------------------------------------------------------------------------
# SQLite harness (the test_run_outputs_dualwrite pattern)
# ---------------------------------------------------------------------------


async def _now_sqlite(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_now(dbapi_connection: Any, connection_record: Any) -> None:
        dbapi_connection.create_function("now", 0, lambda: "now")


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    await _now_sqlite(eng)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_RUN_AND_ORG_TABLES))
        # FAR-583 B1: the legacy blob columns left the ORM mapping but remain
        # IN THE DATABASE until B2b — the marker path's raw-SQL legacy leg
        # (read_legacy_raw_output_markers / write_legacy_raw_output_markers)
        # selects them, so the test schema reproduces the migrated shape.
        for legacy_col in ("outputs_json", "node_telemetry_json", "raw_output_markers"):
            await conn.exec_driver_sql(f"ALTER TABLE runs ADD COLUMN {legacy_col} JSON")
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
def sqlite_sessionmaker(sqlite_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(sqlite_engine, expire_on_commit=False, autobegin=False)


async def _seed_run(
    maker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    status: str = "running",
) -> None:
    async with maker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, 'gate org', :slug, '{}', '{}')"
            ),
            {"id": str(_ORG), "slug": f"gate-{_ORG.hex[:12]}"},
        )
        await session.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "run_number, input_hash, langgraph_thread_id, claim_token, cancellation_requested) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :status, 1, 'ih', :thread, 'tok-a', 0)"
            ),
            {
                "id": run_id.hex,
                "oid": _ORG.hex,
                "pid": uuid.uuid4().hex,
                "sid": uuid.uuid4().hex,
                "status": status,
                "thread": f"gate-{run_id}",
            },
        )


async def _load_run(maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> Run:
    async with maker() as session, session.begin():
        return (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()


# ---------------------------------------------------------------------------
# the marker savepoint (unconditional post-B2a) + the legacy-lost classification
# ---------------------------------------------------------------------------


class TestMarkerSavepointWrite:
    @pytest.mark.asyncio
    async def test_write_persists_the_merged_marker_row(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The savepoint leg runs the REAL ``write_run_markers`` — one row
        per attempt key in the new table."""
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        await _write_raw_output_marker(
            sqlite_sessionmaker,
            org_uuid=_ORG,
            run_id=str(run_id),
            node_id="n1",
            attempt_key=None,
            marker={"status": "failed", "raw_output": "raw"},
        )
        async with sqlite_sessionmaker() as check, check.begin():
            rows = (await check.execute(select(RunNodeOutput).where(RunNodeOutput.run_id == run_id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].attempt_key == f"run:{run_id}:node:n1:fallback"
        assert rows[0].node_id == "n1"

    @pytest.mark.asyncio
    async def test_missing_org_is_an_explicit_runtime_error(
        self,
        sqlite_sessionmaker: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """qa rider (e): the org guard is an explicit RuntimeError (asserts
        vanish under -O) — surfaced through the never-raise persist contract's
        failure log."""
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)

        caplog.set_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner")
        await _write_raw_output_marker(
            sqlite_sessionmaker,
            org_uuid=None,
            run_id=str(run_id),
            node_id="n1",
            attempt_key=None,
            marker={"status": "failed", "raw_output": "raw"},
        )
        assert "requires a parsed organisation id" in caplog.text, (
            "the explicit RuntimeError must surface through the persist-failure log"
        )


class TestMarkerSavepointLegacyLostClassification:
    @pytest.mark.asyncio
    async def test_txn_aborting_failure_logs_legacy_marker_also_lost(
        self,
        sqlite_sessionmaker: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """qa rider (f): a 40P01-class savepoint failure poisons the WHOLE
        transaction — the log must say the legacy marker is lost too, not
        claim the 0177 repair migrates."""
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)

        def _failing_write(*args: Any, **kwargs: Any) -> Any:
            err = OperationalError("stmt", {}, Exception("deadlock detected"))
            err.orig = type("_FakePG", (Exception,), {"sqlstate": "40P01"})("deadlock detected")
            raise err

        caplog.set_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner")
        with patch("modulo.db.crud.run_node_outputs.write_run_markers", _failing_write):
            await _write_raw_output_marker(
                sqlite_sessionmaker,
                org_uuid=_ORG,
                run_id=str(run_id),
                node_id="n1",
                attempt_key=None,
                marker={"status": "failed", "raw_output": "raw"},
            )
        assert any("raw_output_marker_legacy_marker_also_lost" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_savepoint_scoped_failure_keeps_the_legacy_claim(
        self,
        sqlite_sessionmaker: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A savepoint-SCOPED failure (hard SQLSTATE) does NOT fire the
        legacy-lost log — the legacy marker write above the savepoint still
        commits."""
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)

        def _failing_write(*args: Any, **kwargs: Any) -> Any:
            err = OperationalError("stmt", {}, Exception("permission denied"))
            err.orig = type("_FakePG", (Exception,), {"sqlstate": "42501"})("permission denied")
            raise err

        caplog.set_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner")
        with patch("modulo.db.crud.run_node_outputs.write_run_markers", _failing_write):
            await _write_raw_output_marker(
                sqlite_sessionmaker,
                org_uuid=_ORG,
                run_id=str(run_id),
                node_id="n1",
                attempt_key=None,
                marker={"status": "failed", "raw_output": "raw"},
            )
        assert not any("legacy_marker_also_lost" in r.message for r in caplog.records)
        # FAR-583 B1: the legacy column's ORM mapping is cut — read it through
        # the repo's raw parameterised-SQL helper.
        from modulo.db.crud.run_node_outputs import read_legacy_raw_output_markers

        async with sqlite_sessionmaker() as check, check.begin():
            legacy = await read_legacy_raw_output_markers(check, run_id=run_id)
        assert legacy is not None, "the legacy marker survives a savepoint-scoped failure"


# ---------------------------------------------------------------------------
# qa M4/M5 — the gate reads consume the single fenced reader
# ---------------------------------------------------------------------------


class _CountingSession:
    """A fake session that counts execute() calls (statement-count pins)."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.execute_calls: list[Any] = []
        self._rows = rows or []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: Any, *args: object, **kwargs: object) -> Any:
        self.execute_calls.append(stmt)
        rows = list(self._rows)

        class _Result:
            def fetchone(self) -> Any:
                return rows[0] if rows else None

            def all(self) -> list[Any]:
                return rows

        return _Result()


class TestGateReadUsesFencedReader:
    @pytest.mark.asyncio
    async def test_gate_read_issues_no_statement_outside_the_repo(self) -> None:
        """qa M4/M5: the FAR-228 gate read is ONE fenced repo call — no
        predicate SELECT outside the repo reader (the old shape was a
        predicate SELECT + a fat all-sides fallback read)."""
        fenced_calls: list[dict[str, Any]] = []

        async def _fenced(session: Any, **kwargs: Any) -> dict[str, Any] | None:
            fenced_calls.append(kwargs)
            return {"run:x:node:n1:1": {"delivery_done": True}}

        session = _CountingSession()
        with (
            patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
            patch("modulo.db.rls.set_rls_execution_context", new=AsyncMock()),
            patch("modulo.db.crud.run_node_outputs.read_run_markers_fenced", _fenced),
        ):
            markers = await _read_run_raw_output_markers_for_gate(
                lambda: session,
                run_id=str(_RUN_ID),
                org_id_raw=str(_ORG),
                claim_lease="tok-1",
                node_id="n1",
            )
        assert not session.execute_calls, "the gate read must not issue a statement outside the repo reader"
        assert fenced_calls == [
            {
                "run_id": _RUN_ID,
                "organisation_id": _ORG,
                "claim_token": "tok-1",
                "for_update": False,
            }
        ]
        assert markers is not None
        assert "run:x:node:n1:1" in markers

    @pytest.mark.asyncio
    async def test_gate_read_fence_miss_serves_none(self) -> None:
        async def _fenced(session: Any, **kwargs: Any) -> None:
            return None

        session = _CountingSession()
        with (
            patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
            patch("modulo.db.rls.set_rls_execution_context", new=AsyncMock()),
            patch("modulo.db.crud.run_node_outputs.read_run_markers_fenced", _fenced),
        ):
            markers = await _read_run_raw_output_markers_for_gate(
                lambda: session,
                run_id=str(_RUN_ID),
                org_id_raw=str(_ORG),
                claim_lease="tok-stale",
                node_id="n1",
            )
        assert markers is None, "a fence miss must serve None (byte-for-byte the old visibility)"

    @pytest.mark.asyncio
    async def test_connector_gate_read_uses_fenced_reader_with_row_lock(self) -> None:
        """qa M5: the connector rewrite read reassembles the markers via the
        fenced reader with ``for_update=True`` (the run-row lock preserved);
        the persisted idempotency key still comes from the run row."""
        fenced_calls: list[dict[str, Any]] = []

        async def _fenced(session: Any, **kwargs: Any) -> dict[str, Any] | None:
            fenced_calls.append(kwargs)
            return {"run:x:node:n1:connector": {"delivery_done": True}}

        session = _CountingSession(rows=[("run-row-id", "key-1")])
        with (
            patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
            patch("modulo.db.rls.set_rls_execution_context", new=AsyncMock()),
            patch("modulo.db.crud.run_node_outputs.read_run_markers_fenced", _fenced),
        ):
            markers, persisted_key = await _read_connector_idempotency_gate_state(
                lambda: session,
                run_id=str(_RUN_ID),
                org_id_raw=str(_ORG),
                node_id="n1",
            )
        assert persisted_key == "key-1"
        assert fenced_calls == [
            {
                "run_id": _RUN_ID,
                "organisation_id": _ORG,
                "claim_token": None,
                "for_update": True,
                "fence_status": False,
            }
        ], (
            "the connector read must fence by other means (no claim token) WITH the row lock, "
            "and WITHOUT the status fence (qa Minor 4 — the old read had no status predicate)"
        )
        assert markers is not None
        assert "run:x:node:n1:connector" in markers
        assert len(session.execute_calls) == 1, "only the idempotency-key SELECT remains outside the repo"


class TestFenceStatusVisibilityDuringCancel:
    """qa Minor 4: the connector rewrite read (fence_status=False) preserves
    the OLD read's no-status-predicate semantics — during a concurrent cancel
    it still serves the suppression evidence; the FAR-228 gate read
    (fence_status=True) keeps the status fence — a cancelled run serves it
    NOTHING. Exercised against the REAL reader on the SQLite harness."""

    _MARKERS: ClassVar[dict[str, Any]] = {"k1": {"raw": "a"}}

    async def _seed_cancelled_run(self, maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id, status="cancelled")
        # The markers land via the repo's raw Core legacy table (FAR-583 B1 —
        # the ORM mapping of the column is cut) so the raw-SQL fenced reader
        # sees the JSON payload exactly as the migrated database stores it.
        async with maker() as session, session.begin():
            await seed_legacy_blobs(session, run_id, raw_output_markers=self._MARKERS)
        return run_id

    @pytest.mark.asyncio
    async def test_connector_shape_read_serves_markers_of_a_cancelled_run(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The connector caller's shape (for_update=True, claim_token=None,
        fence_status=False) — a cancelled run's markers are STILL served, so
        a delivery_done stamped just before the cancel keeps suppressing the
        duplicate connector write exactly like the old no-status read."""
        run_id = await self._seed_cancelled_run(sqlite_sessionmaker)
        async with sqlite_sessionmaker() as session, session.begin():
            await set_rls_org(session, _ORG)
            served = await read_run_markers_fenced(
                session,
                run_id=run_id,
                organisation_id=_ORG,
                claim_token=None,
                for_update=True,
                fence_status=False,
            )
        assert served == self._MARKERS

    @pytest.mark.asyncio
    async def test_gate_shape_read_serves_none_for_a_cancelled_run(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The FAR-228 gate caller's shape (fence_status=True default) — the
        status fence holds: a cancelled run's markers are NOT served."""
        run_id = await self._seed_cancelled_run(sqlite_sessionmaker)
        async with sqlite_sessionmaker() as session, session.begin():
            await set_rls_org(session, _ORG)
            served = await read_run_markers_fenced(
                session,
                run_id=run_id,
                organisation_id=_ORG,
                claim_token="tok-a",
            )
        assert served is None


# ---------------------------------------------------------------------------
# qa M16 — the guard obligation is enforced + documented
# ---------------------------------------------------------------------------

_GUARDED_SITES: set[tuple[str, str]] = {
    ("core/cost_controller/finalize.py", "_fallback_write"),
    ("core/cost_controller/finalize.py", "_reduced_escape"),
    ("core/cost_controller/finalize.py", "_write_finalized_run"),
}
_UNGUARDED_SITES: set[tuple[str, str]] = {
    ("core/cost_controller/finalize.py", "_write_empty_terminal"),
    ("core/pipeline_engine/executor.py", "_check_capacity"),
    ("core/pipeline_engine/executor.py", "_check_spend_ceiling_gate"),
    ("core/pipeline_engine/executor.py", "_claim_run_and_audit"),
    ("core/pipeline_engine/executor.py", "resume"),
    ("core/dispatch.py", "_org_capacity_deferred"),
    # ('api/routes/hitl.py', 'claim_gate') removed post-rebase: main's FAR-612
    # refactored the claim status flip to the fenced ``transition_run`` helper,
    # which carries NO outputs/telemetry kwargs — the site no longer calls
    # ``update_run_status`` at all, so there is nothing to guard there.
}


def _update_run_status_call_sites() -> set[tuple[str, str, bool]]:
    """Every ``update_run_status(...)`` call site in backend/src with its
    enclosing function and whether it sits inside ``async with guard_dual_write``."""

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.guard_depth = 0
            self.findings: set[tuple[str, str, bool]] = set()

        def _scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._scoped(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._scoped(node)

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
            wrapped = (
                len(node.items) == 1
                and isinstance(node.items[0].context_expr, ast.Call)
                and isinstance(node.items[0].context_expr.func, ast.Name)
                and node.items[0].context_expr.func.id == "guard_dual_write"
            )
            if wrapped:
                self.guard_depth += 1
            self.generic_visit(node)
            if wrapped:
                self.guard_depth -= 1

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id == "update_run_status":
                enclosing = self.scope[-1] if self.scope else "<module>"
                self.findings.add((self._rel, enclosing, self.guard_depth > 0))
            self.generic_visit(node)

    findings: set[tuple[str, str, bool]] = set()
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):  # pragma: no cover
            continue
        visitor = Visitor()
        visitor._rel = rel  # type: ignore[attr-defined]
        visitor.visit(tree)
        findings |= visitor.findings
    return findings


class TestGuardContract:
    def test_every_blobs_passing_call_site_is_guard_wrapped(self) -> None:
        """qa M16: the NEXT blobs-passing ``update_run_status`` call site added
        without ``guard_dual_write`` fails here. The wrapped set is pinned to
        the sanctioned sites; the unwrapped set is pinned to the no-blobs
        sites; anything else is an unreviewed drift."""
        findings = _update_run_status_call_sites()
        wrapped = {(rel, fn) for rel, fn, guarded in findings if guarded}
        unwrapped = {(rel, fn) for rel, fn, guarded in findings if not guarded}
        missing_guard = wrapped - _GUARDED_SITES
        unguarded_drift = unwrapped - _UNGUARDED_SITES
        assert not missing_guard, (
            "update_run_status call sites wrapped in guard_dual_write that are NOT in the sanctioned "
            f"list (review + extend the allowlist): {sorted(missing_guard)}"
        )
        assert not unguarded_drift, (
            "UNGUARDED update_run_status call sites outside the known no-blobs set — if the new site "
            f"passes outputs_json/node_telemetry_json it MUST be wrapped in guard_dual_write: "
            f"{sorted(unguarded_drift)}"
        )
        assert wrapped >= _GUARDED_SITES, (
            f"a sanctioned guard-wrapped site disappeared: {sorted(_GUARDED_SITES - wrapped)}"
        )
        assert unwrapped >= _UNGUARDED_SITES, (
            f"a known no-blobs site became guarded (review the pin): {sorted(_UNGUARDED_SITES - unwrapped)}"
        )

    def test_update_run_status_docstring_documents_raises_and_guard(self) -> None:
        from modulo.db.crud.run import update_run_status

        doc = inspect.getdoc(update_run_status)
        assert doc is not None
        assert "DualWriteError" in doc, "the Raises contract must be documented at the chokepoint"
        assert "guard_dual_write" in doc, "the guard obligation must be documented at the chokepoint"

    def test_dual_write_helper_docstring_documents_the_contract(self) -> None:
        from modulo.db.crud.run import write_run_outputs_from_run

        doc = inspect.getdoc(write_run_outputs_from_run)
        assert doc is not None
        assert "DualWriteError" in doc
        # B1 contract cut: the helper is the PRIMARY store write — the
        # kill-switch machinery is GONE (removed at B2a) and the docstring
        # documents that (there is no legacy-only mode to fall back to).
        assert "PRIMARY" in doc
        assert "no emergency OFF" in doc


# ---------------------------------------------------------------------------
# qa M18 — the recovery failure path is claim-token fenced
# ---------------------------------------------------------------------------


class TestRecoveryClaimTokenFence:
    @pytest.mark.asyncio
    async def test_recovery_failure_carries_the_claim_token(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """qa M18: the recovery dual-write failure path passes
        ``run.claim_token`` — without it the separate-session terminalize
        could mark a SUCCESSOR's re-claim."""
        from modulo.core.pipeline_engine.recovery import _apply_recovery_markers

        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        captured: dict[str, Any] = {}

        async def _failing_dual_write(session: Any, **kwargs: Any) -> None:
            captured.update(kwargs)
            raise DualWriteError(
                "injected dual-write failure",
                run_id=kwargs["run_id"],
                organisation_id=kwargs["organisation_id"],
                claim_token=kwargs["claim_token"],
                origin=kwargs["origin"],
            )

        with patch("modulo.db.crud.run.write_run_outputs_from_run", _failing_dual_write):
            async with sqlite_sessionmaker() as session, session.begin():
                loaded = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
                with pytest.raises(DualWriteError) as caught:
                    await _apply_recovery_markers(session, loaded, "n1", {"recovered": True})

        assert captured["claim_token"] == "tok-a", "the loaded run's claim_token must fence the orchestration"
        assert caught.value.claim_token == "tok-a"
        assert caught.value.origin == "recovery.apply_recovery_markers"

    @pytest.mark.asyncio
    async def test_orchestration_uses_the_claim_token_for_terminalize(self) -> None:
        """End-to-end through the orchestrator: the DualWriteError's claim
        token reaches ``_mark_run_failed`` (the successor-reclaim fence)."""
        from modulo.core.error_tracking import saq_hooks
        from modulo.core.run_outputs_dualwrite import orchestrate_dual_write_failure

        exc = DualWriteError(
            "injected",
            run_id=uuid.uuid4(),
            organisation_id=_ORG,
            claim_token="tok-owner",
            sqlstate="42501",
            origin="recovery.apply_recovery_markers",
        )
        with (
            patch.object(saq_hooks, "_mark_run_failed", new_callable=AsyncMock, return_value=1) as mark,
            patch("modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event", new_callable=AsyncMock),
        ):
            await orchestrate_dual_write_failure(exc)
        assert mark.await_args.kwargs["claim_token"] == "tok-owner"


class TestRecoveryInheritedSentinelKeys:
    """qa iteration 2 (Major 4): the recovery chokepoint passes the
    PRE-mutation legacy dicts as *inherited_outputs* / *inherited_telemetry*
    (mirroring the ORM path) — pre-0176 inherited ``__``-prefixed keys are
    FILTERED from the new-table leg, never wedging the node's recovery."""

    @pytest.mark.asyncio
    async def test_inherited_kwargs_carry_the_pre_mutation_dicts(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        from modulo.core.pipeline_engine.recovery import _apply_recovery_markers

        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        captured: dict[str, Any] = {}

        async def _capturing_dual_write(session: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

        with patch("modulo.db.crud.run.write_run_outputs_from_run", _capturing_dual_write):
            async with sqlite_sessionmaker() as session, session.begin():
                loaded = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
                await _apply_recovery_markers(session, loaded, "n1", {"recovered": True})
        assert "__sneaky__" not in (captured["inherited_outputs"] or {}), "no pre-mutation capture, no filter"
        # (The seeded run carries no sentinel keys — the capture mirrors them.)
        assert captured["inherited_outputs"] is not None
        assert not captured["inherited_outputs"]
        assert captured["inherited_telemetry"] is not None
        assert not captured["inherited_telemetry"]
        # The post-mutation payloads still carry the recovery marker.
        assert captured["outputs"]["n1"] == {"recovered": True}

    @pytest.mark.asyncio
    async def test_recovery_on_an_inherited_sentinel_run_is_filtered_not_failed(
        self, sqlite_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """End-to-end through the REAL chokepoint: a run whose legacy blobs
        carry a pre-0176 ``__sneaky__`` key recovers successfully — the
        sentinel is filtered from the new-table leg (kept on the legacy
        column) and counted, instead of raising
        :class:`OutputsSentinelViolation` and wedging the recovery."""
        from modulo.core.pipeline_engine.recovery import _apply_recovery_markers

        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        # Pre-0176 inherited junk lands in the legacy blobs through the raw
        # Core legacy table (FAR-583 B1 — the ORM mapping is cut).
        from modulo.db.crud.run_node_outputs import read_legacy_run_blobs

        async with sqlite_sessionmaker() as seed_session, seed_session.begin():
            await seed_legacy_blobs(
                seed_session,
                run_id,
                outputs_json={"__sneaky__": {"v": 0}, "a": {"v": 1}},
                node_telemetry_json={"a": {"ms": 1}},
            )
        async with sqlite_sessionmaker() as session, session.begin():
            loaded = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            await set_rls_org(session, _ORG)
            # MUST NOT raise OutputsSentinelViolation — the M19 wedge this
            # fix removes from the recovery path.
            await _apply_recovery_markers(session, loaded, "n1", {"recovered": True})
        async with sqlite_sessionmaker() as check, check.begin():
            rows = (await check.execute(select(RunNodeOutput).where(RunNodeOutput.run_id == run_id))).scalars().all()
        final_nodes = {row.node_id for row in rows if row.attempt_key == "__final__"}
        assert "__sneaky__" not in final_nodes, "the sentinel is filtered from the new-table leg"
        assert "a" in final_nodes
        assert "n1" in final_nodes
        # The legacy column is NEVER written post-B1 (the store write is the
        # only store) — the inherited key survives there untouched until B2b's
        # repair migrates it into the new table.
        async with sqlite_sessionmaker() as check_legacy, check_legacy.begin():
            legacy = await read_legacy_run_blobs(check_legacy, run_id=run_id)
        assert legacy.outputs is not None
        assert "__sneaky__" in legacy.outputs
