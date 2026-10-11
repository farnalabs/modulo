"""Unit tests for migration 0294_eval_results_org_fk (FAR-969).

Structural + model-parity contract (no Postgres / Testcontainers needed):

* **Revision chain** — the revision/down_revision pin this migration onto the
  0293_oauth_clients_team_id parent, and the migrations directory has
  exactly one head (this migration), so the pre-commit ``check-migration-heads``
  hook can never be ambushed by a renumber.
* **Composite FK emission (mocked ``op``)** — on Postgres the upgrade swaps the
  single-column ``eval_results_eval_id_fkey`` for the composite
  ``fk_eval_results_eval_org`` over ``(eval_id, organisation_id)``; the
  downgrade restores the plain FK.  The ``uq_evals_id_organisation_id`` leg is
  added only when absent.
* **Non-Postgres no-op** — SQLite / ORM-created schemas rely on the model
  ``create_all``, so both entry points return before emitting DDL.
* **Model parity** — ``EvalResult`` declares the same composite FK (columns,
  target, ON DELETE CASCADE) with no standalone ``eval_id`` FK, and ``Eval``
  declares the referenced ``UNIQUE (id, organisation_id)`` — so ``create_all``'d
  schemas and migrated ones agree.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint

from modulo.db.models.eval import Eval
from modulo.db.models.eval_result import EvalResult

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0294_eval_results_org_fk"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0293_oauth_clients_team_id"

_COMPOSITE_FK = "fk_eval_results_eval_org"
_OLD_FK = "eval_results_eval_id_fkey"
_UNIQUE = "uq_evals_id_organisation_id"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_with_mocked_op(dialect: str, entry_point: str, unique_exists: bool = True) -> list[str]:
    """Run an entry point against a mocked ``op``; return emitted SQL strings."""
    module = _load_migration()
    executed: list[str] = []

    def _record_execute(stmt: object, *_args: object, **_kwargs: object) -> None:
        executed.append(str(getattr(stmt, "text", stmt)))

    with (
        patch.object(module, "op") as op,
        patch.object(module, "_constraint_exists", return_value=unique_exists),
    ):
        op.get_bind.return_value.dialect.name = dialect
        op.execute.side_effect = _record_execute
        getattr(module, entry_point)()
    return executed


class TestChain:
    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_down_revision_is_0293_oauth_clients_team_id(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_single_head_and_this_migration_is_an_ancestor(self) -> None:
        # 0294 is no longer the tip once a later migration lands on top (0295,
        # FAR-1108 chunk 8b). The invariant this test owns is that the chain
        # still has exactly ONE head and that this migration is on it.
        script = ScriptDirectory(str(_VERSIONS.parent))
        heads = script.get_heads()
        assert len(heads) == 1, f"expected a single head, got {heads}"
        revisions = {rev.revision for rev in script.walk_revisions()}
        assert _MIGRATION_NAME in revisions

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestDdlEmission:
    def test_postgres_upgrade_swaps_plain_fk_for_composite(self) -> None:
        executed = _run_with_mocked_op("postgresql", "upgrade")
        assert executed == [
            f'ALTER TABLE eval_results DROP CONSTRAINT IF EXISTS "{_OLD_FK}"',
            f'ALTER TABLE eval_results DROP CONSTRAINT IF EXISTS "{_COMPOSITE_FK}"',
            "ALTER TABLE eval_results ADD CONSTRAINT fk_eval_results_eval_org "
            "FOREIGN KEY (eval_id, organisation_id) "
            "REFERENCES evals (id, organisation_id) ON DELETE CASCADE",
        ], executed

    def test_postgres_upgrade_adds_unique_when_missing(self) -> None:
        executed = _run_with_mocked_op("postgresql", "upgrade", unique_exists=False)
        assert executed[0] == (
            "ALTER TABLE evals ADD CONSTRAINT uq_evals_id_organisation_id UNIQUE (id, organisation_id)"
        ), executed
        assert len(executed) == 4, executed

    def test_postgres_downgrade_restores_plain_fk(self) -> None:
        executed = _run_with_mocked_op("postgresql", "downgrade")
        assert executed == [
            f'ALTER TABLE eval_results DROP CONSTRAINT IF EXISTS "{_COMPOSITE_FK}"',
            f'ALTER TABLE eval_results DROP CONSTRAINT IF EXISTS "{_OLD_FK}"',
            "ALTER TABLE eval_results ADD CONSTRAINT eval_results_eval_id_fkey "
            "FOREIGN KEY (eval_id) REFERENCES evals (id) ON DELETE CASCADE",
        ], executed

    def test_non_postgres_is_a_no_op(self) -> None:
        assert not _run_with_mocked_op("sqlite", "upgrade")
        assert not _run_with_mocked_op("sqlite", "downgrade")

    def test_unique_is_never_dropped_on_downgrade(self) -> None:
        """``uq_evals_id_organisation_id`` predates this migration (0250), so
        the downgrade must leave it in place."""
        source = _MIGRATION_PATH.read_text(encoding="utf-8")
        assert "DROP CONSTRAINT" in source
        assert f'DROP CONSTRAINT IF EXISTS "{_UNIQUE}"' not in source


class TestModelParity:
    def test_eval_result_declares_the_composite_fk(self) -> None:
        named = {fk.name: fk for fk in EvalResult.__table__.foreign_key_constraints if fk.name is not None}
        assert _COMPOSITE_FK in named, f"model/migration drift: {_COMPOSITE_FK} missing from the ORM"
        fk = named[_COMPOSITE_FK]
        assert [column.name for column in fk.columns] == ["eval_id", "organisation_id"]
        assert [element.target_fullname for element in fk.elements] == [
            "evals.id",
            "evals.organisation_id",
        ]
        assert fk.ondelete == "CASCADE"

    def test_eval_id_is_covered_by_exactly_one_composite_fk(self) -> None:
        """No standalone single-column ``eval_id`` FK may coexist with the
        composite one — a single-column FK would not enforce same-org binding."""
        covering = [
            fk
            for fk in EvalResult.__table__.foreign_key_constraints
            if any(column.name == "eval_id" for column in fk.columns)
        ]
        assert len(covering) == 1, [sorted(c.name for c in fk.columns) for fk in covering]
        assert [column.name for column in covering[0].columns] == ["eval_id", "organisation_id"]

    def test_evals_declares_the_composite_unique_target(self) -> None:
        uniques = {c.name for c in Eval.__table__.constraints if isinstance(c, UniqueConstraint)}
        assert _UNIQUE in uniques, f"evals must declare {_UNIQUE} as the composite FK target"
