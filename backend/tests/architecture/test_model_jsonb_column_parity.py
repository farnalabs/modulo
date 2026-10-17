"""Architecture gate: the 0147-converted JSON columns that still resolve are JSONB.

SCOPE — read this before trusting the gate. This is **not** a general
"jsonb vs generic JSON" gate for the whole schema. It covers exactly the 66
``(table, column)`` pairs ``0147_json_to_jsonb_standardize`` promoted, minus the
three whose tables no longer answer to their 0147 names. A column whose physical
Postgres type is ``jsonb`` because of some **other** migration (or because it was
created ``jsonb`` from the start) is NOT checked here. At the time of writing 16
such columns are still declared bare generic ``JSON`` over a physical ``jsonb``
column; they are tracked separately (FAR-1941) and remain listed in
``tests/integration/test_initial_migration.py::_JSONB_DB_TO_JSON_ORM``. This gate
makes no claim about them and must not be read as one.

Why the 0147 columns still matter: a mapped column whose Postgres type renders
as plain ``json`` while the physical column is ``jsonb`` makes every SQL-level
predicate against it bind as ``$1::JSON`` and raise

    operator does not exist: jsonb = json

That is not hypothetical — FAR-1856 was exactly this failure on
``lifecycle_maps.content_json`` (declared bare ``JSON``, physically ``jsonb``),
and because the offending scanner tolerated per-scanner exceptions it returned
nothing in production forever while appearing healthy. FAR-1878 aligned the 0147
columns; this gate is the **regression guard for that set**, not the ticket's
closure.

The pair list is read from the migration module itself, so the gate cannot
silently drift from what 0147 actually converted — if the migration's contract
test pins a different set, this gate follows the migration, not a hand-copied
list.

Two structural invariants are enforced:

1. **Dialect parity (0147 scope only)** — every 0147 pair that still resolves
   against ``Base.metadata`` maps to a column whose ``postgresql`` dialect impl
   is ``JSONB``. Three pairs point at tables that a later migration renamed or
   dropped; each is named explicitly below, and ANY other absent table is a hard
   failure — there is no blanket skip, so a renamed table cannot hide its column.
2. **One shared definition** — the ``JSON().with_variant(JSONB(), "postgresql")``
   expression is constructed exactly once (``modulo.db.models.base.JSONB_COL``).
   Enforced over the whole backend source tree (migrations excluded) by AST, so
   a direct ``mapped_column(JSONB())`` or a qualified
   ``with_variant(postgresql.JSONB(), "postgresql")`` is caught too — both of
   which the dialect-parity check alone would accept while silently breaking
   SQLite/MariaDB parity.

All three checks are static (no database): they read the migration, the ORM
metadata and the source tree.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB

import modulo.db.models  # noqa: F401  (registers every mapper on Base.metadata)
from modulo.db.models.base import Base

_BACKEND = Path(__file__).resolve().parent.parent.parent
_SRC = _BACKEND / "src" / "modulo"
_MIGRATION_PATH = _SRC / "db" / "migrations" / "versions" / "0147_json_to_jsonb_standardize.py"
_MIGRATIONS_DIR = _SRC / "db" / "migrations"
#: The ONLY module allowed to construct a ``JSONB`` type.
_JSONB_ALIAS_PATH = _SRC / "db" / "models" / "base.py"

#: A Postgres dialect instance for type resolution. ``create_engine`` builds the
#: dialect without opening a connection, and (unlike ``postgresql.dialect()``)
#: is fully typed for mypy strict.
_POSTGRES_DIALECT = create_engine("postgresql+psycopg://").dialect

#: 0147 predates two migrations that removed three of its target pairs from
#: ``Base.metadata``. EVERY such pair must be named here — the parity test
#: hard-fails on any other absent 0147 table, so these exceptions cannot grow
#: silently and a future rename cannot hide.
#:
#: A RENAME is expressed as the pair's CURRENT location, and the column is then
#: checked THERE — so a renamed table is still covered rather than skipped.
_RENAMED_0147_PAIRS: dict[tuple[str, str], tuple[str, str]] = {
    # 0257_rename_remy_to_assistant: the converted column moved with the table.
    ("remy_skills", "triggers"): ("assistant_skills", "triggers"),
}

#: A DROP is expressed per pair with the migration that removed it. The parity
#: test additionally asserts the table stays absent, so a re-introduction (or a
#: rename that a reader mistakes for a drop) fails loudly instead of passing.
_DROPPED_0147_PAIRS: dict[tuple[str, str], str] = {
    # 0179_drop_workspace_leases
    ("workspace_leases", "resource_usage_json"): "0179_drop_workspace_leases",
    ("workspace_leases", "output_artifact_refs_json"): "0179_drop_workspace_leases",
}


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("migration_0147_json_to_jsonb_standardize", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolved_pair(pair: tuple[str, str]) -> tuple[str, str]:
    """Return the pair's current ``(table, column)`` after the 0257 rename."""
    return _RENAMED_0147_PAIRS.get(pair, pair)


