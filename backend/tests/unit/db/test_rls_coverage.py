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

# Matches a literal DDL enable, e.g. ALTER TABLE "foo" ENABLE ROW LEVEL SECURITY
# or ALTER TABLE public.foo ENABLE ROW LEVEL SECURITY (the reconciliation chain
# prefixes the schema). The loop-based migrations use an f-string placeholder
# ("{table}") that this regex intentionally does not match — those are handled
# via constant import.
_RLS_ENABLE_RE = re.compile(
    r'ALTER\s+TABLE\s+(?:public\.)?"?(?P<table>[a-z_][a-z0-9_]*)"?\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY',
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
    assert spec is not None
    assert spec.loader is not None
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


# ---------------------------------------------------------------------------
# Fail-closed RLS regression guard for migrations 0155 / 0156
# ---------------------------------------------------------------------------

# The null-safe strict scope that migrations 0155/0156 re-created the
# rls_org_isolation policy with — a null/empty app.organisation_id yields NULL
# which matches no row (fail-closed), instead of the old fail-open OR-branch.
_STRICT_SCOPE_MARKER = "nullif(current_setting('app.organisation_id', true), '')"

# Any of these substrings indicates a fail-open OR-branch on the rls_org_isolation
# policy: `(... IS NULL) OR ...` or `... OR (nullif(...) IS NULL)` lets a caller
# with no org context read every row.
_FAIL_OPEN_MARKERS = (
    "IS NULL OR",
    "OR (nullif",
    "OR (current_setting",
)

# Migration file -> tables it must tighten to fail-closed.
_STRICT_MIGRATIONS: dict[str, tuple[str, ...]] = {
    "0162_rls_strict_parameter_schemas_sets": ("parameter_schemas", "parameter_sets"),
    "0163_rls_strict_oauth_auth_codes_token_families": (
        "oauth_authorization_codes",
        "oauth_token_families",
    ),
}

# Matches the scope variable an upgrade CREATE POLICY actually binds, e.g.
#   CREATE POLICY rls_org_isolation ON public.{table} USING ({_STRICT_SCOPE})
_CREATE_POLICY_RE = re.compile(
    r"CREATE POLICY rls_org_isolation ON public\.\{_?\w+\} USING \(\{(_?\w+)\}\)",
)

# Captures the whole (possibly multi-line, implicitly-concatenated) assignment
# block for a top-level scope variable, e.g.
#   _FAIL_OPEN_SCOPE = (
#       "(organisation_id = ...) "
#       "OR (nullif(...) IS NULL)"
#   )
_SCOPE_BLOCK_RE = re.compile(r"(\w+)\s*=((?s:.*?)(?=\n(?!\s)|\Z))")


def _scope_block(text: str, var_name: str) -> str:
    for name, block in _SCOPE_BLOCK_RE.findall(text):
        if name == var_name:
            return block
    raise AssertionError(f"could not find scope variable {var_name!r} definition")


def test_strict_rls_migrations_close_fail_open_branch() -> None:
    """Migrations 0155/0156 must close the fail-open rls_org_isolation OR-branch.

    Regression guard: the prior policy on these four tables OR'd the strict
    org scope with a null-context branch, so any caller without an
    ``app.organisation_id`` setting could read every row (a silent
    cross-tenant fail-open). The migrations were tightened to a null-safe
    strict scope that fails CLOSED.

    We assert that the rls_org_isolation CREATE POLICY in these migrations is
    bound to a scope literal that is the null-safe strict form (fails closed)
    and contains NO fail-open OR-branch. The upgrade path must also not create
    the stacked permissive ``rls_org_isolation_null_context`` policy and must
    contain no fail-open OR-branch text. The downgrade leg intentionally
    restores the prior fail-open form, so its scope literal is allowed to be
    fail-open — but the upgrade (strict) scope must not be.

    Without this guard a future revert to the fail-open form (or a missed
    ``set_rls_org`` call site) would re-open these tables with no test
    tripping.
    """
    for filename, tables in _STRICT_MIGRATIONS.items():
        path = _MIGRATIONS_DIR / f"{filename}.py"
        assert path.is_file(), f"strict RLS migration missing: {path}"

        text = path.read_text(encoding="utf-8")
        for table in tables:
            assert table in text, f"{filename}: expected target table {table!r} not referenced"

        # Every scope variable bound to an rls_org_isolation CREATE POLICY, and
        # the subset that are the null-safe strict (fail-closed) form.
        policy_scopes = set(_CREATE_POLICY_RE.findall(text))
        assert policy_scopes, f"{filename}: no rls_org_isolation CREATE POLICY found"

        def _is_strict(source: str, var: str) -> bool:
            block = _scope_block(source, var)
            return _STRICT_SCOPE_MARKER in block and not any(m in block for m in _FAIL_OPEN_MARKERS)

        strict_scopes = {v for v in policy_scopes if _is_strict(text, v)}
        assert strict_scopes, (
            f"{filename}: no strict (fail-closed) rls_org_isolation scope found — "
            "the fail-open OR-branch may have been reintroduced"
        )

        # Isolate the upgrade() body from the module-level constants/helpers
        # (the downgrade's fail-open scope is reused only there).
        upgrade_src = text.split("def upgrade(", 1)[1].split("def downgrade", 1)[0]
        assert "rls_org_isolation_null_context" not in upgrade_src, (
            f"{filename}: upgrade must not create the fail-open rls_org_isolation_null_context policy"
        )
        for marker in _FAIL_OPEN_MARKERS:
            assert marker not in upgrade_src, f"{filename}: upgrade still contains a fail-open OR branch ({marker!r})"
