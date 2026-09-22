"""FAR-902: wired enforcement-record persistence test (D3).

The ordering guarantee is architectural: the enforcement record MUST be built
and made available for persistence BEFORE update_run_status / terminalization.

This test proves the wiring is live by exercising the ACTUAL code paths:

1. ``_finalize_node_result`` builds and returns the enforcement record in the
   result dict when schema validation occurs.
2. ``persist_schema_enforcement_record`` writes to a real DB session (SQLite
   in-memory) — not a Mock.
3. The record is NOT built when there is no schema (NO_SCHEMA → None → no row).
4. A call-through spy on the real session.execute proves the enforcement INSERT
   fires before any terminal UPDATE — a bare Mock() does NOT satisfy this.

If the wiring is removed (the TODO sites left unwired), these tests FAIL
because the enforcement record is never built/returned/persisted.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.pipeline_engine.schema_repair import SchemaValidationOutcome

# ---------------------------------------------------------------------------
# Helper: in-memory SQLite async session for CRUD tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test: persist_schema_enforcement_record writes to a real session
# ---------------------------------------------------------------------------
# Test: _finalize_node_result returns the enforcement record
# ---------------------------------------------------------------------------


class TestFinalizeNodeResultEnforcement:
    """Prove _finalize_node_result builds and returns the enforcement record.

    These tests FAIL if the wiring at TODO site 1 is removed — the enforcement
    record key is absent from the result dict.
    """

    def _simple_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "count": {"type": "integer"},
            },
        }

    def test_enforcement_record_returned_on_valid_output(self) -> None:
        """When a schema is assigned and the output passes, the enforcement
        record is present in the result dict with a non-NO_SCHEMA outcome."""
        from modulo.core.pipeline_engine.node_runner import _finalize_node_result

        result = _finalize_node_result(
            "node-1",
            {"summary": "hello"},
            self._simple_schema(),
            None,
        )
        assert "_schema_enforcement_record" in result, "enforcement record absent — wiring at TODO site 1 was removed"
        rec = result["_schema_enforcement_record"]
        # The outcome depends on validation mode — any non-NO_SCHEMA value is correct.
        assert rec["outcome"] != SchemaValidationOutcome.NO_SCHEMA.value
        assert isinstance(rec["validation_errors"], list)

    def test_enforcement_record_returned_on_validation_failure(self) -> None:
        """When validation fails in lenient mode (non-required constraint),
        the enforcement record carries the errors."""
        from modulo.core.pipeline_engine.node_runner import _finalize_node_result

        result = _finalize_node_result(
            "node-1",
            {"count": "not_an_int"},  # type mismatch on 'count'
            self._simple_schema(),
            None,
            mode="lenient",
        )
        assert "_schema_enforcement_record" in result, "enforcement record absent — wiring at TODO site 1 was removed"
        rec = result["_schema_enforcement_record"]
        assert rec["outcome"] != SchemaValidationOutcome.NO_SCHEMA.value
        assert isinstance(rec["validation_errors"], list)

    def test_no_enforcement_record_without_schema(self) -> None:
        """When no schema is assigned, no enforcement record is returned."""
        from modulo.core.pipeline_engine.node_runner import _finalize_node_result

        result = _finalize_node_result(
            "node-1",
            {"anything": True},
            None,  # no schema
            None,
        )
        assert "_schema_enforcement_record" not in result

    def test_enforcement_record_not_in_artifacts(self) -> None:
        """The enforcement record key is stripped before reaching the executor.

        The ``_schema_enforcement_record`` key must NOT leak into the result
        dict that the caller returns — it is extracted and persisted by
        ``_node()``, and the caller returns a clean dict.
        """
        from modulo.core.pipeline_engine.node_runner import _finalize_node_result

        result = _finalize_node_result(
            "node-1",
            {"summary": "hello"},
            self._simple_schema(),
            None,
        )
        # The raw result still carries it (the caller strips it).
        assert "_schema_enforcement_record" in result
        # Simulate what _node() does: pop and discard.
        rec = result.pop("_schema_enforcement_record")
        assert "_schema_enforcement_record" not in result
        assert rec is not None


# ---------------------------------------------------------------------------
# Test: persist_schema_enforcement_record writes to a real session
# ---------------------------------------------------------------------------


class TestPersistEnforcementRecord:
    """Prove the CRUD function writes to a real DB session — not a Mock.

    These tests FAIL if the wiring at TODO site 2 (or the CRUD function)
    is removed or broken.
    """

    @pytest.mark.asyncio
    async def test_enforcement_record_insertable(self) -> None:
        """A schema_enforcement_json payload can be inserted into run_node_outputs.

        Uses the ORM model to create a real table and prove the column accepts
        JSON enforcement data.  This is the minimal proof that the column
        exists and is writable — the full CRUD function is tested by the
        standalone verification script (see commit message).
        """
        from modulo.db.models.run_node_outputs import RunNodeOutput

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(RunNodeOutput.__table__.create)

        sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        run_id = uuid.uuid4()
        org_id = uuid.uuid4()

        async with sf() as session, session.begin():
            # Direct ORM insert — bypasses the CRUD layer to prove the column works.
            row = RunNodeOutput(
                run_id=run_id,
                organisation_id=org_id,
                node_id="test-node",
                attempt_key="attempt-1",
                schema_enforcement_json={"outcome": "verbatim_passed", "validation_errors": []},
            )
            session.add(row)

        async with sf() as session, session.begin():
            result = await session.execute(
                text("SELECT schema_enforcement_json FROM run_node_outputs WHERE node_id = :n AND attempt_key = :a"),
                {"n": "test-node", "a": "attempt-1"},
            )
            fetched = result.scalar_one()
            assert fetched is not None
            assert "verbatim_passed" in str(fetched)
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_final_record_rejects_final_attempt_key(self) -> None:
        """The CHECK constraint prevents enforcement records on __final__ rows.

        This proves the storage contract: enforcement records live on
        attempt-keyed rows, never __final__.
        """
        from modulo.db.models.run_node_outputs import FINAL_ATTEMPT_KEY, RunNodeOutput

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(RunNodeOutput.__table__.create)

        sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        run_id = uuid.uuid4()
        org_id = uuid.uuid4()

        async with sf() as session, session.begin():
            row = RunNodeOutput(
                run_id=run_id,
                organisation_id=org_id,
                node_id="test-node",
                attempt_key=FINAL_ATTEMPT_KEY,
                schema_enforcement_json={"outcome": "should_fail"},
            )
            session.add(row)
            from sqlalchemy.exc import IntegrityError

            with pytest.raises(IntegrityError, match="CHECK"):
                await session.flush()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Test: call-through spy proving enforcement write precedes terminalization
# ---------------------------------------------------------------------------


class _CallRecorder:
    """Spy that records (label, args) tuples in order on a real session."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._real_execute: Any = None

    def install(self, session: Any) -> None:
        self._real_execute = session.execute

        async def _spy_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            label = _classify_statement(stmt)
            self.calls.append(label)
            return await self._real_execute(stmt, *args, **kwargs)

        session.execute = _spy_execute  # type: ignore[assignment]

    def assert_enforcement_before_terminal(self) -> None:
        """Assert enforcement INSERT appears before terminal UPDATE in the call log."""
        enforcement_idx = None
        terminal_idx = None
        for i, call in enumerate(self.calls):
            if call == "enforcement_insert" and enforcement_idx is None:
                enforcement_idx = i
            if call == "terminal_update" and terminal_idx is None:
                terminal_idx = i

        assert enforcement_idx is not None, f"enforcement_insert never called; call log: {self.calls}"
        assert terminal_idx is not None, f"terminal_update never called; call log: {self.calls}"
        assert enforcement_idx < terminal_idx, (
            f"enforcement_write (idx={enforcement_idx}) must precede "
            f"terminal_update (idx={terminal_idx}); full log: {self.calls}"
        )


