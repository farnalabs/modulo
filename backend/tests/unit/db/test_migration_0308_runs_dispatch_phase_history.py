"""Unit tests for migration 0308_runs_dispatch_phase_history (FAR-1714).

Structural contract (no Postgres needed):

* **Revision chain** — 0307 revises ``0307_runs_guardrail_override_resume_pending``;
  the migrations directory has exactly one head (``0308_runs_dispatch_phase_history``,
  pinned by all sibling chain tests), the revision id matches the filename, and
  neither ``branch_labels`` nor ``depends_on`` is set (the pre-commit
  check-migration-heads hook and every ``test_single_head_*`` pin depend on
  that shape).
* **Column shape (mocked ``op``)** — the upgrade emits EXACTLY two
  ``add_column`` calls on ``runs``: ``dispatch_phase_history`` (nullable,
  JSON on the generic dialect / JSONB compiled on Postgres) and
  ``dispatch_phase_write_drops`` (NOT NULL, server default ``0``); the
  downgrade drops exactly those two columns.
* **ORM parity** — ``Run.__table__`` maps BOTH columns with matching
  nullability, default and type (the database.md rule: a migration must
  update the model in the SAME change, in both directions).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql

from modulo.db.models.run import Run

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0308_runs_dispatch_phase_history"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0307_runs_guardrail_override_resume_pending"
_CHAIN_HEAD_MIGRATION = "0308_runs_dispatch_phase_history"
_HISTORY_COLUMN = "dispatch_phase_history"
_DROPS_COLUMN = "dispatch_phase_write_drops"


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
    def test_single_head_is_0308_runs_dispatch_phase_history(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_CHAIN_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0307_runs_guardrail_override_resume_pending(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_emits_exactly_two_add_columns_on_runs(self) -> None:
        calls = [c for c in _migration_calls("upgrade") if c[0] == "add_column"]
        assert len(calls) == 2, f"expected exactly two add_column calls, got {calls}"
        for _name, args, _kwargs in calls:
            assert args[0] == "runs"

    def test_history_column_shape(self) -> None:
        calls = [c for c in _migration_calls("upgrade") if c[0] == "add_column"]
        columns = {args[1].name: args[1] for _name, args, _kwargs in calls}
        col = columns[_HISTORY_COLUMN]
        # Nullable: pre-0308 rows simply have no timeline.
        assert col.nullable is True
        # Multi-backend convention: generic JSON, JSONB compiled on Postgres.
        assert isinstance(col.type, sa.JSON)
        assert col.type.compile(dialect=postgresql.dialect()) == "JSONB"

    def test_drop_counter_column_shape(self) -> None:
        calls = [c for c in _migration_calls("upgrade") if c[0] == "add_column"]
        columns = {args[1].name: args[1] for _name, args, _kwargs in calls}
        col = columns[_DROPS_COLUMN]
        # NOT NULL DEFAULT 0: pre-0308 rows never recorded a drop.
        assert col.nullable is False
        assert col.server_default is not None
        assert str(col.server_default.arg) == "0"
        assert isinstance(col.type, sa.Integer)


class TestDowngrade:
    def test_drops_exactly_the_two_columns(self) -> None:
        calls = [c for c in _migration_calls("downgrade") if c[0] == "drop_column"]
        assert len(calls) == 2, f"expected exactly two drop_column calls, got {calls}"
        dropped = [args[1] for _name, args, _kwargs in calls]
        assert dropped == [_DROPS_COLUMN, _HISTORY_COLUMN]


class TestModelParity:
    def test_model_maps_both_columns(self) -> None:
        """Model and migration ship both columns in the SAME change."""
        columns = {c.name: c for c in Run.__table__.columns}
        assert _HISTORY_COLUMN in columns, "Run must map dispatch_phase_history (migration 0308 parity)"
        assert _DROPS_COLUMN in columns, "Run must map dispatch_phase_write_drops (migration 0308 parity)"

    def test_model_history_column_matches_migration(self) -> None:
        col = Run.__table__.columns[_HISTORY_COLUMN]
        assert col.nullable is True
        assert isinstance(col.type, sa.JSON)
        # Same variant pair as the migration: JSONB on Postgres, JSON elsewhere.
        assert col.type.compile(dialect=postgresql.dialect()) == "JSONB"

    def test_model_drop_counter_matches_migration(self) -> None:
        col = Run.__table__.columns[_DROPS_COLUMN]
        assert col.nullable is False
        assert isinstance(col.type, sa.Integer)
        assert col.server_default is not None
        assert str(col.server_default.arg) == "0"
