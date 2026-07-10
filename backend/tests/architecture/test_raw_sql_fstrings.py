"""Architecture test: no raw f-string SQL in production code.

Analogous to ArchUnit's coded-rule checks — verifies code structure
constraints that static analysis (Ruff, bandit) can miss or that are
unique to this project's SQLAlchemy + asyncpg patterns.

This is not a Semgrep duplicate: this test runs in CI via pytest
and fails the build with an explicit error message, providing a
second layer of defense beyond pre-commit hooks.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "modulo"
EXCLUDE_PREFIXES = ("modulo/db/migrations",)


def _iter_py_files(root: Path):
    for path in root.rglob("*.py"):
        rel = path.relative_to(root.parent.parent)  # relative to backend/
        if any(str(rel).startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        yield path


RAWSQL_CALLS = re.compile(
    r'text\(\s*[fF][uU]\s*"'
    r'[^"]*\{[^}]+}[^"]*"',
)


def test_no_raw_sql_fstrings():
    violations = []
    for path in _iter_py_files(SRC):
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for i, line in enumerate(content.splitlines(), 1):
            if RAWSQL_CALLS.search(line):
                violations.append(f"  {path.relative_to(SRC)}:{i}  {line.strip()[:100]}")
    assert not violations, (
        f"Found {len(violations)} raw f-string SQL text() calls (SQL injection risk).\n"
        "Use parameterized queries: text('... :param ...').bindparams(param=value)\n" + "\n".join(violations)
    )
