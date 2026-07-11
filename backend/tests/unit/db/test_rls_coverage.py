"""Architecture test: every org-scoped table must have an RLS policy migration.

Migration 0002 enabled row-level security on the org-scoped tables that existed
at the time. Subsequent tables were repeatedly added without a matching
``ENABLE ROW LEVEL SECURITY`` migration, silently opening tenant-isolation gaps
(a 2026-07-09 security review found ~14 uncovered tables). This test collects
every table that an RLS-enabling migration touches and asserts that set covers
every ORM table carrying an ``organisation_id`` column.

A new org-scoped model added without a corresponding RLS migration will fail
this test.

Two migration styles are supported when collecting covered tables:

1. Literal DDL — ``op.execute("ALTER TABLE foo ENABLE ROW LEVEL SECURITY")``
   (e.g. 0045_saved_views). Found by regex over the source.
2. Loop over a table tuple — ``for t in _ORG_SCOPED_TABLES: ALTER TABLE "{t}"``
   (e.g. 0002_rls_policies, 0088_rls_missing_policies). The table names live in
   module-level tuple/list constants, so we import each RLS-enabling migration
   module and read those constants. Reading the actual code constants (not the
   docstring prose) means removing a table from the tuple correctly makes this
   test fail.
"""

import importlib.util
import re
from pathlib import Path

from modulo.db.models import Base

# The root tenant entity is intentionally never row-level-secured: it is read
# before an org context is established. This is the ONLY permitted exclusion.
_EXCLUDED_TABLES = frozenset({"organisations"})

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

_RLS_ENABLE_MARKER = "ENABLE ROW LEVEL SECURITY"

# Matches a literal DDL enable, e.g. ALTER TABLE "foo" ENABLE ROW LEVEL SECURITY.
# The loop-based migrations use an f-string placeholder ("{table}") that this
# regex intentionally does not match — those are handled via constant import.
_RLS_ENABLE_RE = re.compile(
    r'ALTER\s+TABLE\s+"?(?P<table>[a-z_][a-z0-9_]*)"?\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY',
    re.IGNORECASE,
)


def _org_scoped_orm_tables() -> set[str]:
    """Every ORM table with an ``organisation_id`` column, minus exclusions."""
    return {
        name
        for name, table in Base.metadata.tables.items()
        if "organisation_id" in table.columns and name not in _EXCLUDED_TABLES
    }


def _load_migration_module(path: Path) -> object:
    spec = importlib.util.spec_from_file_location(f"_rls_mig_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tables_with_rls_migration() -> set[str]:
    """Every table covered by an ``ENABLE ROW LEVEL SECURITY`` migration."""
    covered: set[str] = set()
    for path in _MIGRATIONS_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _RLS_ENABLE_MARKER.upper() not in text.upper():
            continue
        # Style 1: literal ALTER TABLE statements.
        covered.update(m.group("table") for m in _RLS_ENABLE_RE.finditer(text))
        # Style 2: table names held in module-level tuple/list constants.
        module = _load_migration_module(path)
        for value in vars(module).values():
            if isinstance(value, tuple | list) and all(isinstance(x, str) for x in value):
                covered.update(value)
    return covered


def test_migrations_dir_exists() -> None:
    assert _MIGRATIONS_DIR.is_dir(), f"migrations dir not found: {_MIGRATIONS_DIR}"


def test_every_org_scoped_table_has_rls_policy() -> None:
    org_scoped = _org_scoped_orm_tables()
    covered = _tables_with_rls_migration()

    missing = org_scoped - covered
    assert not missing, (
        "Org-scoped tables missing an RLS `ENABLE ROW LEVEL SECURITY` migration: "
        f"{sorted(missing)}. Add ENABLE + `CREATE POLICY rls_org_isolation` for each "
        "in a new Alembic migration (see 0002_rls_policies.py / 0088_rls_missing_policies.py)."
    )


def test_organisations_table_is_the_only_exclusion() -> None:
    # Guards against silently expanding the exclusion set. If a new table is
    # legitimately unpoliced, this test (and its reviewers) must be updated
    # deliberately.
    assert sorted(_EXCLUDED_TABLES) == ["organisations"]
