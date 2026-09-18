"""Document the intent of the ``migration-fstring-sql`` semgrep rule.

The rule lives at ``.semgrep/migration-fstring-sql.yml`` and flags f-string
SQL passed to ``op.execute``/``<cmd>.execute`` inside Alembic migrations
(FAR-915 / GitHub #133, preventive — no injection existed in the files at
the time of writing).

Migrations through revision 0247 are historical artifacts (shipped long
before the rule) and are exempt via the rule's ``paths.exclude`` glob window;
NEW migrations are NOT exempt and every interpolated value in them must be
guarded by a ``_validate_identifier``-style helper before interpolation.

These tests exercise the rule's matching logic directly (semgrep-core cannot
run on Windows — see the semgrep lesson in AGENTS.md): the path-filter
boundary via ``wcmatch`` (the library semgrep uses for path filtering) and
the ``pattern-regex`` predicates via Python's ``re``. The line-level regexes
were additionally verified against a real semgrep run (docker, semgrep
1.176.1): 3 findings on a probe migration with f-string SQL, 0 findings on
the untouched historical tree.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from wcmatch import glob

REPO_ROOT = Path(__file__).parents[4]
RULE_FILE = REPO_ROOT / ".semgrep" / "migration-fstring-sql.yml"
VERSIONS_DIR = REPO_ROOT / "backend" / "src" / "modulo" / "db" / "migrations" / "versions"

# The frozen exclude cutoff baked into the rule's path filters: migrations
# through 0247 shipped before the rule existed and are its only exemptions.
_EXEMPT_MAX_REVISION = 247


def _revision_number(filename: str) -> int | None:
    """Extract the 4-digit revision prefix from a migration filename.

    Returns the integer revision for names like ``0248_probe.py`` and
    ``None`` for non-migration files (``__init__.py``, helpers, short
    names, etc.).
    """
    prefix = filename[:4]
    return int(prefix) if prefix.isdigit() else None


def _rule() -> dict[str, Any]:
    data = yaml.safe_load(RULE_FILE.read_text(encoding="utf-8"))
    return data["rules"][0]


def _is_excluded(rel_path: str) -> bool:
    """Mirror semgrep's path-filter semantics: exclude wins over include."""
    rule = _rule()
    return any(glob.globmatch(rel_path, pattern, flags=glob.GLOBSTAR) for pattern in rule["paths"]["exclude"])


def _is_included(rel_path: str) -> bool:
    rule = _rule()
    return any(glob.globmatch(rel_path, pattern, flags=glob.GLOBSTAR) for pattern in rule["paths"]["include"])


def _exempt_window_violation(path: Path) -> str | None:
    """Return a violation message when an at-or-below-cutoff migration path is not exempt, else None (skip).

    This is the per-file decision extracted from the loop in
    ``test_every_historical_migration_is_exempt`` so it can be unit-tested
    directly with synthetic paths (no filesystem needed — ``Path.relative_to``
    works on non-existent paths).
    """
    revision = _revision_number(path.name)
    if revision is None or revision > _EXEMPT_MAX_REVISION:
        return None
    rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    if not _is_excluded(rel):
        return f"{path.name} (revision {revision:04d}) is not covered by the exclude cutoff"
    return None


