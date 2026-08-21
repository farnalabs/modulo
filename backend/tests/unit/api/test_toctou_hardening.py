"""Tests for TOCTOU hardening (#1376, #1105).

Covers:
- RateLimitConflictError domain exception
- ErrorNotificationRule model has deleted_at column and partial unique index
- Migration 0117 file exists and is well-formed
- The per-signal unique index allows seeding multiple default rules per org (#1376)
- The rate-limit conflict path in ``create_run`` maps a unique violation to a
  RateLimitConflictError without leaving the outer transaction aborted (#1105)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Table, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from modulo.core.error_tracking import DEFAULT_ALERT_RULES, seed_default_alert_rules_for_org
from modulo.core.exceptions import RateLimitConflictError
from modulo.db.crud.run import _is_unique_violation, create_run
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.error_notification_rule import DeletedDefault, ErrorNotificationRule
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.team import Team

_MIGRATION_FILE = (
    Path(__file__).parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / "0117_toctou_hardening.py"
)

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")


def _enr_index_columns() -> set[str]:
    from modulo.db.models.error_notification_rule import ErrorNotificationRule

    for idx in ErrorNotificationRule.__table__.indexes:
        if idx.name == "uq_enr_org_active":
            return {c.name for c in idx.columns}
    return set()


# ---------------------------------------------------------------------------
# RateLimitConflictError
# ---------------------------------------------------------------------------


class TestRateLimitConflictError:
    def test_stores_attributes(self) -> None:
        pid = uuid.uuid4()
        exc = RateLimitConflictError(pipeline_id=pid, rate_limit_key="key:abc")
        assert exc.pipeline_id == pid
        assert exc.rate_limit_key == "key:abc"
        assert "key:abc" in str(exc)

    def test_defaults_are_none(self) -> None:
        exc = RateLimitConflictError()
        assert exc.pipeline_id is None
        assert exc.rate_limit_key is None


# ---------------------------------------------------------------------------
# ErrorNotificationRule — deleted_at column + index design (#1376)
# ---------------------------------------------------------------------------


class TestErrorNotificationRuleDeletedAt:
    def test_model_has_deleted_at_column(self) -> None:
        from modulo.db.models.error_notification_rule import ErrorNotificationRule

        col = ErrorNotificationRule.__table__.c.deleted_at
        assert col.nullable is True

    def test_partial_unique_index_exists(self) -> None:
        from modulo.db.models.error_notification_rule import ErrorNotificationRule

        index_names = [idx.name for idx in ErrorNotificationRule.__table__.indexes]
        assert "uq_enr_org_active" in index_names

    def test_unique_index_keys_on_org_and_signal(self) -> None:
        """The cap/index must key on the unit that must be unique.

        A unique index on ``(organisation_id)`` alone would enforce at most ONE
        active rule per org, contradicting the per-org cap of 10 and the
        default-rule seed (4 rules with distinct signals, all active). Keying on
        ``(organisation_id, signal)`` allows up to ``_MAX_RULES_PER_ORG``
        distinct active rules while preventing duplicate default seeds.
        """
        assert _enr_index_columns() == {"organisation_id", "signal"}


class TestDefaultRuleSeedInvariant:
    def test_default_rule_signals_are_distinct(self) -> None:
        """The seed inserts one default rule per signal; distinct signals mean the
        per-(org, signal) unique index never blocks seeding all of them."""
        signals = [spec["signal"] for spec in DEFAULT_ALERT_RULES]
        assert len(signals) == len(set(signals))
        assert len(signals) >= 2


# ---------------------------------------------------------------------------
# Migration file exists and chains correctly
# ---------------------------------------------------------------------------


class TestMigration0117:
    def test_migration_file_exists(self) -> None:
        assert _MIGRATION_FILE.exists(), f"Migration not found at {_MIGRATION_FILE}"

    def test_migration_chain(self) -> None:
        content = _MIGRATION_FILE.read_text()
        assert 'revision: str = "0117_toctou_hardening"' in content
        assert 'down_revision: str | None = "0116_guardrail_trust_pr_b"' in content

    def test_migration_has_both_indexes(self) -> None:
        content = _MIGRATION_FILE.read_text()
        assert "uq_enr_org_active" in content
        assert "uq_runs_pipeline_rate_limit_key" in content
        assert "deleted_at IS NULL" in content
        assert "rate_limit_key IS NOT NULL" in content

    def test_migration_unique_index_keys_on_org_and_signal(self) -> None:
        """Migration 0117 must not build the org-only unique index that would
        break both the 10-rule cap and the default-rule seed."""
        content = _MIGRATION_FILE.read_text()
        assert "ON error_notification_rules (organisation_id, signal)" in content
        assert "ON error_notification_rules (organisation_id) " not in content


# ---------------------------------------------------------------------------
# Run model — rate-limit index is the source of truth (migration 0117 / #1105)
# ---------------------------------------------------------------------------


class TestRunRateLimitIndex:
    def test_model_declares_partial_unique_index(self) -> None:
        """The Run model must declare the partial unique index that migration
        0117 creates, so create_all-based environments (test fixtures, dev
        bootstrap) get the DB-level rate-limit backstop instead of silently
        drifting from the migration."""
        indexes = {idx.name: idx for idx in Run.__table__.indexes}
        assert "uq_runs_pipeline_rate_limit_key" in indexes
        idx = indexes["uq_runs_pipeline_rate_limit_key"]
        assert idx.unique is True
        assert {c.name for c in idx.columns} == {"pipeline_id", "rate_limit_key"}

    def test_model_does_not_regenerate_dropped_index(self) -> None:
        """Migration 0117 drops ix_runs_rate_limit_key; the model must not
        declare index=True on rate_limit_key (which would recreate it)."""
        assert "ix_runs_rate_limit_key" not in {idx.name for idx in Run.__table__.indexes}
        col = Run.__table__.c.rate_limit_key
        assert col.index is None


# ---------------------------------------------------------------------------
# _is_unique_violation (#1105)
# ---------------------------------------------------------------------------


class TestIsUniqueViolation:
    def test_postgres_unique_returns_true(self) -> None:
        orig = type("D", (), {"pgcode": "23505"})()
        exc = IntegrityError("stmt", {}, orig)
        assert _is_unique_violation(exc) is True

    def test_postgres_other_returns_false(self) -> None:
        orig = type("D", (), {"pgcode": "23502"})()
        exc = IntegrityError("stmt", {}, orig)
        assert _is_unique_violation(exc) is False

    def test_sqlite_unique_returns_true(self) -> None:
        orig = Exception("UNIQUE constraint failed: runs.rate_limit_key")
        exc = IntegrityError("stmt", {}, orig)
        assert _is_unique_violation(exc) is True

    def test_sqlite_non_unique_returns_false(self) -> None:
        orig = Exception("NOT NULL constraint failed: runs.status")
        exc = IntegrityError("stmt", {}, orig)
        assert _is_unique_violation(exc) is False

    def test_no_orig_returns_false(self) -> None:
        exc = IntegrityError("stmt", {}, None)
        assert _is_unique_violation(exc) is False


# ---------------------------------------------------------------------------
# DB-backed: seeding multiple default rules per org works (#1376)
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_engine() -> AsyncEngine:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    tables: list[Table] = cast(
        list[Table],
        [
            Organisation.__table__,
            Pipeline.__table__,
            Account.__table__,
            Team.__table__,
            Run.__table__,
            PipelineSnapshot.__table__,
            Journey.__table__,
            EvalDefinition.__table__,
            EvalResult.__table__,
            AuditEvent.__table__,
            AuditChainHead.__table__,
            EnvironmentProfile.__table__,
            ErrorNotificationRule.__table__,
            DeletedDefault.__table__,
        ],
    )
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def sqlite_session(sqlite_engine: AsyncEngine) -> AsyncSession:
    maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with maker() as s:
        yield s


class TestSeedingMultipleDefaultRules:
    async def test_seed_all_default_rules_for_same_org(self, sqlite_session: AsyncSession) -> None:
        """Seeding 4 default rules (agent.failed, agent.no_op, agent.stall,
        contract.schema) for ONE org must succeed. An org-only unique index would
        violate on the 2nd insert with ``deleted_at IS NULL``."""
        sqlite_session.add(Organisation(id=_ORG, name="org", slug="org"))
        await sqlite_session.commit()

        async with sqlite_session.begin():
            seeded = await seed_default_alert_rules_for_org(sqlite_session, _ORG)

        assert seeded == len(DEFAULT_ALERT_RULES)
        await sqlite_session.commit()

        rows = (
            await sqlite_session.execute(
                text("SELECT signal, deleted_at FROM error_notification_rules ORDER BY signal")
            )
        ).fetchall()
        signals = {r[0] for r in rows}
        assert signals == {spec["signal"] for spec in DEFAULT_ALERT_RULES}
        assert all(r[1] is None for r in rows)

    async def test_seed_is_idempotent_across_calls(self, sqlite_session: AsyncSession) -> None:
        """Re-running the seed with the per-signal index in place must not throw
        (matches the idempotent upsert contract of the seed)."""
        sqlite_session.add(Organisation(id=_ORG, name="org", slug="org"))
        await sqlite_session.commit()

        async with sqlite_session.begin():
            first = await seed_default_alert_rules_for_org(sqlite_session, _ORG)
        await sqlite_session.commit()

        async with sqlite_session.begin():
            second = await seed_default_alert_rules_for_org(sqlite_session, _ORG)
        await sqlite_session.commit()

        assert first == len(DEFAULT_ALERT_RULES)
        assert second == len(DEFAULT_ALERT_RULES)

    async def test_duplicate_signal_is_rejected(self, sqlite_engine: AsyncEngine) -> None:
        """A second ACTIVE rule with the same (org, signal) must violate —
        proving the index enforces per-signal uniqueness, not single-rule-per-org."""
        maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)
        async with maker() as s:
            s.add(Organisation(id=_ORG, name="org", slug="org"))
            await s.commit()

        # Same (org, signal) twice → unique violation.
        async with maker() as s:
            s.add(
                ErrorNotificationRule(
                    organisation_id=_ORG,
                    name="a",
                    condition_level="error",
                    signal="agent.failed",
                    action_type="in_app",
                )
            )
            s.add(
                ErrorNotificationRule(
                    organisation_id=_ORG,
                    name="b",
                    condition_level="error",
                    signal="agent.failed",
                    action_type="in_app",
                )
            )
            with pytest.raises(IntegrityError):
                await s.flush()

        # Different signals for the SAME org still coexist.
        async with maker() as s:
            s.add(
                ErrorNotificationRule(
                    organisation_id=_ORG,
                    name="a",
                    condition_level="error",
                    signal="agent.failed",
                    action_type="in_app",
                )
            )
            s.add(
                ErrorNotificationRule(
                    organisation_id=_ORG,
                    name="b",
                    condition_level="error",
                    signal="agent.stall",
                    action_type="in_app",
                )
            )
            await s.flush()
            await s.commit()


# ---------------------------------------------------------------------------
# DB-backed: rate-limit conflict maps to RateLimitConflictError (#1105)
# ---------------------------------------------------------------------------


async def _seed_run_base(session: AsyncSession) -> None:
    session.add(Organisation(id=_ORG, name="org", slug="org"))
    session.add(Account(id=_ACCOUNT, email="admin@example.com", display_name="admin"))
    session.add(Pipeline(id=_PIPELINE, organisation_id=_ORG, name="pipeline", account_id=_ACCOUNT, visibility="org"))
    await session.commit()


async def _assert_unique_rate_limit_index(sqlite_engine: AsyncEngine) -> None:
    """Assert the per-pipeline rate-limit backstop exists in the live DB.

    The Run model now declares ``uq_runs_pipeline_rate_limit_key`` in
    ``__table_args__`` (migration 0117), so ``create_all`` — which the
    sqlite_engine fixture runs from the ORM metadata — creates it automatically.
    Previously the test had to manufacture the index by hand because the model
    didn't declare it (the model/migration drift this PR fixes); that manual DDL
    would now collide with the model-declared index.
    """
    async with sqlite_engine.begin() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_runs_pipeline_rate_limit_key'"
        )
        assert rows.fetchall(), "uq_runs_pipeline_rate_limit_key missing from DB — model must declare it"


class TestRateLimitConflictPath:
    async def test_conflict_raises_rate_limit_conflict_and_not_transaction_error(
        self, sqlite_engine: AsyncEngine, sqlite_session: AsyncSession
    ) -> None:
        """A second run with the same (pipeline, rate_limit_key) must surface as a
        RateLimitConflictError. Before the savepoint fix, the failed flush aborted
        the outer transaction and the caller's subsequent flush raised
        PendingRollbackError — a 503, not the intended 429."""
        await _assert_unique_rate_limit_index(sqlite_engine)
        await _seed_run_base(sqlite_session)
        maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)

        # First run claims the rate-limit key.
        async with maker() as s:
            run1 = await create_run(
                s,
                org_id=_ORG,
                pipeline_id=_PIPELINE,
                snapshot_id=_SNAPSHOT,
                trigger_type="manual",
                input_payload={},
                rate_limit_key="rl:key",
            )
            assert run1.rate_limit_key == "rl:key"
            await s.commit()

        # Second run with the same key conflicts.
        async with maker() as s:
            with pytest.raises(RateLimitConflictError) as excinfo:
                await create_run(
                    s,
                    org_id=_ORG,
                    pipeline_id=_PIPELINE,
                    snapshot_id=_SNAPSHOT,
                    trigger_type="manual",
                    input_payload={},
                    rate_limit_key="rl:key",
                )
            assert excinfo.value.pipeline_id == _PIPELINE
            assert excinfo.value.rate_limit_key == "rl:key"
            # The outer transaction must still be usable (savepoint rolled back
            # only the nested insert, not the whole transaction).
            await s.commit()

    async def test_distinct_keys_do_not_conflict(
        self, sqlite_engine: AsyncEngine, sqlite_session: AsyncSession
    ) -> None:
        await _assert_unique_rate_limit_index(sqlite_engine)
        await _seed_run_base(sqlite_session)
        maker = async_sessionmaker(sqlite_engine, expire_on_commit=False)

        async with maker() as s:
            r1 = await create_run(
                s,
                org_id=_ORG,
                pipeline_id=_PIPELINE,
                snapshot_id=_SNAPSHOT,
                trigger_type="manual",
                input_payload={},
                rate_limit_key="rl:a",
            )
            await s.flush()
            r2 = await create_run(
                s,
                org_id=_ORG,
                pipeline_id=_PIPELINE,
                snapshot_id=_SNAPSHOT,
                trigger_type="manual",
                input_payload={},
                rate_limit_key="rl:b",
            )
            assert r1.rate_limit_key == "rl:a"
            assert r2.rate_limit_key == "rl:b"
