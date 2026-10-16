"""Unit tests for migration 0115_notification_preferences (FAR-247).

Structural contract (no Postgres needed):

* **Revision chain** — 0115 revises ``0114_org_api_keys_run_id``; the tree has
  exactly one head and 0115 is on that chain. 0115 is mid-chain, so the tip
  legitimately moves as later migrations land on top; the exact head is pinned
  by the sibling chain tests, not here. The revision id matches the filename;
  no branch labels.
* **Upgrade (mocked ``op``)** — creates ``notification_preferences`` with the
  org/account/category unique constraint and a ``String(100)`` category (NO
  CHECK — the vocabulary is code-driven), then — on Postgres — runs the 0066
  role ceremony (``SET ROLE modulo_migrate`` + owner assertion), ENABLE +
  **FORCE** ROW LEVEL SECURITY, the org-scope SELECT policy, the per-user
  INSERT/UPDATE/DELETE policies (``app.user_id``, never ``app.account_id``),
  and the DML GRANT to ``modulo_app``.
* **Downgrade** — drops the four policies, disables RLS, drops the indexes
  and the table.
* **Model parity** — the ORM model carries the same unique constraint and
  column width the migration writes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Column, String, UniqueConstraint

from modulo.db.models.notification import NotificationPreference

_MIGRATION_REVISION = "0115_notification_preferences"
_MIGRATION_DOWN_REVISION = "0114_org_api_keys_run_id"

_UNIQUE_NAME = "uq_notification_preferences_org_account_category"

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_REVISION}.py"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_REVISION}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Bind:
    """Fake connection for ``op.get_bind()``.

    ``role_exists=False`` models a fresh dev/BDD DB (no modulo_migrate /
    modulo_app roles — the migration skips the ceremony); ``owner`` is what
    the post-create ownership assertion reads back.
    """

    def __init__(self, *, dialect: str = "postgresql", role_exists: bool = True, owner: str = "modulo_migrate") -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self._role_exists = role_exists
        self._owner = owner

    def execute(self, stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        sql = str(stmt)
        result = MagicMock()
        if "pg_roles" in sql:
            result.scalar_one_or_none.return_value = 1 if self._role_exists else None
        elif "relowner" in sql:
            result.scalar_one_or_none.return_value = self._owner
        else:
            result.scalar_one_or_none.return_value = None
        return result


class _Recorder:
    """Captures op.execute SQL and the create/drop calls."""

    def __init__(self, bind: _Bind) -> None:
        self._bind = bind
        self.sql: list[str] = []
        self.created_tables: list[tuple[str, tuple[object, ...]]] = []
        self.created_indexes: list[tuple[str, str, tuple[str, ...]]] = []
        self.dropped_indexes: list[str] = []
        self.dropped_tables: list[str] = []

    def get_bind(self) -> _Bind:
        return self._bind

    def execute(self, stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        self.sql.append(str(stmt))
        return MagicMock()

    def create_table(self, name: str, *args: object, **_kwargs: object) -> None:
        self.created_tables.append((name, args))

    def create_index(self, name: str, table_name: str, columns: tuple[str, ...], **_kwargs: object) -> None:
        self.created_indexes.append((name, table_name, columns))

    def drop_index(self, name: str, **_kwargs: object) -> None:
        self.dropped_indexes.append(name)

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


def _run(what: str, *, bind: _Bind | None = None) -> _Recorder:
    module = _load_migration()
    rec = _Recorder(bind or _Bind())
    with patch.object(module, "op", rec):
        getattr(module, what)()
    return rec


def _joined(rec: _Recorder) -> str:
    return "\n".join(rec.sql)


def _table_args(rec: _Recorder) -> tuple[object, ...]:
    tables = [args for name, args in rec.created_tables if name == "notification_preferences"]
    assert len(tables) == 1, f"expected one create_table for notification_preferences, got {rec.created_tables}"
    return tables[0]


class TestChain:
    def test_single_head_and_this_migration_is_an_ancestor(self) -> None:
        # 0115 is mid-chain: the tip moves as later migrations land on top (a
        # sibling migration may become the head at any time). The invariant
        # this test owns is that the chain still has exactly ONE head and that
        # 0115 is on it — not the identity of the tip.
        script = ScriptDirectory(str(_VERSIONS.parent))
        heads = script.get_heads()
        assert len(heads) == 1, f"expected a single head, got {heads}"
        revisions = {rev.revision for rev in script.walk_revisions()}
        assert _MIGRATION_REVISION in revisions, f"{_MIGRATION_REVISION} is not on the migration chain"

    def test_down_revision_is_0114_org_api_keys_run_id(self) -> None:
        assert _load_migration().down_revision == _MIGRATION_DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_REVISION

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgradeRolesPresent:
    """Full 0066 ceremony: roles exist, Postgres dialect."""

    def test_sets_and_resets_migrate_role_around_create(self) -> None:
        joined = _joined(_run("upgrade"))
        assert "SET ROLE modulo_migrate" in joined
        assert "RESET ROLE" in joined

    def test_creates_table_with_unique_constraint_and_string100_category(self) -> None:
        args = _table_args(_run("upgrade"))
        uniques = [a for a in args if isinstance(a, UniqueConstraint)]
        assert any(u.name == _UNIQUE_NAME for u in uniques), f"missing {_UNIQUE_NAME} in {uniques}"
        category_cols = [a for a in args if isinstance(a, Column) and a.name == "category"]
        assert len(category_cols) == 1
        category_type = category_cols[0].type
        assert isinstance(category_type, String)
        assert category_type.length == 100
        # No CHECK on category — the vocabulary is code-driven (event_mapper).
        assert not any(isinstance(a, CheckConstraint) for a in args), "category must not carry a CHECK constraint"

    def test_enables_and_forces_row_level_security(self) -> None:
        joined = _joined(_run("upgrade"))
        assert "ALTER TABLE notification_preferences ENABLE ROW LEVEL SECURITY" in joined
        assert "ALTER TABLE notification_preferences FORCE ROW LEVEL SECURITY" in joined

    def test_policies_split_org_select_and_per_user_writes(self) -> None:
        joined = _joined(_run("upgrade"))
        assert "CREATE POLICY rls_org_isolation ON notification_preferences FOR SELECT USING" in joined, (
            "org-scope SELECT policy missing"
        )
        for verb in ("INSERT", "UPDATE", "DELETE"):
            assert f"rls_user_isolation_{verb.lower()} ON notification_preferences" in joined
        # app.user_id is the value set_rls_user_context writes; app.account_id
        # is never set and must not appear in any policy body.
        assert "app.user_id" in joined
        assert "app.account_id" not in joined

    def test_grants_dml_to_app_role(self) -> None:
        joined = _joined(_run("upgrade"))
        assert "GRANT SELECT, INSERT, UPDATE, DELETE ON notification_preferences TO modulo_app" in joined, (
            "modulo_app DML grant missing"
        )

    def test_creates_the_two_lookup_indexes(self) -> None:
        names = {name for name, _table, _cols in _run("upgrade").created_indexes}
        assert "ix_notification_preferences_organisation_id" in names
        assert "ix_notification_preferences_account_id" in names

    def test_owner_assertion_rejects_app_role_ownership(self) -> None:
        module = _load_migration()
        rec = _Recorder(_Bind(owner="modulo_app"))
        with (
            patch.object(module, "op", rec),
            pytest.raises(RuntimeError, match="owner is"),
        ):
            module.upgrade()


class TestUpgradeFreshDatabase:
    """Roles absent (fresh dev/BDD DB): the table + RLS still land; the
    SET ROLE / GRANT ceremony is skipped."""

    def test_skips_role_ceremony_but_still_forces_rls(self) -> None:
        rec = _run("upgrade", bind=_Bind(role_exists=False))
        joined = _joined(rec)
        assert "SET ROLE modulo_migrate" not in joined
        assert "RESET ROLE" not in joined
        assert "TO modulo_app" not in joined
        assert "FORCE ROW LEVEL SECURITY" in joined
        assert len(rec.created_tables) == 1

    def test_non_postgres_dialect_skips_all_rls_sql(self) -> None:
        rec = _run("upgrade", bind=_Bind(dialect="sqlite", role_exists=False))
        joined = _joined(rec)
        assert "ROW LEVEL SECURITY" not in joined
        assert "CREATE POLICY" not in joined


class TestDowngrade:
    def test_drops_policies_disables_rls_and_drops_table(self) -> None:
        rec = _run("downgrade")
        joined = _joined(rec)
        for policy in (
            "rls_user_isolation_delete",
            "rls_user_isolation_update",
            "rls_user_isolation_insert",
            "rls_org_isolation",
        ):
            assert f"DROP POLICY IF EXISTS {policy} ON notification_preferences" in joined
        assert "ALTER TABLE notification_preferences DISABLE ROW LEVEL SECURITY" in joined
        assert rec.dropped_tables == ["notification_preferences"]
        assert "ix_notification_preferences_organisation_id" in rec.dropped_indexes
        assert "ix_notification_preferences_account_id" in rec.dropped_indexes


class TestModelParity:
    def test_model_carries_same_unique_constraint(self) -> None:
        uniques = [c for c in NotificationPreference.__table__.constraints if isinstance(c, UniqueConstraint)]
        assert any(u.name == _UNIQUE_NAME for u in uniques)

    def test_model_category_is_string100_without_check(self) -> None:
        category = NotificationPreference.__table__.c.category
        assert isinstance(category.type, String)
        assert category.type.length == 100
        checks = [c for c in NotificationPreference.__table__.constraints if isinstance(c, CheckConstraint)]
        assert not checks