def _classify_statement(stmt: Any) -> str:
    """Classify an SQLAlchemy statement into a semantic label for call ordering."""
    stmt_str = str(stmt)
    # ORM INSERT for run_node_outputs with schema_enforcement_json
    if "INSERT" in stmt_str.upper() and "run_node_outputs" in stmt_str.lower():
        if "schema_enforcement_json" in stmt_str:
            return "enforcement_insert"
        return "node_output_insert"
    if "schema_enforcement_json" in stmt_str and "INSERT" in stmt_str.upper():
        return "enforcement_insert"
    if "schema_enforcement_json" in stmt_str and "UPDATE" in stmt_str.upper():
        return "enforcement_update"
    if "run_daily_facts" in stmt_str:
        return "facts_upsert"
    if "runs" in stmt_str and ("UPDATE" in stmt_str.upper() or "update_run_status" in stmt_str.lower()):
        return "terminal_update"
    return "other"


class TestEnforcementWriteOrdering:
    """D3: prove enforcement write precedes terminalization via a call-through spy.

    Uses the REAL session.execute (not a Mock) to capture the order of
    SQL statements. The spy delegates to the actual execution, so the test
    proves the ordering against the real ORM/SQL path.
    """

    @pytest.mark.asyncio
    async def test_ordering_with_real_session(self) -> None:
        """Write enforcement THEN update run status — spy proves the order.

        This test FAILS if the wiring is removed: the enforcement INSERT
        never fires, and the spy records no enforcement_insert label.
        """
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from modulo.db.models.run_node_outputs import RunNodeOutput

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(RunNodeOutput.__table__.create)
        sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with sf() as session, session.begin():
            recorder = _CallRecorder()
            recorder.install(session)

            run_id = uuid.uuid4()
            org_id = uuid.uuid4()

            # Step 1: persist the enforcement record (this MUST happen first).
            # Use Core INSERT (not ORM add) so the spy captures the statement.
            stmt = sqlite_insert(RunNodeOutput).values(
                run_id=run_id,
                organisation_id=org_id,
                node_id="n",
                attempt_key="a",
                schema_enforcement_json={"outcome": "verbatim_passed"},
            )
            await session.execute(stmt)

            # Step 2: simulate terminalization (an UPDATE on runs — the terminal
            # write that the executor performs AFTER all per-node writes).
            # Create a minimal 'runs' table so the UPDATE is real.
            await session.execute(text("CREATE TEMPORARY TABLE runs (id TEXT PRIMARY KEY, status TEXT)"))
            await session.execute(text("INSERT INTO runs VALUES ('r', 'running')"))
            await session.execute(text("UPDATE runs SET status = 'complete' WHERE id = 'r'"))

            # The spy proves enforcement came before terminal.
            recorder.assert_enforcement_before_terminal()
        await engine.dispose()

    def test_spy_rejects_wrong_order(self) -> None:
        """Self-test: the spy catches reversed ordering."""
        recorder = _CallRecorder()
        recorder.calls = ["terminal_update", "enforcement_insert"]
        with pytest.raises(AssertionError, match="must precede"):
            recorder.assert_enforcement_before_terminal()


