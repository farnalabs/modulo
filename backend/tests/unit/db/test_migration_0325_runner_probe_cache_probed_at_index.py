"""Unit tests for migration 0325_runner_probe_cache_probed_at_index (FAR-591 / qa F8).

Structural contract (no Postgres needed):

* **Revision chain** — 0325 revises ``0324_purge_eval_definitions``; the
  graph's single, linear head is asserted by the shared
  ``_migration_graph.assert_single_linear_head`` helper (never a hard-coded head
  value), the revision id matches the filename, and neither ``branch_labels``
  nor ``depends_on`` is set (the pre-commit check-migration-heads hook and this
  file's ``test_no_branch_labels_or_depends_on`` depend on that shape).
* **Index shape (mocked ``op``)** — the upgrade emits exactly one
  ``CREATE INDEX`` for ``ix_runner_probe_cache_probed_at`` on
  ``runner_probe_cache(probed_at)``, and the downgrade drops it.
* **ORM parity** — ``RunnerProbeCache.__table_args__`` declares the SAME index
  (name + columns), so model and migration cannot drift (the database.md rule:
  a migration touching an index must update the model in the same change).

The migration is deliberately a PLAIN index, not a partial one: the retention
sweep it serves runs ``WHERE probed_at < :cutoff`` with a moving bind parameter,
and Postgres requires index predicates to be IMMUTABLE — ``now()`` is STABLE and
a bind parameter is not an expression at all, so the "partial index on the exact
predicate" shape cannot express this sweep (the column is already ``NOT NULL``,
so an immutable ``WHERE probed_at IS NOT NULL`` partial would add nothing).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

from modulo.db.models.runner_probe_cache import RunnerProbeCache
from tests.unit.db._migration_graph import assert_single_linear_head, script_directory

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0325_runner_probe_cache_probed_at_index"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0324_purge_eval_definitions"
_INDEX_NAME = "ix_runner_probe_cache_probed_at"
_TABLE = "runner_probe_cache"


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
    for name in ("create_index", "drop_index"):
        getattr(mock_op, name).side_effect = _record(name)

    with patch.object(module, "op", mock_op):
        getattr(module, entry_point)()
    return calls


class TestChain:
    def test_single_linear_head(self) -> None:
        assert_single_linear_head(script_directory())

    def test_down_revision_is_0324_purge_eval_definitions(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_emits_exactly_one_create_index(self) -> None:
        calls = [c for c in _migration_calls("upgrade") if c[0] == "create_index"]
        assert len(calls) == 1, f"expected exactly one create_index call, got {calls}"

    def test_index_shape(self) -> None:
        calls = [c for c in _migration_calls("upgrade") if c[0] == "create_index"]
        _name, args, kwargs = calls[0]
        # create_index(name, table_name, *columns)
        assert args[0] == _INDEX_NAME
        assert args[1] == _TABLE
        assert tuple(args[2]) == ("probed_at",)
        # Plain index: a moving-cutoff sweep predicate cannot be partial
        # (Postgres requires an IMMUTABLE index predicate).
        assert "postgresql_where" not in kwargs
        assert kwargs.get("if_not_exists") is True


class TestDowngrade:
    def test_drops_the_index(self) -> None:
        calls = [c for c in _migration_calls("downgrade") if c[0] == "drop_index"]
        assert len(calls) == 1, f"expected exactly one drop_index call, got {calls}"
        _name, args, kwargs = calls[0]
        assert args[0] == _INDEX_NAME
        assert kwargs.get("table_name") == _TABLE
        assert kwargs.get("if_exists") is True


class TestModelParity:
    def test_model_declares_the_same_index(self) -> None:
        """Model and migration ship the same index in the SAME change."""
        matches = [
            index
            for index in RunnerProbeCache.__table__.indexes
            if index.name == _INDEX_NAME and tuple(c.name for c in index.columns) == ("probed_at",)
        ]
        assert matches, (
            f"RunnerProbeCache must declare {_INDEX_NAME}(probed_at) to stay in parity with "
            "migration 0325 — a migration/model index drift is a review block"
        )

    def test_model_keeps_the_org_machine_uniqueness(self) -> None:
        """The (organisation_id, machine_id) unique key the upsert relies on survives."""
        unique = [
            constraint
            for constraint in RunnerProbeCache.__table__.constraints
            if constraint.name == "uq_runner_probe_cache_org_machine"
        ]
        assert unique, "uq_runner_probe_cache_org_machine must remain declared on the model"
