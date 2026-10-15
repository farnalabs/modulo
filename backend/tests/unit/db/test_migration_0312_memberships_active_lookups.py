"""Unit tests for migration 0312_memberships_active_lookups.

Structural contract (no Postgres needed):

* **Revision chain** — 0312 revises ``0311_library_cluster_schema_hygiene``; the
  migrations directory has exactly one head (``0314_external_agents_mcp_servers``,
  pinned by all sibling chain tests), the revision id matches the filename, and
  neither ``branch_labels`` nor ``depends_on`` is set (the pre-commit
  check-migration-heads hook and every ``test_single_head_*`` pin depend on
  that shape).
* **Upgrade (mocked ``op``)** — emits exactly one ``create_index`` for
  ``ix_org_memberships_org_role_active`` on ``(organisation_id, role)`` with the
  partial predicate ``deactivated_at IS NULL`` (``postgresql_where``) and
  ``if_not_exists``, and exactly one ``alter_column`` setting
  ``team_memberships.role``'s ``server_default`` to ``'viewer'``.
* **Downgrade** — reverses both legs: clears the ``team_memberships.role``
  server default and drops the index (``if_exists``).
* **Model parity** — ``OrgMembership`` declares the same partial index (name,
  columns, both ``postgresql_where`` / ``sqlite_where`` predicates) and
  ``TeamMembership.role`` carries the same ``server_default='viewer'``, so
  model/migration drift cannot silently reappear.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

from alembic.script import ScriptDirectory
from sqlalchemy import String

from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.team_membership import TeamMembership

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_REVISION = "0312_memberships_active_lookups"
_MIGRATION_DOWN_REVISION = "0311_library_cluster_schema_hygiene"
_CHAIN_HEAD_MIGRATION = "0314_external_agents_mcp_servers"

_INDEX_NAME = "ix_org_memberships_org_role_active"
_INDEX_TABLE = "org_memberships"
_INDEX_COLUMNS = ("organisation_id", "role")
_ACTIVE_WHERE = "deactivated_at IS NULL"

_TEAM_TABLE = "team_memberships"
_TEAM_ROLE_COLUMN = "role"
_ROLE_SERVER_DEFAULT = "viewer"

_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_REVISION}.py"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_REVISION}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def _migration_calls(entry_point: str) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    """Every op.* call the entry point makes, in order."""
    module = _load_migration()
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(name: str):
        def _side_effect(*args: Any, **kwargs: Any) -> Any:
            calls.append((name, args, kwargs))
            return MagicMock()

        return _side_effect

    mock_op = MagicMock()
    for name in ("create_index", "drop_index", "alter_column"):
        getattr(mock_op, name).side_effect = _record(name)

    with patch.object(module, "op", mock_op):
        getattr(module, entry_point)()
    return calls


def _only(calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]], name: str) -> tuple[Any, ...]:
    matching = [call for call in calls if call[0] == name]
    assert len(matching) == 1, f"expected exactly one {name} call, got {matching}"
    return matching[0]


class TestChain:
    def test_single_head_is_0314_external_agents_mcp_servers(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_CHAIN_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0311_library_cluster_schema_hygiene(self) -> None:
        assert _load_migration().down_revision == _MIGRATION_DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_REVISION

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_creates_exactly_one_index(self) -> None:
        calls = [call for call in _migration_calls("upgrade") if call[0] == "create_index"]
        assert len(calls) == 1, f"expected exactly one create_index call, got {calls}"

    def test_index_name_table_and_columns(self) -> None:
        _name, args, _kwargs = _only(_migration_calls("upgrade"), "create_index")
        # create_index(index_name, table_name, columns, ...)
        assert args[0] == _INDEX_NAME
        assert args[1] == _INDEX_TABLE
        assert tuple(args[2]) == _INDEX_COLUMNS

    def test_index_is_partial_on_deactivated_at(self) -> None:
        """The partial predicate keeps tombstoned rows (which only grow) out
        of the btree and matches the model's ``postgresql_where`` declaration."""
        _name, _args, kwargs = _only(_migration_calls("upgrade"), "create_index")
        pg_where = kwargs.get("postgresql_where")
        assert pg_where is not None, "the active-admin index must be partial"
        assert str(pg_where) == _ACTIVE_WHERE
        assert kwargs.get("if_not_exists") is True

    def test_sets_team_role_server_default(self) -> None:
        _name, args, kwargs = _only(_migration_calls("upgrade"), "alter_column")
        assert args[0] == _TEAM_TABLE
        assert args[1] == _TEAM_ROLE_COLUMN
        assert kwargs.get("server_default") == _ROLE_SERVER_DEFAULT
        assert kwargs.get("nullable") is False

    def test_alter_column_keeps_the_existing_role_type(self) -> None:
        _name, _args, kwargs = _only(_migration_calls("upgrade"), "alter_column")
        existing_type = kwargs.get("existing_type")
        assert isinstance(existing_type, String)
        assert existing_type.length == 20

    def test_upgrade_never_uses_concurrently(self) -> None:
        """Alembic runs inside a single transaction (the migration docstring
        explains why), so ``CREATE INDEX CONCURRENTLY`` cannot be used. The
        docstring legitimately NAMES it while explaining that, so the
        assertion runs against the EXECUTED calls, not the source."""
        calls = _migration_calls("upgrade")
        assert all("CONCURRENTLY" not in str(call) for call in calls)