def test_0147_pairs_that_still_resolve_are_jsonb_on_postgres() -> None:
    """Every mapped 0147 pair resolves to a JSONB dialect impl."""
    pairs = _load_migration()._JSON_TO_JSONB
    assert pairs, "0147 declares no (table, column) pairs — the contract moved"

    dialect = _POSTGRES_DIALECT
    violations: list[str] = []

    for pair in pairs:
        table_name, column_name = pair

        if pair in _DROPPED_0147_PAIRS:
            if table_name in Base.metadata.tables:
                violations.append(
                    f"{table_name}.{column_name}: recorded as dropped by "
                    f"{_DROPPED_0147_PAIRS[pair]} but the table is back in Base.metadata"
                )
            continue

        resolved_table, resolved_column = _resolved_pair(pair)
        table = Base.metadata.tables.get(resolved_table)
        if table is None:
            violations.append(
                f"{table_name}.{column_name}: table {resolved_table!r} is absent from "
                "Base.metadata and the pair is not justified in _DROPPED_0147_PAIRS — a "
                "renamed or dropped 0147 table must be named explicitly, never skipped"
            )
            continue

        column = table.columns.get(resolved_column)
        if column is None:
            violations.append(f"{resolved_table}.{resolved_column}: column missing from the mapped table")
            continue

        dialect_impl = column.type.dialect_impl(dialect)
        if not isinstance(dialect_impl, JSONB):
            violations.append(
                f"{resolved_table}.{resolved_column}: declared {type(column.type).__name__}, "
                f"renders as {type(dialect_impl).__name__} on postgresql — the physical "
                "column is JSONB after 0147"
            )

    assert not violations, (
        "0147-converted columns whose ORM declaration disagrees with the physical jsonb "
        "column (a SQL predicate against these binds as ::JSON and raises "
        "'operator does not exist: jsonb = json'):\n  " + "\n  ".join(violations)
    )


def test_0147_pair_resolution_is_complete_and_justified() -> None:
    """Every absent 0147 pair is justified, and no justification is stale."""
    pairs = set(_load_migration()._JSON_TO_JSONB)
    justified = set(_RENAMED_0147_PAIRS) | set(_DROPPED_0147_PAIRS)

    stale = sorted(justified - pairs)
    assert not stale, (
        "_RENAMED_0147_PAIRS/_DROPPED_0147_PAIRS name pairs 0147 does not convert "
        f"(a stale exception would hide whatever it still claims): {stale}"
    )

    unmapped_absent = sorted(
        pair for pair in pairs if pair not in justified and _resolved_pair(pair)[0] not in Base.metadata.tables
    )
    assert not unmapped_absent, (
        "0147 pairs whose table is absent from Base.metadata and are not justified in "
        f"_RENAMED_0147_PAIRS/_DROPPED_0147_PAIRS: {unmapped_absent}"
    )

    for renamed_from, destination in _RENAMED_0147_PAIRS.items():
        assert destination[0] in Base.metadata.tables, (
            f"_RENAMED_0147_PAIRS maps {renamed_from} to {destination}, but {destination[0]!r} "
            "is not mapped in Base.metadata"
        )


def _jsonb_construction_lines(path: Path) -> list[int]:
    """Line numbers of every ``JSONB(...)`` Call in *path* (AST, not text).

    AST rather than a regex: prose that merely mentions ``JSONB (`` must not
    register, while every call shape — bare ``JSONB()``,
    ``postgresql.JSONB()``, ``JSON().with_variant(JSONB(), ...)`` — must.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name == "JSONB":
            lines.append(node.lineno)
    return lines


def test_jsonb_is_only_constructed_in_the_shared_alias() -> None:
    """A ``JSONB(...)`` call may appear only in the shared alias (``base.py``).

    Migrations are excluded (they legitimately issue jsonb DDL). Anywhere else —
    a model, a Core ``Table``, a CRUD module — a locally constructed ``JSONB()``
    both re-fragments the single-definition rule and breaks SQLite/MariaDB parity,
    and the dialect-parity check alone cannot catch it (a direct ``JSONB()`` has
    a JSONB dialect impl too).
    """
    offenders: dict[str, list[int]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        if _MIGRATIONS_DIR in path.parents:
            continue
        lines = _jsonb_construction_lines(path)
        if lines:
            offenders[path.relative_to(_BACKEND).as_posix()] = lines

    alias_key = _JSONB_ALIAS_PATH.relative_to(_BACKEND).as_posix()
    assert alias_key in offenders, (
        f"{alias_key} must construct the shared JSONB variant (JSONB_COL) — removing it "
        "would leave every 0147 column undeclared"
    )
    extra = {path: lines for path, lines in offenders.items() if path != alias_key}
    assert not extra, (
        "A JSONB() call outside the shared alias (modulo.db.models.base.JSONB_COL). "
        f"Import JSONB_COL instead of constructing JSONB locally: {extra}"
    )
