"""Vocabulary/constraint tests for the FAR-192 ``auto_deactivated`` widening.

Covers the ``0104_trigger_event_auto_deactivated`` migration and its contract
with the ORM model:

* the model vocabulary (``VALIDATION_RESULT_VALUES``) contains
  ``auto_deactivated`` and the ORM CHECK constraint reflects it,
* the migration's hardcoded vocabulary stays in sync with the model (the
  single source of truth) — comparing against the model MINUS the value added
  after 0104 by FAR-214 (``guardrail_blocked``, migration 0106),
* the migration sits on ``0103_lifecycle_map_version_actor`` and the chain is
  revised up to the FAR-214 head ``0106_trigger_event_guardrail_blocked`` (the
  sole head),
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

# The FAR-214 migration that revises the 0104 chain and is the current head.
_HEAD_MIGRATION_NAME = "0106_trigger_event_guardrail_blocked"

# The value added AFTER 0104 shipped (by FAR-214 migration 0106). 0104's own
# hardcoded vocabulary predates it, so 0104-era comparisons exclude it.
_POST_0104_ADDITIONS = frozenset({"guardrail_blocked"})

# The 19-value vocabulary BEFORE this migration (what 0069 created).
_OLD_VALIDATION_RESULT_VALUES = tuple(
    v for v in VALIDATION_RESULT_VALUES if v not in ("auto_deactivated", "guardrail_blocked")
)

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

    def test_model_vocabulary_has_21_values(self) -> None:
        assert len(VALIDATION_RESULT_VALUES) == 21
        assert len(set(VALIDATION_RESULT_VALUES)) == len(VALIDATION_RESULT_VALUES)

    def test_orm_check_constraint_includes_auto_deactivated(self) -> None:
        from modulo.db.models.trigger_event import TriggerEvent

        checks = [c for c in TriggerEvent.__table_args__ if isinstance(c, CheckConstraint)]
        check = next(c for c in checks if c.name == _CHECK_CONSTRAINT_NAME)
        assert "auto_deactivated" in check.sqltext.text


class TestMigrationChain:
    def test_migration_is_revised_by_head_0106(self) -> None:
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
        assert head.down_revision == "0105_guardrail_pins"

    def test_migration_vocabulary_matches_model(self) -> None:
        """The migration's hardcoded vocabulary must equal the ORM single source
        of truth — a value added to one side and not the other breaks either the
        migration (constraint rejects a value the model allows) or the model
        (the ORM CHECK rejects a value the migration accepts).

        0104 shipped BEFORE FAR-214 added ``guardrail_blocked`` (migration
        0106), so the comparison excludes the post-0104 additions."""
        migration = _load_migration()
        model_vocab = tuple(v for v in VALIDATION_RESULT_VALUES if v not in _POST_0104_ADDITIONS)
        assert tuple(migration._VALIDATION_RESULT_VALUES) == model_vocab, (
            "migration _VALIDATION_RESULT_VALUES must equal the model vocabulary "
            f"(missing: {sorted(set(model_vocab) - set(migration._VALIDATION_RESULT_VALUES))}, "
            f"extra: {sorted(set(migration._VALIDATION_RESULT_VALUES) - set(model_vocab))})"
        )

    def test_migration_old_vocabulary_is_19_values(self) -> None:
        migration = _load_migration()
        old = tuple(migration._OLD_VALIDATION_RESULT_VALUES)
        assert len(old) == 19
        assert "auto_deactivated" not in old


class TestGuardrailBlockedMigration0106:
    """FAR-214 vocabulary twin for migration 0106 (the current head).

    0106 widens the constraint with ``guardrail_blocked``. Its hardcoded
    ``_VALIDATION_RESULT_VALUES`` must equal the FULL model vocabulary (a value
    added to one side and not the other breaks the CHECK on real Postgres and
    silently rolls back the block transaction — the FAR-190 hard-DB-gate)."""

    _MIGRATION_PATH = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "modulo"
        / "db"
        / "migrations"
        / "versions"
        / "0106_trigger_event_guardrail_blocked.py"
    )

    def test_migration_0106_is_sole_head(self) -> None:
        migration = self._load_0106()
        assert migration.revision == "0106_trigger_event_guardrail_blocked"
        assert migration.down_revision == "0105_guardrail_pins"
        assert _script().get_heads() == ["0106_trigger_event_guardrail_blocked"]

    def test_0106_vocabulary_matches_full_model(self) -> None:
        """0106's hardcoded twin equals the FULL model vocabulary — including
        ``guardrail_blocked`` (unlike 0104's, which predates it)."""
        migration = self._load_0106()
        assert tuple(migration._VALIDATION_RESULT_VALUES) == VALIDATION_RESULT_VALUES, (
            "0106 _VALIDATION_RESULT_VALUES must equal the model vocabulary "
            f"(missing: {sorted(set(VALIDATION_RESULT_VALUES) - set(migration._VALIDATION_RESULT_VALUES))}, "
            f"extra: {sorted(set(migration._VALIDATION_RESULT_VALUES) - set(VALIDATION_RESULT_VALUES))})"
        )
        assert "guardrail_blocked" in migration._VALIDATION_RESULT_VALUES

    def test_0106_old_vocabulary_is_20_values(self) -> None:
        migration = self._load_0106()
        old = tuple(migration._OLD_VALIDATION_RESULT_VALUES)
        assert len(old) == 20
        assert "guardrail_blocked" not in old

    def test_0106_upgrade_adds_guardrail_blocked_constraint(self) -> None:
        migration = self._load_0106()
        mock_op = _run_on_mock_postgres(migration, "upgrade")
        sql = _executed_sql(mock_op)
        add = next(s for s in sql if "ADD CONSTRAINT" in s and "CHECK (validation_result IN" in s)
        for value in VALIDATION_RESULT_VALUES:
            assert value in add, f"0106 upgrade constraint missing {value!r}"
        assert "NOT VALID" in add
        assert "VALIDATE CONSTRAINT" in "\n".join(sql)

    def test_0106_downgrade_restores_20_value_vocabulary(self) -> None:
        migration = self._load_0106()
        mock_op = _run_on_mock_postgres(migration, "downgrade")
        sql = _executed_sql(mock_op)
        add = next(s for s in sql if "ADD CONSTRAINT" in s and "CHECK (validation_result IN" in s)
        for value in ("auto_deactivated", "paused"):
            assert value in add, f"0106 downgrade constraint missing {value!r}"
        assert "guardrail_blocked" not in add
        assert "NOT VALID" not in add

    def _load_0106(self) -> ModuleType:
        assert self._MIGRATION_PATH.exists(), f"Migration file missing: {self._MIGRATION_PATH}"
        spec = importlib.util.spec_from_file_location("migration_0106", self._MIGRATION_PATH)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class TestMigrationDdl:
    def test_upgrade_drops_then_adds_full_vocabulary_not_valid_then_validates(self) -> None:
        migration = _load_migration()
        mock_op = _run_on_mock_postgres(migration, "upgrade")
        sql = _executed_sql(mock_op)
        drop = next(s for s in sql if "DROP CONSTRAINT IF EXISTS" in s)
        assert _CHECK_CONSTRAINT_NAME in drop
        add = next(s for s in sql if "ADD CONSTRAINT" in s and "CHECK (validation_result IN" in s)
        assert "NOT VALID" in add
        for value in migration._VALIDATION_RESULT_VALUES:
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
