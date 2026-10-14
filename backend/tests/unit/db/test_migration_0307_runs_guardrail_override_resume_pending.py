"""Unit tests for migration 0307_runs_guardrail_override_resume_pending (FAR-1728).

Structural contract (no Postgres needed):

* **Revision chain** — 0307 revises ``0306_trigger_secret_encryption``;
  the migrations directory has exactly one head
  (``0312_external_agents_mcp_servers``, pinned by all sibling chain
  tests), the revision id matches the filename, and neither ``branch_labels``
  nor ``depends_on`` is set (the pre-commit check-migration-heads hook and
  every ``test_single_head_*`` pin depend on that shape).
* **Column shape (mocked ``op``)** — the upgrade emits exactly one
  ``add_column`` for ``runs.guardrail_override_resume_pending`` as a
  ``BOOLEAN NOT NULL`` with server default ``false``; the downgrade drops it.
* **ORM parity** — the ``Run`` model declares the SAME column (name, type,
  nullability, server default), so model and migration cannot drift (the
  database.md rule: a migration touching a column must update the model in
  the same change, and vice versa).

The dedicated column replaces the pre-0307 sentinel STRING that the
guardrail-override resume flip stuffed into ``runs.error_detail`` — a
control-flow marker in an observation/error column that is exposed on
REST/MCP responses. See the migration docstring for the full rationale.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

from alembic.script import ScriptDirectory
from sqlalchemy import Boolean
from sqlalchemy.sql.sqltypes import NullType

from modulo.db.models.run import Run

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0307_runs_guardrail_override_resume_pending"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0306_trigger_secret_encryption"
_CHAIN_HEAD_MIGRATION = "0312_external_agents_mcp_servers"
_COLUMN_NAME = "guardrail_override_resume_pending"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    for name in ("add_column", "drop_column"):
        getattr(mock_op, name).side_effect = _record(name)

    with patch.object(module, "op", mock_op):
        getattr(module, entry_point)()
    return calls


class TestChain:
    def test_single_head_is_0312_external_agents_mcp_servers(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_CHAIN_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0306_trigger_secret_encryption(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_emits_exactly_one_add_column(self) -> None:
        calls = [c for c in _migration_calls("upgrade") if c[0] == "add_column"]
        assert len(calls) == 1, f"expected exactly one add_column call, got {calls}"

    def test_column_shape(self) -> None:
        calls = [c for c in _migration_calls("upgrade") if c[0] == "add_column"]
        _name, args, _kwargs = calls[0]
        assert args[0] == "runs"
        column = args[1]
        assert column.name == _COLUMN_NAME
        assert isinstance(column.type, Boolean)
        assert column.nullable is False
        assert column.server_default is not None
        assert "false" in str(column.server_default.arg)


class TestDowngrade:
    def test_drops_the_column(self) -> None:
        calls = [c for c in _migration_calls("downgrade") if c[0] == "drop_column"]
        assert len(calls) == 1, f"expected exactly one drop_column call, got {calls}"
        _name, args, _kwargs = calls[0]
        assert args[0] == "runs"
        assert args[1] == _COLUMN_NAME


class TestModelParity:
    def test_model_declares_the_same_column(self) -> None:
        """Model and migration ship the column in the SAME change."""
        column = Run.__table__.columns.get(_COLUMN_NAME)
        assert column is not None, (
            f"Run must declare {_COLUMN_NAME} to stay in parity with migration 0307 — "
            "a migration/model column drift is a review block"
        )
        # NullType is the generic stand-in SQLAlchemy uses before dialect
        # resolution; the model's declared type must be Boolean.
        assert isinstance(column.type, Boolean), f"expected Boolean, got {type(column.type)!r}"
        assert column.nullable is False

    def test_server_default_is_false(self) -> None:
        column = Run.__table__.columns[_COLUMN_NAME]
        assert column.server_default is not None, "server_default false is required (NOT NULL, no backfill)"
        assert "false" in str(column.server_default.arg)
        # Guard against a NullType slip in the isinstance check above.
        assert not isinstance(column.type, NullType)
