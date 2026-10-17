"""Vocabulary/constraint tests for the ``auto_deactivated`` widening.

The migration chain was squashed into three idempotent reconciliation
migrations (``0108_schema_org_identity`` / ``0109_schema_teams_library`` /
``0110_schema_pipeline_runtime``). The per-feature migrations that used to carry
this surface (``0104_trigger_event_auto_deactivated`` head ``0105``, plus main's
``0106`` adding ``guardrail_blocked``) no longer exist;
``0110_schema_pipeline_runtime`` now owns the ``ck_trigger_events_validation_result``
constraint with the FULL 21-value vocabulary. This file asserts:

* the model vocabulary (``VALIDATION_RESULT_VALUES``) contains
  ``auto_deactivated`` and the ORM CHECK constraint reflects it,
* the widening migration's hardcoded vocabulary stays in sync with the
  model (the single source of truth) — a value added to one side and not the
  other breaks the constraint/model contract,
* the chain: this migration's own parent is pinned by ``_MIGRATION_NAME`` /
  ``_MIGRATION_PATH`` below; the graph's single linear head, and its
  completeness, are asserted by
  ``_migration_graph.assert_single_linear_head``.

The old SQLite round-trip (which ran the migration's upgrade/downgrade against
a mock ``op``) is obsolete: the reconciliation migration expresses the
constraint as guarded raw DDL (``ADD CONSTRAINT ... IF NOT EXISTS`` with the
full vocabulary) rather than a reversible drop/add pair, and its downgrade is a
no-op. The drift-guard tests below are the meaningful contract.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.db.models.base import Base
from modulo.db.models.trigger_event import VALIDATION_RESULT_VALUES, TriggerEvent
from tests.unit.db._migration_graph import assert_single_linear_head, script_directory

_MIGRATION_NAME = "0319_trigger_event_parse_failed"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)

# (_MIGRATION_NAME above is pinned to the migration that owns the widened
# constraint under test — the Slack parse-failure fix moved that ownership to
# 0319_trigger_event_parse_failed.)
_CHECK_CONSTRAINT_NAME = "ck_trigger_events_validation_result"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestModelVocabulary:
    def test_auto_deactivated_in_model_vocabulary(self) -> None:
        assert "auto_deactivated" in VALIDATION_RESULT_VALUES

    def test_guardrail_blocked_in_model_vocabulary(self) -> None:
        # Folded in from main's 0106 (guardrail_blocked), now part of 0008.
        assert "guardrail_blocked" in VALIDATION_RESULT_VALUES

    def test_far604_values_in_model_vocabulary(self) -> None:
        # FAR-604 admission healing: latest-wins coalescing + backpressure skip.
        assert "coalesced" in VALIDATION_RESULT_VALUES
        assert "backpressure_skipped" in VALIDATION_RESULT_VALUES

    def test_far1144_value_in_model_vocabulary(self) -> None:
        # FAR-1144: distinct label for the value-filter gate (event_filters).
        assert "event_value_filter_not_accepted" in VALIDATION_RESULT_VALUES

    def test_far1916_value_in_model_vocabulary(self) -> None:
        # FAR-1916: a work_item_refs_required pipeline refused a delivery with
        # no refs. The engine wrote this value but it was missing from the
        # vocabulary, so the refusal INSERT violated the CHECK and no event
        # ever persisted (the 422 was a 503).
        assert "required_refs_missing" in VALIDATION_RESULT_VALUES

    def test_parse_failed_value_in_model_vocabulary(self) -> None:
        # The Slack app_mention engine writes ``parse_failed`` when a payload
        # cannot be parsed. It was missing from the vocabulary, so the audit
        # INSERT violated the CHECK and no parse-failure event ever persisted.
        assert "parse_failed" in VALIDATION_RESULT_VALUES

    def test_model_vocabulary_is_26_values(self) -> None:
        assert len(VALIDATION_RESULT_VALUES) == 26
        assert len(set(VALIDATION_RESULT_VALUES)) == len(VALIDATION_RESULT_VALUES)

    def test_orm_check_constraint_includes_auto_deactivated(self) -> None:
        from sqlalchemy import CheckConstraint

        from modulo.db.models.trigger_event import TriggerEvent

        checks = [c for c in TriggerEvent.__table_args__ if isinstance(c, CheckConstraint)]
        check = next(c for c in checks if c.name == _CHECK_CONSTRAINT_NAME)
        assert "auto_deactivated" in check.sqltext.text


class TestReconciliationMigration:
    def test_single_linear_head(self) -> None:
        assert_single_linear_head(script_directory())

    def test_0008_owns_trigger_events_validation_constraint(self) -> None:
        """The widening migration must create the constraint with the
        FULL model vocabulary — a value in the model but missing from the
        migration breaks the constraint on a fresh DB, and a value in the
        migration but not the model widens the constraint beyond the ORM."""
        _load_migration()
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        assert _CHECK_CONSTRAINT_NAME in source
        for value in VALIDATION_RESULT_VALUES:
            assert f"'{value}'" in source, f"widening constraint DDL missing {value!r}"

    def test_0008_constraint_guards_idempotency(self) -> None:
        """The constraint is added only when absent (pg_constraint guard), so
        re-running the widening migration is a no-op."""
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        assert f"conname='{_CHECK_CONSTRAINT_NAME}'" in source
        assert f"ADD CONSTRAINT {_CHECK_CONSTRAINT_NAME} CHECK" in source

    def test_0008_add_is_not_valid_then_validated(self) -> None:
        """FAR-604 F5 — the widened CHECK is added NOT VALID (instant, no
        full-table validation scan under ACCESS EXCLUSIVE on the hottest
        insert table) and then validated in a separate guarded step (VALIDATE
        CONSTRAINT takes SHARE UPDATE EXCLUSIVE — non-blocking for INSERTs).
        The validation step is guarded on ``NOT convalidated`` so re-running
        the reconciliation chain skips an already-validated constraint."""
        source = Path(_MIGRATION_PATH).read_text(encoding="utf-8")
        assert f"ADD CONSTRAINT {_CHECK_CONSTRAINT_NAME} CHECK" in source
        assert "NOT VALID" in source
        assert f"VALIDATE CONSTRAINT {_CHECK_CONSTRAINT_NAME}" in source
        assert "NOT convalidated" in source

    def test_0008_drop_guard_expected_def_is_whitespace_stripped(self) -> None:
        """FAR-604 F5 — the drop guard compares the live definition against a
        WHITESPACE-STRIPPED expected literal (it is compared against
        ``regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g')``). A
        literal carrying the ``character varying`` space form never matches,
        so the drop — and its ACCESS EXCLUSIVE lock — would fire on EVERY
        re-run of the reconciliation chain (the 0110 guard pattern)."""
        module = _load_migration()
        drop_stmt: str = module._DROP_IF_DIFFERENT
        assert "regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g')" in drop_stmt
        assert "::charactervarying" in drop_stmt
        assert "character varying" not in drop_stmt


class TestRequiredRefsMissingRoundTrip:
    """FAR-1916: the new ``required_refs_missing`` value must be INSERTABLE.

    The whole point of widening the vocabulary is that a write site can now
    persist the value; before the change the CHECK — a HARD DB gate — rejected
    it (``IntegrityError``), so the refusal event could never survive. This is a
    REAL round-trip against an in-memory SQLite database (the ORM CHECK
    constraint is enforced by the engine, so a missing vocabulary entry fails
    the INSERT, not a mock assertion)."""

    async def test_required_refs_missing_event_persists(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[TriggerEvent.__table__]))
                await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
            maker = async_sessionmaker(engine, expire_on_commit=False)
            org_id = uuid.uuid4()
            trigger_id = uuid.uuid4()
            async with maker() as session, session.begin():
                session.add(
                    TriggerEvent(
                        organisation_id=org_id,
                        trigger_id=trigger_id,
                        trigger_type="webhook",
                        raw_payload_hash="a" * 64,
                        validation_result="required_refs_missing",
                        error_detail="Required work-item refs missing",
                    )
                )
            # The row exists once the request/transaction returned — read it back
            # through the same engine rather than trusting the write.
            from sqlalchemy import select

            async with maker() as session:
                stored = (
                    await session.execute(select(TriggerEvent).where(TriggerEvent.trigger_id == trigger_id))
                ).scalar_one()
                assert stored.validation_result == "required_refs_missing"
                assert stored.organisation_id == org_id
        finally:
            await engine.dispose()


class TestParseFailedRoundTrip:
    """The Slack ``parse_failed`` value must be INSERTABLE.

    ``slack_app_mention._parse_mention`` audits a malformed ``app_mention``
    payload with ``validation_result="parse_failed"``. Before the vocabulary
    was widened the CHECK — a HARD DB gate — rejected it (``IntegrityError``),
    so the audit row could never survive. This is a REAL round-trip against an
    in-memory SQLite database: the ORM CHECK constraint is enforced by the
    engine, so a missing vocabulary entry fails the INSERT rather than a mock
    assertion."""

    async def test_parse_failed_event_persists(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[TriggerEvent.__table__]))
                await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
            maker = async_sessionmaker(engine, expire_on_commit=False)
            org_id = uuid.uuid4()
            trigger_id = uuid.uuid4()
            async with maker() as session, session.begin():
                session.add(
                    TriggerEvent(
                        organisation_id=org_id,
                        trigger_id=trigger_id,
                        trigger_type="slack",
                        raw_payload_hash="b" * 64,
                        validation_result="parse_failed",
                        error_detail="app_mention payload could not be parsed",
                    )
                )
            # Read the row back through the same engine rather than trusting
            # the write.
            from sqlalchemy import select

            async with maker() as session:
                stored = (
                    await session.execute(select(TriggerEvent).where(TriggerEvent.trigger_id == trigger_id))
                ).scalar_one()
                assert stored.validation_result == "parse_failed"
                assert stored.organisation_id == org_id
        finally:
            await engine.dispose()
