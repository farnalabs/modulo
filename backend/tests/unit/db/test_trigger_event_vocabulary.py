"""Vocabulary/constraint tests for the FAR-192 ``auto_deactivated`` widening.

Covers the ``0104_trigger_event_auto_deactivated`` migration and its contract
with the ORM model:

* the model vocabulary (``VALIDATION_RESULT_VALUES``) contains
  ``auto_deactivated`` and the ORM CHECK constraint reflects it,
* the migration's hardcoded vocabulary stays in sync with the model (the
  single source of truth),
* the migration sits on ``0103_lifecycle_map_version_actor`` and is revised by
  the FAR-219 head ``0105_guardrail_pins`` (the sole head),
* the migration's Postgres DDL widens the constraint to the FULL 20-value set
  (NOT VALID + VALIDATE, 0069-pattern) and the downgrade restores the 19-value
  set,
* a real SQLite round-trip proves the actual constraint behaviour: before
  upgrade the old 19-value constraint REJECTS ``auto_deactivated``, after
  upgrade it ACCEPTS it, after downgrade it REJECTS it again,
* the orphan-row guard fails loudly in both directions when a row outside the
  target vocabulary exists.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from modulo.db.models.trigger_event import VALIDATION_RESULT_VALUES

_MIGRATION_NAME = "0104_trigger_event_auto_deactivated"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)

# The FAR-219 migration that revises 0104 and is the current head of the chain.
_HEAD_MIGRATION_NAME = "0105_guardrail_pins"

# The 19-value vocabulary BEFORE this migration (what 0069 created).
_OLD_VALIDATION_RESULT_VALUES = tuple(v for v in VALIDATION_RESULT_VALUES if v != "auto_deactivated")

_CHECK_CONSTRAINT_NAME = "ck_trigger_events_validation_result"


def _vocab_sql(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(_MIGRATION_PATH.parent.parent))


def _run_on_mock_postgres(migration: ModuleType, func_name: str, orphan_values: list[str] | None = None) -> MagicMock:
    """Execute ``upgrade()``/``downgrade()`` against a mocked Postgres ``op``.

    ``op.get_bind()`` returns a mock connection whose ``execute(...).scalars().all()``
    yields *orphan_values* (empty by default) so the migration's defensive
    orphan check sees a clean table unless the test says otherwise.
    """
    mock_bind = MagicMock()
    mock_bind.dialect.name = "postgresql"
    mock_op = MagicMock()
    mock_op.get_bind.return_value = mock_bind
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = orphan_values if orphan_values is not None else []
    mock_bind.execute.return_value = mock_result
    with patch.object(migration, "op", mock_op):
        getattr(migration, func_name)()
    return mock_op


def _executed_sql(mock_op: MagicMock) -> list[str]:
    return [call.args[0] for call in mock_op.execute.call_args_list]


class TestModelVocabulary:
    def test_auto_deactivated_in_model_vocabulary(self) -> None:
        assert "auto_deactivated" in VALIDATION_RESULT_VALUES

    def test_model_vocabulary_is_20_values(self) -> None:
        assert len(VALIDATION_RESULT_VALUES) == 20
        assert len(set(VALIDATION_RESULT_VALUES)) == len(VALIDATION_RESULT_VALUES)

    def test_orm_check_constraint_includes_auto_deactivated(self) -> None:
        from modulo.db.models.trigger_event import TriggerEvent

        checks = [c for c in TriggerEvent.__table_args__ if isinstance(c, CheckConstraint)]
        check = next(c for c in checks if c.name == _CHECK_CONSTRAINT_NAME)
        assert "auto_deactivated" in check.sqltext.text


class TestMigrationChain:
    def test_migration_is_revised_by_head_0105(self) -> None:
        migration = _load_migration()
        assert migration.revision == _MIGRATION_NAME
        assert migration.down_revision == "0103_lifecycle_map_version_actor"
        assert migration.branch_labels is None
        script = _script()
        heads = script.get_heads()
        assert heads == [_HEAD_MIGRATION_NAME], f"expected a single head, got {heads}"
        revisions = {rev.revision for rev in script.walk_revisions()}
        assert migration.revision in revisions
        assert _HEAD_MIGRATION_NAME in revisions
        head = script.get_revision(_HEAD_MIGRATION_NAME)
        assert head is not None
        assert head.down_revision == _MIGRATION_NAME

    def test_migration_vocabulary_matches_model(self) -> None:
        """The migration's hardcoded vocabulary must equal the ORM single source
        of truth — a value added to one side and not the other breaks either the
        migration (constraint rejects a value the model allows) or the model
        (the ORM CHECK rejects a value the migration accepts)."""
        migration = _load_migration()
        assert tuple(migration._VALIDATION_RESULT_VALUES) == VALIDATION_RESULT_VALUES, (
            "migration _VALIDATION_RESULT_VALUES must equal the model vocabulary "
            f"(missing: {sorted(set(VALIDATION_RESULT_VALUES) - set(migration._VALIDATION_RESULT_VALUES))}, "
            f"extra: {sorted(set(migration._VALIDATION_RESULT_VALUES) - set(VALIDATION_RESULT_VALUES))})"
        )

    def test_migration_old_vocabulary_is_19_values(self) -> None:
        migration = _load_migration()
        old = tuple(migration._OLD_VALIDATION_RESULT_VALUES)
        assert len(old) == 19
        assert "auto_deactivated" not in old


class TestMigrationDdl:
    def test_upgrade_drops_then_adds_full_vocabulary_not_valid_then_validates(self) -> None:
        migration = _load_migration()
        mock_op = _run_on_mock_postgres(migration, "upgrade")
        sql = _executed_sql(mock_op)
        drop = next(s for s in sql if "DROP CONSTRAINT IF EXISTS" in s)
        assert _CHECK_CONSTRAINT_NAME in drop
        add = next(s for s in sql if "ADD CONSTRAINT" in s and "CHECK (validation_result IN" in s)
        assert "NOT VALID" in add
        for value in VALIDATION_RESULT_VALUES:
            assert value in add, f"upgrade constraint missing {value!r}"
        assert "auto_deactivated" in add
        assert "VALIDATE CONSTRAINT" in "\n".join(sql)

    def test_downgrade_restores_19_value_vocabulary(self) -> None:
        migration = _load_migration()
        mock_op = _run_on_mock_postgres(migration, "downgrade")
        sql = _executed_sql(mock_op)
        add = next(s for s in sql if "ADD CONSTRAINT" in s and "CHECK (validation_result IN" in s)
        for value in _OLD_VALIDATION_RESULT_VALUES:
            assert value in add, f"downgrade constraint missing {value!r}"
        assert "auto_deactivated" not in add
        assert "NOT VALID" not in add


class TestOrphanRowGuard:
    def test_upgrade_fails_loudly_on_out_of_vocabulary_row(self) -> None:
        migration = _load_migration()
        with pytest.raises(RuntimeError, match="out-of-vocabulary validation_result"):
            _run_on_mock_postgres(migration, "upgrade", orphan_values=["stale_value"])

    def test_downgrade_fails_loudly_on_auto_deactivated_row(self) -> None:
        """A row carrying ``auto_deactivated`` at downgrade must fail loudly —
        the old 19-value constraint cannot express it."""
        migration = _load_migration()
        with pytest.raises(RuntimeError, match="auto_deactivated"):
            _run_on_mock_postgres(migration, "downgrade", orphan_values=["auto_deactivated"])


class TestSqliteRoundTrip:
    """Real in-memory SQLite round-trip through the migration's batch path —
    the SQLite analogue of the Postgres NOT VALID + VALIDATE flow (the batch
    rebuild enforces the new check on existing rows)."""

    @pytest.fixture
    async def sqlite_engine(self) -> AsyncEngine:
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE trigger_events ("
                "id VARCHAR(36) NOT NULL, "
                "organisation_id VARCHAR(36) NOT NULL, "
                "trigger_id VARCHAR(36) NOT NULL, "
                "trigger_type VARCHAR(20) NOT NULL, "
                "raw_payload_hash VARCHAR(64) NOT NULL, "
                "received_at DATETIME NOT NULL, "
                "validation_result VARCHAR(50) NOT NULL, "
                "run_id VARCHAR(36), "
                "error_detail VARCHAR(2000), "
                "created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL, "
                f"PRIMARY KEY (id), CONSTRAINT {_CHECK_CONSTRAINT_NAME} "
                f"CHECK (validation_result IN ({_vocab_sql(_OLD_VALIDATION_RESULT_VALUES)}))"
                ")"
            )
        yield engine
        await engine.dispose()

    async def _run_migration(self, engine: AsyncEngine, func_name: str) -> None:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: self._execute_with_operations(
                    migration=_load_migration(),
                    conn=sync_conn,
                    func_name=func_name,
                )
            )

    @staticmethod
    def _execute_with_operations(migration: ModuleType, conn: object, func_name: str) -> None:
        ctx = MigrationContext.configure(conn, opts={"render_as_batch": True})
        ops = Operations(ctx)
        with patch.object(migration, "op", ops):
            getattr(migration, func_name)()

    @staticmethod
    async def _insert_event(engine: AsyncEngine, validation_result: str) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO trigger_events (id, organisation_id, trigger_id, trigger_type, "
                    "raw_payload_hash, received_at, validation_result, created_at, updated_at) "
                    "VALUES (:id, :oid, :tid, 'ongoing', 'hash', CURRENT_TIMESTAMP, :vr, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(uuid.uuid4()),
                    "tid": str(uuid.uuid4()),
                    "vr": validation_result,
                },
            )

    async def test_pre_upgrade_constraint_rejects_auto_deactivated(self, sqlite_engine: AsyncEngine) -> None:
        """The pre-migration 19-value constraint rejects the new value — this is
        the exact production bug the FAR-190 streak engine hit."""
        with pytest.raises(IntegrityError):
            await self._insert_event(sqlite_engine, "auto_deactivated")

    async def test_upgrade_widens_constraint_to_accept_auto_deactivated(self, sqlite_engine: AsyncEngine) -> None:
        """After the upgrade, an ``auto_deactivated`` TriggerEvent row persists —
        the FAR-190 deactivation transaction no longer rolls back."""
        await self._run_migration(sqlite_engine, "upgrade")
        await self._insert_event(sqlite_engine, "auto_deactivated")
        async with sqlite_engine.begin() as conn:
            rows = (await conn.execute(text("SELECT validation_result FROM trigger_events"))).scalars().all()
        assert rows == ["auto_deactivated"]

    async def test_downgrade_restores_rejection_on_clean_table(self, sqlite_engine: AsyncEngine) -> None:
        """On a clean table (no orphan rows) the downgrade restores the 19-value
        constraint and the value is rejected again."""
        await self._run_migration(sqlite_engine, "upgrade")
        await self._run_migration(sqlite_engine, "downgrade")
        with pytest.raises(IntegrityError):
            await self._insert_event(sqlite_engine, "auto_deactivated")

    async def test_downgrade_fails_loudly_on_orphan_row(self, sqlite_engine: AsyncEngine) -> None:
        """A row carrying ``auto_deactivated`` at downgrade fails loudly — the
        SQLite batch rebuild enforces the old check on existing rows (the
        SQLite analogue of the Postgres orphan-row guard)."""
        await self._run_migration(sqlite_engine, "upgrade")
        await self._insert_event(sqlite_engine, "auto_deactivated")
        with pytest.raises(IntegrityError):
            await self._run_migration(sqlite_engine, "downgrade")
