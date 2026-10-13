"""Unit tests for migration 0305_hitl_claims_sweep_decided_index.

Structural contract (no Postgres needed):

* **Revision chain** — 0305 revises ``0304_metrics_staging_recorded_at_index``; the
  migrations directory has exactly one head (``0305_hitl_claims_sweep_decided_index``,
  pinned by all sibling chain tests), the revision id matches the filename, and
  neither ``branch_labels`` nor ``depends_on`` is set (the pre-commit
  check-migration-heads hook and every ``test_single_head_*`` pin depend on
  that shape).
* **Index shape (mocked ``op``)** — the upgrade emits exactly one
  ``CREATE INDEX`` for ``ix_hitl_claims_sweep_decided`` on
  ``hitl_claims(organisation_id, decided_by, decision_at)`` with matching
  ``postgresql_where`` / ``sqlite_where`` partial predicates covering the
  sweep detector's population, and the downgrade drops it.
* **ORM parity** — ``HitlClaim.__table_args__`` declares the SAME index
  (name + columns), so model and migration cannot drift (the 0216/0217 +
  0303/0304 rule: a migration touching an index must update the model in
  the same change).

The migration is deliberately a PARTIAL index: the sweep detector
(``count_recent_approves``) only ever returns actor-stamped rows with a
counted decision (``approved`` / ``deliver_manual``) and a stamped
``decision_at`` — rejected rows and legacy NULL-``decided_by`` rows never
match, so baking that population into the predicate keeps the index
proportional to recent approvals instead of table history.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

from alembic.script import ScriptDirectory

from modulo.db.models.hitl_claim import HitlClaim

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0305_hitl_claims_sweep_decided_index"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0304_metrics_staging_recorded_at_index"
_CHAIN_HEAD_MIGRATION = "0305_hitl_claims_sweep_decided_index"
_INDEX_NAME = "ix_hitl_claims_sweep_decided"
_INDEX_COLUMNS = ("organisation_id", "decided_by", "decision_at")


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
    def test_single_head_is_0305_hitl_claims_sweep_decided_index(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_CHAIN_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0304_metrics_staging_recorded_at_index(self) -> None:
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
        assert args[1] == "hitl_claims"
        assert tuple(args[2]) == _INDEX_COLUMNS
        # Partial index on the detector's population, portable on both dialects.
        assert "postgresql_where" in kwargs
        assert "sqlite_where" in kwargs
        assert str(kwargs["postgresql_where"]) == str(kwargs["sqlite_where"])
        assert kwargs.get("if_not_exists") is True


class TestDowngrade:
    def test_drops_the_index(self) -> None:
        calls = [c for c in _migration_calls("downgrade") if c[0] == "drop_index"]
        assert len(calls) == 1, f"expected exactly one drop_index call, got {calls}"
        _name, args, kwargs = calls[0]
        assert args[0] == _INDEX_NAME
        assert kwargs.get("table_name") == "hitl_claims"
        assert kwargs.get("if_exists") is True


class TestModelParity:
    def test_model_declares_the_same_index(self) -> None:
        """Model and migration ship the same index in the SAME change."""
        matches = [
            index
            for index in HitlClaim.__table__.indexes
            if index.name == _INDEX_NAME and tuple(c.name for c in index.columns) == _INDEX_COLUMNS
        ]
        assert matches, (
            f"HitlClaim must declare {_INDEX_NAME}{list(_INDEX_COLUMNS)} to stay in parity with "
            "migration 0305 — a migration/model index drift is a review block"
        )

    def test_model_keeps_the_decided_by_leg_index(self) -> None:
        """The pre-existing single-column decided_by index must survive."""
        assert any(idx.name == "ix_hitl_claims_decided_by" for idx in HitlClaim.__table__.indexes), (
            "ix_hitl_claims_decided_by must remain declared on the model"
        )

    def test_model_keeps_the_run_review_unique(self) -> None:
        """The (run_id, review_id) claim identity must survive."""
        assert any(c.name == "uq_hitl_claims_run_review" for c in HitlClaim.__table__.constraints), (
            "uq_hitl_claims_run_review must remain declared on the model"
        )
