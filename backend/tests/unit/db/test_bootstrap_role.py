"""Unit tests for db bootstrap_role helpers — pure functions only, no DB.

The connection helpers are exercised here so a broken DSN transform is caught
without requiring a live Postgres.
"""

import pytest

from modulo.db.bootstrap_role import _asyncpg_admin_connect


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+asyncpg://admin:pw@db:5432/modulo",
            ("postgres://admin:pw@db:5432/modulo", False),
        ),
        (
            "postgres://admin:pw@db/modulo",
            ("postgres://admin:pw@db/modulo", False),
        ),
        (
            "postgres://u:p@h/db?sslmode=require",
            ("postgres://u:p@h/db", "require"),
        ),
        (
            "postgres://u:p@h/db?sslmode=verify-ca",
            ("postgres://u:p@h/db", "verify-ca"),
        ),
        (
            "postgres://u:p@h/db?sslmode=verify-full",
            ("postgres://u:p@h/db", "verify-full"),
        ),
        (
            "postgres://u:p@h/db?sslmode=disable",
            ("postgres://u:p@h/db", False),
        ),
        (
            "postgres://u:p@h/db?sslmode=prefer",
            ("postgres://u:p@h/db", False),
        ),
        (
            "postgres://u:p@h/db?sslmode=require&application_name=modulo",
            ("postgres://u:p@h/db", "require"),
        ),
        (
            "postgres://u:p@h:5433/db?sslmode=require",
            ("postgres://u:p@h:5433/db", "require"),
        ),
    ],
)
def test_asyncpg_admin_connect(url: str, expected: tuple[str, bool | str]) -> None:
    assert _asyncpg_admin_connect(url) == expected
