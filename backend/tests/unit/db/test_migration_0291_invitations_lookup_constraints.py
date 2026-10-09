"""Unit tests for migration 0291_invitations_lookup_constraints.

Structural + SQLite-model contract (no Postgres / Testcontainers needed):

* **Revision chain** — the revision/down_revision pin this migration onto the
  0290_scheduled_reports_due_scan parent; the migrations directory has exactly
  one head (0292_audit_events_resource_lookup, pinned by all sibling chain
  tests) so the pre-commit check-migration-heads hook can never be ambushed by
  a renumber.
* **Index shape (mocked ``op``)** — the upgrade emits exactly the two
  idempotent ``CREATE INDEX IF NOT EXISTS`` statements the live-invite read
  paths (``crud.invitations._live_conditions``) are written against:
  ``ix_invitations_org_email_live (organisation_id, email)`` and
  ``ix_invitations_expires_at_live (expires_at)``, both leading on the tenant
  column and both carrying the shared partial predicate.
* **Predicate parity** — the emitted predicate is exactly the un-consumed /
  un-revoked null scope of ``_live_conditions``; a drift would create an index
  the planner never uses. The ``expires_at > now()`` range term is deliberately
  NOT part of the composite predicate (it cannot be an index condition on the
  org/email index) and is served by the second index instead.
* **CHECK emission** — on Postgres the upgrade adds both domain CHECKs via
  ``op.create_check_constraint`` and the downgrade drops them; on non-Postgres
  dialects the CHECK legs are skipped (a bare ``ALTER TABLE ... ADD CONSTRAINT``
  is not SQLite-executable, the 0246 precedent).
* **Model parity** — the ``Invitation`` model declares the same two partial
  indexes (name, ordered columns, predicate) and the same two CHECK names and
  expressions, so ``create_all``'d schemas and migrated ones agree.
* **CHECK effectiveness (SQLite)** — the model's CHECKs actually reject a
  disallowed ``org_role`` and a non-64-char ``token_hash`` while accepting a
  valid row.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, create_engine, text
from sqlalchemy.exc import IntegrityError

from modulo.db.crud.invitations import _live_conditions
from modulo.db.models.invitation import Invitation

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0291_invitations_lookup_constraints"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0290_scheduled_reports_due_scan"
_HEAD_MIGRATION = "0292_audit_events_resource_lookup"

#: Index name -> ordered key columns. The single source of truth asserted
#: against BOTH the migration DDL and the ORM declaration.
_INDEXES: dict[str, tuple[str, ...]] = {
    "ix_invitations_org_email_live": ("organisation_id", "email"),
    "ix_invitations_expires_at_live": ("expires_at",),
}

#: Constraint name -> CHECK expression. Checked against the migration's emitted
#: DDL and the ORM ``CheckConstraint``.
_CHECKS: dict[str, str] = {
    "ck_invitations_org_role": "org_role IN ('admin', 'operator', 'runner', 'viewer')",
    "ck_invitations_token_hash_len": "length(token_hash) = 64",
}

_LIVE_WHERE = "consumed_at IS NULL AND revoked_at IS NULL"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_code() -> str:
    """The migration's executable code, minus the module docstring."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    parts = source.split('"""', 2)
    return parts[2] if len(parts) >= 3 else source


def _expected_index_statement(name: str) -> str:
    columns = ", ".join(_INDEXES[name])
    return f"CREATE INDEX IF NOT EXISTS {name} ON public.invitations ({columns}) WHERE {_LIVE_WHERE}"


def _run_with_mocked_op(dialect: str, entry_point: str) -> tuple[list[str], list[tuple], list[tuple]]:
    """Run an entry point against a mocked ``op``.

    Returns ``(executed_statements, check_calls, drop_calls)`` where a check
    call is ``(name, table, condition_sql)`` and a drop call is
    ``(name, table, type_)``.
    """
    module = _load_migration()
    executed: list[str] = []
    check_calls: list[tuple] = []
    drop_calls: list[tuple] = []

    def _record_execute(stmt: object, *_args: object, **_kwargs: object) -> None:
        executed.append(str(getattr(stmt, "text", stmt)))

    def _record_check(*args: object, **_kwargs: object) -> None:
        check_calls.append((str(args[0]), str(args[1]), str(args[2])))

    def _record_drop(*args: object, **kwargs: object) -> None:
        drop_calls.append((str(args[0]), str(args[1]), kwargs.get("type_")))

    with patch.object(module, "op") as op:
        op.get_bind.return_value.dialect.name = dialect
        op.execute.side_effect = _record_execute
        op.create_check_constraint.side_effect = _record_check
        op.drop_constraint.side_effect = _record_drop
        getattr(module, entry_point)()
    return executed, check_calls, drop_calls


class TestChain:
    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_down_revision_is_0290_scheduled_reports_due_scan(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_single_head_is_0292(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestIndexShape:
    def test_upgrade_emits_exactly_the_two_create_index_statements(self) -> None:
        executed, _checks, _drops = _run_with_mocked_op("postgresql", "upgrade")
        assert executed == [_expected_index_statement(name) for name in _INDEXES], executed

    def test_downgrade_emits_exactly_the_two_drop_index_statements(self) -> None:
        executed, _checks, _drops = _run_with_mocked_op("postgresql", "downgrade")
        # Drops reverse the creation order (expiry index then composite).
        assert executed == [f"DROP INDEX IF EXISTS public.{name}" for name in reversed(_INDEXES)], executed

    def test_composite_index_leads_on_organisation_id(self) -> None:
        """The email equality term has no index support on its own, so the
        composite must lead with the org column the lookups scope by."""
        assert _INDEXES["ix_invitations_org_email_live"] == ("organisation_id", "email")

    def test_expiry_index_is_single_column(self) -> None:
        """A global stale-invite purge sweeps on expiry; a single-column index
        on ``expires_at`` serves that range scan."""
        assert _INDEXES["ix_invitations_expires_at_live"] == ("expires_at",)

    def test_uses_the_idempotent_create_index_if_not_exists_convention(self) -> None:
        code = _source_code()
        assert "op.create_index" not in code
        assert code.count("CREATE INDEX IF NOT EXISTS") == len(_INDEXES)


class TestPredicateParity:
    def test_migration_predicate_is_the_shared_live_null_scope(self) -> None:
        """The partial predicate must equal the un-consumed / un-revoked null
        scope from the ONE place the lookup paths define liveness."""
        conditions = _live_conditions(Invitation)
        rendered = " AND ".join(str(c) for c in conditions[:2])
        normalized = rendered.replace('"invitations".', "").replace("invitations.", "")
        assert normalized == _LIVE_WHERE, (
            f"migration predicate drifted from _live_conditions: {normalized!r} != {_LIVE_WHERE!r}"
        )

    def test_expiry_range_term_is_not_part_of_the_partial_predicate(self) -> None:
        """``expires_at > now()`` cannot be an index condition on the composite
        org/email index; it is served by ``ix_invitations_expires_at_live``."""
        assert "expires_at" not in _LIVE_WHERE
        assert _load_migration()._LIVE_WHERE == _LIVE_WHERE

    def test_each_emitted_index_carries_the_exact_predicate(self) -> None:
        executed, _checks, _drops = _run_with_mocked_op("postgresql", "upgrade")
        for name in _INDEXES:
            statement = _expected_index_statement(name)
            assert statement in executed, statement
            assert statement.endswith(f"WHERE {_LIVE_WHERE}"), statement


class TestCheckEmission:
    def test_postgres_upgrade_adds_both_checks(self) -> None:
        _executed, checks, _drops = _run_with_mocked_op("postgresql", "upgrade")
        assert checks == [(name, "invitations", _CHECKS[name]) for name in _CHECKS], checks

    def test_postgres_downgrade_drops_both_checks(self) -> None:
        _executed, _checks, drops = _run_with_mocked_op("postgresql", "downgrade")
        assert drops == [
            ("ck_invitations_token_hash_len", "invitations", "check"),
            ("ck_invitations_org_role", "invitations", "check"),
        ], drops

    def test_non_postgres_skips_the_check_legs(self) -> None:
        """SQLite cannot execute a bare ``ALTER TABLE ... ADD CONSTRAINT``, so
        the CHECK legs are Postgres-only; the model ``create_all`` path supplies
        them on SQLite (the 0246 precedent)."""
        executed, checks, _drops = _run_with_mocked_op("sqlite", "upgrade")
        assert checks == []
        assert executed == [_expected_index_statement(name) for name in _INDEXES]

    def test_non_postgres_downgrade_skips_the_check_drops(self) -> None:
        _executed, _checks, drops = _run_with_mocked_op("sqlite", "downgrade")
        assert drops == []


class TestModelParity:
    def test_model_declares_both_indexes_with_matching_columns_and_predicates(self) -> None:
        declared = {idx.name: idx for idx in Invitation.__table__.indexes if idx.name is not None}
        for name, columns in _INDEXES.items():
            assert name in declared, f"model/migration drift: {name} missing from the ORM"
            assert tuple(col.name for col in declared[name].columns) == columns, name
            postgresql_where = declared[name].dialect_options["postgresql"].get("where")
            sqlite_where = declared[name].dialect_options["sqlite"].get("where")
            assert str(postgresql_where) == _LIVE_WHERE, name
            assert str(sqlite_where) == _LIVE_WHERE, name

    def test_model_declares_both_checks_with_matching_expressions(self) -> None:
        declared = {c.name: str(c.sqltext) for c in Invitation.__table__.constraints if isinstance(c, CheckConstraint)}
        for name, expression in _CHECKS.items():
            assert name in declared, f"model/migration drift: {name} missing from the ORM"
            assert declared[name] == expression, f"{name}: {declared[name]!r} != {expression!r}"


def _insert_invitation(engine: object, org_role: str, token_hash: str) -> None:
    with engine.connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            text(
                "INSERT INTO invitations (id, organisation_id, email, display_name, "
                "org_role, token_hash, invited_by, expires_at) "
                "VALUES (:id, :org, :email, :name, :role, :token, :by, :expires)"
            ),
            {
                "id": uuid.uuid4().hex,
                "org": uuid.uuid4().hex,
                "email": f"{uuid.uuid4().hex[:8]}@example.com",
                "name": "Invitee",
                "role": org_role,
                "token": token_hash,
                "by": uuid.uuid4().hex,
                "expires": "2030-01-01 00:00:00",
            },
        )
        conn.commit()


class TestCheckEffectiveness:
    def test_model_checks_reject_bad_role_and_bad_hash(self) -> None:
        engine = create_engine("sqlite://")
        try:
            Invitation.__table__.create(engine)
            # A row inside both domains is accepted.
            _insert_invitation(engine, "admin", "a" * 64)
            # A role outside the vocabulary is rejected.
            with pytest.raises(IntegrityError, match="ck_invitations_org_role"):
                _insert_invitation(engine, "superuser", "b" * 64)
            # A token hash that is not 64 chars is rejected.
            with pytest.raises(IntegrityError, match="ck_invitations_token_hash_len"):
                _insert_invitation(engine, "viewer", "deadbeef")
        finally:
            engine.dispose()