class TestDowngrade:
    def test_clears_the_team_role_server_default(self) -> None:
        _name, args, kwargs = _only(_migration_calls("downgrade"), "alter_column")
        assert args[0] == _TEAM_TABLE
        assert args[1] == _TEAM_ROLE_COLUMN
        assert kwargs.get("server_default") is None

    def test_drops_the_index(self) -> None:
        _name, args, kwargs = _only(_migration_calls("downgrade"), "drop_index")
        assert args[0] == _INDEX_NAME
        assert kwargs.get("table_name") == _INDEX_TABLE
        assert kwargs.get("if_exists") is True

    def test_downgrade_never_uses_concurrently(self) -> None:
        calls = _migration_calls("downgrade")
        assert all("CONCURRENTLY" not in str(call) for call in calls)


class TestMigrationSource:
    def test_never_edits_shipped_revisions(self) -> None:
        # The membership tables are shipped; 0312 changes their effects via NEW
        # DDL, never by editing an earlier revision file.
        source = _source()
        assert "0001_v2_identity_org.py" not in source
        assert "0002_v2_teams_library.py" not in source


class TestModelParity:
    def test_org_membership_declares_the_partial_index(self) -> None:
        matches = [
            index
            for index in OrgMembership.__table__.indexes
            if index.name == _INDEX_NAME and tuple(c.name for c in index.columns) == _INDEX_COLUMNS
        ]
        assert matches, (
            f"OrgMembership must declare {_INDEX_NAME}{_INDEX_COLUMNS} to stay in parity with "
            "migration 0312 — a migration/model index drift is a review block"
        )

    def test_index_predicates_match_the_migration(self) -> None:
        """``postgresql_where`` alone is ignored on SQLite, so the model must
        declare ``sqlite_where`` too — and both must match the migration's
        exact predicate."""
        indexes = [i for i in OrgMembership.__table__.indexes if i.name == _INDEX_NAME]
        assert indexes, f"OrgMembership must declare {_INDEX_NAME}"
        index = indexes[0]
        pg_where = index.dialect_options["postgresql"].get("where")
        sqlite_where = index.dialect_options["sqlite"].get("where")
        assert pg_where is not None
        assert sqlite_where is not None
        assert str(pg_where) == _ACTIVE_WHERE
        assert str(sqlite_where) == _ACTIVE_WHERE

    def test_team_membership_role_has_server_default_viewer(self) -> None:
        column = TeamMembership.__table__.columns[_TEAM_ROLE_COLUMN]
        assert column.server_default is not None, (
            "TeamMembership.role must carry server_default='viewer' to stay in parity with "
            "migration 0312's ALTER COLUMN ... SET DEFAULT"
        )
        assert str(column.server_default.arg) == _ROLE_SERVER_DEFAULT
        assert column.nullable is False