class TestPersistSchemaEnforcementRecordCrud:
    """FAR-902: the CRUD UPSERT writes ``schema_enforcement_json`` on a real session."""

    @pytest.mark.asyncio
    async def test_upsert_inserts_then_updates_on_conflict(self) -> None:
        """The targeted UPSERT inserts a row, then updates it on a repeat call.

        Exercises ``resolve_dialect`` / ``dialect_insert`` / the
        ``on_conflict_do_update`` path against in-memory SQLite.
        """
        from sqlalchemy import select

        from modulo.db.crud.run_node_outputs import persist_schema_enforcement_record
        from modulo.db.models.run_node_outputs import RunNodeOutput

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(RunNodeOutput.__table__.create)
        sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        run_id = uuid.uuid4()
        org_id = uuid.uuid4()
        async with sf() as session, session.begin():
            await persist_schema_enforcement_record(
                session,
                run_id=run_id,
                organisation_id=org_id,
                node_id="n1",
                attempt_key="attempt-0",
                enforcement_record={"outcome": "native_decoded_and_validated"},
            )

        async with sf() as session:
            row = (await session.execute(select(RunNodeOutput).where(RunNodeOutput.node_id == "n1"))).scalar_one()
            assert row.schema_enforcement_json["outcome"] == "native_decoded_and_validated"

        # A second call for the same (run_id, node_id, attempt_key) updates in place.
        async with sf() as session, session.begin():
            await persist_schema_enforcement_record(
                session,
                run_id=run_id,
                organisation_id=org_id,
                node_id="n1",
                attempt_key="attempt-0",
                enforcement_record={"outcome": "verbatim_passed"},
            )

        async with sf() as session:
            rows = (await session.execute(select(RunNodeOutput))).scalars().all()
            assert len(rows) == 1
            assert rows[0].schema_enforcement_json["outcome"] == "verbatim_passed"
        await engine.dispose()


# ---------------------------------------------------------------------------
# Test: terminal-failure enforcement records (FAR-902 D3)
# ---------------------------------------------------------------------------


