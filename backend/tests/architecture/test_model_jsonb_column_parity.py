"""Architecture gate: every 0147-converted JSON column is declared JSONB on Postgres.

Migration ``0147_json_to_jsonb_standardize`` promotes 66 ``(table, column)``
pairs from ``json`` to ``jsonb`` **in the database**. The SQLAlchemy models must
agree: a mapped column whose Postgres type renders as plain ``json`` while the
physical column is ``jsonb`` makes every SQL-level predicate against it bind as
``$1::JSON`` and raise

    operator does not exist: jsonb = json

That is not hypothetical — FAR-1856 was exactly this failure on
``lifecycle_maps.content_json`` (declared bare ``JSON``, physically ``jsonb``),
and because the offending scanner tolerated per-scanner exceptions it returned
nothing in production forever while appearing healthy. FAR-1878 aligned the
remaining columns; this gate is the permanent, cheap version of that ticket.

The pair list is read from the migration module itself, so the gate cannot
silently drift from what 0147 actually converted — if the migration's contract
test (``tests/unit/db/test_migration_0147_json_to_jsonb_standardize.py``) pins a
different set, this gate follows the migration, not a hand-copied list.

Two structural invariants are enforced:

1. **Dialect parity** — every 0147 pair that still resolves against
   ``Base.metadata`` maps to a column whose ``postgresql`` dialect impl is
   ``JSONB``. Tables 0147 converted that a later migration renamed or dropped
   are declared explicitly below; a stale entry in either map is itself a
   failure, so the maps cannot rot.
2. **One shared definition** — the ``JSON().with_variant(JSONB(), "postgresql")``
   expression is defined exactly once (``modulo.db.models.base.JSONB_COL``).
   Inline copies re-fragment the invariant and were the root cause of the
   original inconsistency (six module-private ``_JSONB_COL`` copies plus ~20
   inline forms).

Both tests are static (no database): they read the migration, the ORM metadata
and the source tree.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB

import modulo.db.models  # noqa: F401  (registers every mapper on Base.metadata)
from modulo.db.models.base import Base

_BACKEND = Path(__file__).resolve().parent.parent.parent
_SRC = _BACKEND / "src" / "modulo"
_MIGRATION_PATH = _SRC / "db" / "migrations" / "versions" / "0147_json_to_jsonb_standardize.py"

#: A Postgres dialect instance for type resolution. ``create_engine`` builds the
#: dialect without opening a connection, and (unlike ``postgresql.dialect()``)
#: is fully typed for mypy strict.
_POSTGRES_DIALECT = create_engine("postgresql+psycopg://").dialect

#: 0147 predates migrations that renamed or dropped two of its target tables.
#: ``remy_skills`` -> ``assistant_skills`` (0257_rename_remy_to_assistant) — the
#: converted column moved with the table, so the pair is checked there.
_TABLE_RENAMES: dict[str, str] = {"remy_skills": "assistant_skills"}

#: ``workspace_leases`` was dropped entirely by 0179_drop_workspace_leases, so
#: its two converted columns no longer exist in the ORM metadata. The table must
#: stay absent: if it is ever re-introduced, this map is stale and the gate must
#: be revisited rather than silently skipping the pair.
_DROPPED_TABLES: frozenset[str] = frozenset({"workspace_leases"})


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("migration_0147_json_to_jsonb_standardize", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_0147_converted_column_is_jsonb_on_postgres() -> None:
    """Every mapped 0147 pair resolves to a JSONB dialect impl."""
    pairs = _load_migration()._JSON_TO_JSONB
    assert pairs, "0147 declares no (table, column) pairs — the contract moved"

    dialect = _POSTGRES_DIALECT
    violations: list[str] = []

    for table_name, column_name in pairs:
        if table_name in _DROPPED_TABLES:
            if table_name in Base.metadata.tables:
                violations.append(
                    f"{table_name}.{column_name}: listed in _DROPPED_TABLES but the table "
                    "is back in Base.metadata — the drop map is stale"
                )
            continue

        resolved_table = _TABLE_RENAMES.get(table_name, table_name)
        table = Base.metadata.tables.get(resolved_table)
        if table is None:
            violations.append(
                f"{table_name}.{column_name}: table {resolved_table!r} is not mapped in "
                "Base.metadata and is not covered by _TABLE_RENAMES/_DROPPED_TABLES"
            )
            continue

        column = table.columns.get(column_name)
        if column is None:
            violations.append(f"{resolved_table}.{column_name}: column missing from the mapped table")
            continue

        dialect_impl = column.type.dialect_impl(dialect)
        if not isinstance(dialect_impl, JSONB):
            violations.append(
                f"{resolved_table}.{column_name}: declared {type(column.type).__name__}, "
                f"renders as {type(dialect_impl).__name__} on postgresql — the physical "
                "column is JSONB after 0147"
            )

    assert not violations, (
        "0147-converted columns whose ORM declaration disagrees with the physical jsonb "
        "column (a SQL predicate against these binds as ::JSON and raises "
        "'operator does not exist: jsonb = json'):\n  " + "\n  ".join(violations)
    )


def test_rename_and_drop_maps_only_cover_0147_pairs() -> None:
    """The rename/drop maps cannot reference a pair 0147 does not convert."""
    pairs = set(_load_migration()._JSON_TO_JSONB)
    tables_in_pairs = {table for table, _ in pairs}

    stale = sorted((set(_TABLE_RENAMES) | set(_DROPPED_TABLES)) - tables_in_pairs)
    assert not stale, f"_TABLE_RENAMES/_DROPPED_TABLES reference tables absent from 0147's pair list: {stale}"


def test_jsonb_variant_expression_is_defined_once() -> None:
    """The JSONB-variant expression lives only in ``db/models/base.py``."""
    allowed = _SRC / "db" / "models" / "base.py"
    pattern = re.compile(r"with_variant\(\s*JSONB\(\)")

    definitions: list[Path] = []
    for directory in (_SRC / "db" / "models", _SRC / "db" / "crud"):
        for path in directory.rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                definitions.append(path)

    assert allowed in definitions, (
        f"{allowed.relative_to(_BACKEND)} must define the shared JSONB variant "
        "(JSONB_COL) — removing it would leave every 0147 column undeclared"
    )
    offenders = sorted(p.relative_to(_BACKEND).as_posix() for p in definitions if p != allowed)
    assert not offenders, (
        "The JSONB-variant expression is defined more than once. Use the shared "
        f"JSONB_COL from modulo.db.models.base instead of a local copy: {offenders}"
    )
