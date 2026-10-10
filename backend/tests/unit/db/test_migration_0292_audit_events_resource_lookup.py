"""Unit tests for migration 0292_audit_events_resource_lookup.

Structural + model-parity contract (no Postgres / Testcontainers needed):

* **Revision chain** - the revision/down_revision pin this migration onto the
  0291_invitations_lookup_constraints parent; the migrations directory has
  exactly one head (0294_eval_results_org_fk, pinned by all sibling
  chain tests) so the pre-commit check-migration-heads hook can never be
  ambushed by a renumber.
* **Index shape (mocked ``op``)** - the upgrade creates exactly one index,
  ``ix_audit_events_org_resource`` on
  ``(organisation_id, resource_type, resource_id)``; the downgrade drops it.
  The composite must lead on the tenant column so the RLS org filter is the
  index prefix.
* **Postgres-only default leg** - the ``event_count`` server default is
  Postgres-only; on any other dialect the upgrade creates the index and
  returns (the 0246 precedent).
* **No duplicate CHECK** - the ``event_count >= 0`` guard is migration-owned
  by ``0165_add_check_constraints`` (``ck_audit_event_event_count``); 0292
  must emit no ``CHECK`` DDL, because a second predicate would double-enforce
  on the hot ``audit_chain_heads`` upsert path and the pre-flight scan would
  be dead code on a chain-migrated DB.
* **Model parity** - ``AuditEvent.__table_args__`` declares the same index
  (name, ordered columns), so ``create_all``'d SQLite schemas and migrated
  Postgres schemas agree; the counter CHECK stays out of the ORM for the same
  migration-owned reason.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from modulo.db.models.audit_event import AuditChainHead, AuditEvent

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0292_audit_events_resource_lookup"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0291_invitations_lookup_constraints"
_HEAD_MIGRATION = "0294_eval_results_org_fk"

_INDEX = "ix_audit_events_org_resource"
_TABLE = "audit_events"
_COLUMNS = ["organisation_id", "resource_type", "resource_id"]

_SET_DEFAULT = "ALTER TABLE audit_chain_heads ALTER COLUMN event_count SET DEFAULT 0"
_DROP_DEFAULT = "ALTER TABLE audit_chain_heads ALTER COLUMN event_count DROP DEFAULT"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(dialect: str, entry_point: str) -> tuple[list, list, list, list]:
    """Run ``upgrade``/``downgrade`` against a mocked ``op``.

    Returns ``(created, dropped, executed, bound)`` where ``created`` is the
    recorded ``op.create_index`` calls as ``(name, table, columns)``,
    ``dropped`` is the ``op.drop_index`` calls as ``(name, table_name)``,
    ``executed`` is the ``op.execute`` statements and ``bound`` is the
    ``op.get_bind().execute`` statements - each in call order.
    """
    module = _load_migration()
    created: list[tuple[str, str, list[str]]] = []
    dropped: list[tuple[str, str | None]] = []
    executed: list[str] = []
    bound: list[str] = []

    def _record_create(name: str, table: str, columns: list[str], *_a: object, **_kw: object) -> None:
        created.append((name, table, list(columns)))

    def _record_drop(name: str, *_a: object, **kw: object) -> None:
        dropped.append((name, kw.get("table_name")))

    def _record_op(stmt: object, *_a: object, **_kw: object) -> None:
        executed.append(str(getattr(stmt, "text", stmt)))

    def _record_bind(stmt: object, *_a: object, **_kw: object) -> None:
        bound.append(str(getattr(stmt, "text", stmt)))

    with patch.object(module, "op") as op:
        op.get_bind.return_value.dialect.name = dialect
        op.get_bind.return_value.execute.side_effect = _record_bind
        op.create_index.side_effect = _record_create
        op.drop_index.side_effect = _record_drop
        op.execute.side_effect = _record_op
        getattr(module, entry_point)()
    return created, dropped, executed, bound


class TestChain:
    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_down_revision_is_0291_invitations_lookup_constraints(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_single_head_is_0293(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestIndexShape:
    def test_upgrade_creates_exactly_the_resource_lookup_index(self) -> None:
        created, _dropped, _executed, _bound = _run("postgresql", "upgrade")
        assert created == [(_INDEX, _TABLE, _COLUMNS)], created

    def test_composite_leads_on_the_tenant_column(self) -> None:
        created, _dropped, _executed, _bound = _run("postgresql", "upgrade")
        _name, _table, columns = created[0]
        assert columns[0] == "organisation_id", "the RLS org filter must be the index prefix"

    def test_non_postgres_upgrade_still_creates_the_index(self) -> None:
        created, _dropped, _executed, _bound = _run("sqlite", "upgrade")
        assert created == [(_INDEX, _TABLE, _COLUMNS)], created

    def test_downgrade_drops_the_index(self) -> None:
        _created, dropped, _executed, _bound = _run("postgresql", "downgrade")
        assert dropped == [(_INDEX, _TABLE)], dropped


class TestPostgresDefaultLeg:
    def test_upgrade_sets_the_event_count_default(self) -> None:
        _created, _dropped, executed, bound = _run("postgresql", "upgrade")
        assert executed == [], executed
        assert bound == [_SET_DEFAULT], bound

    def test_non_postgres_upgrade_skips_the_default_leg(self) -> None:
        _created, _dropped, executed, bound = _run("sqlite", "upgrade")
        assert executed == [], executed
        assert bound == [], bound

    def test_postgres_downgrade_drops_the_default(self) -> None:
        _created, _dropped, executed, bound = _run("postgresql", "downgrade")
        assert executed == [], executed
        assert bound == [_DROP_DEFAULT], bound

    def test_non_postgres_downgrade_only_drops_the_index(self) -> None:
        _created, dropped, executed, bound = _run("sqlite", "downgrade")
        assert executed == [], executed
        assert bound == [], bound
        assert dropped == [(_INDEX, _TABLE)], dropped


class TestNoDuplicateCheck:
    def test_upgrade_emits_no_check_constraint_ddl(self) -> None:
        # The event_count >= 0 guard is owned by 0165; 0292 must not re-add it.
        _created, _dropped, executed, bound = _run("postgresql", "upgrade")
        assert executed == [], executed
        assert not any("CHECK" in stmt for stmt in bound), bound

    def test_downgrade_emits_no_check_constraint_ddl(self) -> None:
        _created, _dropped, executed, bound = _run("postgresql", "downgrade")
        assert executed == [], executed
        assert not any("CONSTRAINT" in stmt for stmt in bound), bound


class TestModelParity:
    def test_model_declares_the_resource_lookup_index_with_matching_columns(self) -> None:
        declared = {idx.name: idx for idx in AuditEvent.__table__.indexes if idx.name is not None}
        assert _INDEX in declared, f"model/migration drift: {_INDEX} missing from the ORM"
        assert [col.name for col in declared[_INDEX].columns] == _COLUMNS

    def test_model_declares_both_audit_event_composites(self) -> None:
        declared = {idx.name for idx in AuditEvent.__table__.indexes if idx.name is not None}
        assert {"ix_audit_events_org_type_actor_time", _INDEX} <= declared

    def test_model_does_not_declare_the_counter_check(self) -> None:
        # Migration-owned by 0165 (_MIGRATION_OWNED_CHECKS in
        # tests/integration/test_initial_migration.py): the ORM must not
        # declare the duplicate.
        checks = {
            constraint.name
            for constraint in AuditChainHead.__table__.constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert not checks, checks