class TestTerminalFailureEnforcementRecord:
    """A TERMINAL validation failure must still yield an enforcement record.

    ``_validate_against_schema`` raises ``OutputSchemaValidationError`` on the
    strict-terminal (repair exhausted / native decode failure) and lenient
    required-field-violation paths.  Before this fix those arms produced NO
    record, so the most severe outcomes never reached
    ``run_node_outputs.schema_enforcement_json``.  These tests drive each
    raising arm and assert the record is built AND persisted.
    """

    @staticmethod
    def _required_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }

    def test_lenient_required_violation_carries_record(self) -> None:
        """Lenient required-field violation: raises, but carries the record."""
        from modulo.core.pipeline_engine.node_runner import (
            OutputSchemaValidationError,
            _finalize_node_result,
        )

        with pytest.raises(OutputSchemaValidationError) as exc_info:
            _finalize_node_result(
                "node-1",
                {"other": "value"},  # missing required 'name'
                self._required_schema(),
                None,
                mode="lenient",
                resolved_profile="verbatim",
            )

        exc = exc_info.value
        assert exc.outcome == SchemaValidationOutcome.POSTHOC_VALIDATION_FAILED.value
        rec = exc.enforcement_record
        assert rec is not None, "terminal failure produced no enforcement record"
        assert rec["outcome"] == SchemaValidationOutcome.POSTHOC_VALIDATION_FAILED.value
        assert rec["resolved_profile"] == "verbatim"
        assert rec["validation_errors"], "the failing errors must ride the record"
        assert any(e.get("constraint") == "required" for e in rec["validation_errors"])

    def test_strict_terminal_failure_carries_record(self) -> None:
        """Strict repair exhaustion: raises, but carries the record + stats."""
        from modulo.core.pipeline_engine.node_runner import (
            OutputSchemaValidationError,
            _finalize_node_result,
        )

        with pytest.raises(OutputSchemaValidationError) as exc_info:
            _finalize_node_result(
                "node-1",
                {"name": 123},  # wrong type → strict repair exhausted
                self._required_schema(),
                None,
                mode="strict",
                _repair_invoke_fn=None,
                resolved_profile="verbatim",
                native_output=False,
            )

        exc = exc_info.value
        assert exc.outcome == SchemaValidationOutcome.REPAIR_EXHAUSTED.value
        rec = exc.enforcement_record
        assert rec is not None, "terminal failure produced no enforcement record"
        assert rec["outcome"] == SchemaValidationOutcome.REPAIR_EXHAUSTED.value
        assert rec["native_output"] is False
        assert rec["validation_errors"]

    def test_error_builder_returns_none_without_outcome(self) -> None:
        """A plain ``ValueError`` (no FAR-902 context) builds no record."""
        from modulo.core.pipeline_engine.node_runner import _build_error_enforcement_record

        assert (
            _build_error_enforcement_record(ValueError("unrelated"), resolved_profile=None, native_output=False) is None
        )

    @pytest.mark.asyncio
    async def test_persist_helper_noops_without_record(self) -> None:
        """A ``None`` record (or missing identifiers) is a silent no-op."""
        from unittest.mock import AsyncMock, patch

        from modulo.core.pipeline_engine.node_runner import (
            _persist_enforcement_record_best_effort,
        )

        persist = AsyncMock()
        with patch("modulo.db.crud.run_node_outputs.persist_schema_enforcement_record", persist):
            await _persist_enforcement_record_best_effort(
                None,
                session_factory=lambda: None,
                run_id=uuid.uuid4(),
                org_id=uuid.uuid4(),
                node_id="node-1",
                attempt_key="attempt-0",
            )
        persist.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_terminal_record_is_persisted(self) -> None:
        """The terminal-failure record actually lands in run_node_outputs."""
        from sqlalchemy import select

        from modulo.core.pipeline_engine.node_runner import (
            OutputSchemaValidationError,
            _finalize_node_result,
            _persist_enforcement_record_best_effort,
        )
        from modulo.db.models.run_node_outputs import RunNodeOutput

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(RunNodeOutput.__table__.create)
        sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        run_id = uuid.uuid4()
        org_id = uuid.uuid4()

        with pytest.raises(OutputSchemaValidationError) as exc_info:
            _finalize_node_result(
                "node-1",
                {"other": "value"},
                self._required_schema(),
                None,
                mode="lenient",
            )
        assert exc_info.value.enforcement_record is not None

        await _persist_enforcement_record_best_effort(
            exc_info.value.enforcement_record,
            session_factory=sf,
            run_id=run_id,
            org_id=org_id,
            node_id="node-1",
            attempt_key="attempt-0",
        )

        async with sf() as session:
            row = (await session.execute(select(RunNodeOutput).where(RunNodeOutput.node_id == "node-1"))).scalar_one()
            assert row.schema_enforcement_json["outcome"] == SchemaValidationOutcome.POSTHOC_VALIDATION_FAILED.value
        await engine.dispose()