class TestMigrationFStringRulePaths:
    def test_every_historical_migration_is_exempt(self) -> None:
        """All existing files at-or-below the rule's frozen cutoff are exempts.

        A failure here means the exclude globs drifted (they must match every
        file in the 0000-0247 window) — fix the globs, never an exempt-listed
        migration.
        """
        rule = _rule()
        assert rule["id"] == "migration-fstring-sql"
        for path in sorted(VERSIONS_DIR.glob("*.py")):
            assert _exempt_window_violation(path) is None, f"{path.name} should be exempt"

    def test_a_new_migration_is_not_exempt_but_in_scope(self) -> None:
        """The window ends at the frozen cutoff: the next revision is scanned."""
        rel = "backend/src/modulo/db/migrations/versions/0999_probe_next.py"
        assert not _is_excluded(rel)
        assert _is_included(rel)

    def test_exclude_globs_match_wcmatch_bracket_semantics(self) -> None:
        """The bracket globs must span the full exempt window: 0000-0239 and 0240-0247."""
        for rev in (1, 13, 99, 131, 199, 200, 239):
            rel = f"backend/src/modulo/db/migrations/versions/{rev:04d}_x.py"
            assert _is_excluded(rel), f"revision {rev:04d} should be exempt"
        for rev in (240, 247):
            rel = f"backend/src/modulo/db/migrations/versions/{rev:04d}_x.py"
            assert _is_excluded(rel), f"revision {rev:04d} should be exempt"
        rel = "backend/src/modulo/db/migrations/versions/0248_x.py"
        assert not _is_excluded(rel), "revision 0248 must NOT be exempt"


class TestExemptWindowViolation:
    """Tests for the _exempt_window_violation helper (FAR-920)."""

    def test_non_migration_name_skips_without_error(self) -> None:
        """A non-migration path (revision=None) skips — returns None."""
        assert _exempt_window_violation(VERSIONS_DIR / "__init__.py") is None

    def test_historical_migration_is_exempt(self) -> None:
        """A migration at-or-below the cutoff is exempt — returns None."""
        assert _exempt_window_violation(VERSIONS_DIR / "0001_initial.py") is None

    def test_post_cutoff_non_excluded_path_violates(self) -> None:
        """A migration within the cutoff window but not matched by the exclude globs fires a violation.

        ``0100probe.py`` extracts revision 100 (within the 0-247 window) but
        lacks the underscore separator required by the ``01??_*.py`` glob, so
        ``_is_excluded`` returns False and the violation path fires.
        """
        result = _exempt_window_violation(VERSIONS_DIR / "0100probe.py")
        assert result is not None
        assert "0100probe.py" in result


class TestRevisionNumber:
    """Regression tests for the _revision_number helper (FAR-920)."""

    def test_returns_none_for_init_file(self) -> None:
        assert _revision_number("__init__.py") is None

    def test_returns_none_for_helpers_file(self) -> None:
        assert _revision_number("helpers.py") is None

    def test_returns_none_for_short_name(self) -> None:
        assert _revision_number("x.py") is None

    def test_returns_int_for_valid_migration(self) -> None:
        assert _revision_number("0248_probe.py") == 248

    def test_returns_int_for_zero_revision(self) -> None:
        assert _revision_number("0000_initial.py") == 0


# probing migration content used for the regex predicates
_DIRTY_LINES = {
    2: '    op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({cols})"))',
    3: '    op.execute(f"DROP INDEX IF EXISTS {index_name}")',
    4: '    conn.execute(sqlalchemy.text(f"CREATE INDEX {idx} ON {tbl} (c)"))',
}
_CLEAN_LINE = '    op.execute(sa.text("CREATE INDEX IF NOT EXISTS uses_params ON scan(col)"))'


class TestMigrationFStringRuleRegex:
    def _match_any(self, line: str) -> bool:
        rule = _rule()
        return any(re.search(entry["pattern-regex"], line) for entry in rule["pattern-either"])

    def test_fires_on_fstring_text_sql(self) -> None:
        assert _DIRTY_LINES[2], "fixture must carry an f-string sa.text() execute"
        assert self._match_any(_DIRTY_LINES[2])

    def test_fires_on_bare_fstring_execute(self) -> None:
        assert self._match_any(_DIRTY_LINES[3])

    def test_fires_on_module_qualified_text_execute(self) -> None:
        assert self._match_any(_DIRTY_LINES[4])

    def test_ignores_plain_string_execute(self) -> None:
        assert not self._match_any(_CLEAN_LINE)
