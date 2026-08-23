"""Unit tests for the boot-time DATABASE_URL handling in deploy/fly/bootstrap_db.py.

Covers the pure derivation helpers extracted from the container-startup script:
``fix_database_url`` (async scheme conversion + sslmode strip) and
``derive_system_database_url`` (username swap to ``modulo_system``). The boot
side effects (env writes, the alembic_version bootstrap connection, /tmp file
writes) are guarded behind ``if __name__ == "__main__":`` and not exercised here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOOTSTRAP_DB_PATH = _REPO_ROOT / "deploy" / "fly" / "bootstrap_db.py"


@pytest.fixture(scope="module")
def bootstrap_db():
    spec = importlib.util.spec_from_file_location("bootstrap_db", _BOOTSTRAP_DB_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("runtime_url", "expected"),
    [
        # password containing @ — rpartition must split on the LAST @
        (
            "postgresql+asyncpg://modulo:p@ss:word@db.internal:5432/modulo",
            "postgresql+asyncpg://modulo_system:p@ss:word@db.internal:5432/modulo",
        ),
        # password with %40 escape
        (
            "postgresql+asyncpg://modulo:p%40ss@db.internal:5432/modulo",
            "postgresql+asyncpg://modulo_system:p%40ss@db.internal:5432/modulo",
        ),
        # no password, username only
        (
            "postgresql+asyncpg://modulo@db.internal:5432/modulo",
            "postgresql+asyncpg://modulo_system@db.internal:5432/modulo",
        ),
        # query-string preserved unchanged
        (
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?connect_timeout=10",
            "postgresql+asyncpg://modulo_system:pw@db.internal:5432/modulo?connect_timeout=10",
        ),
        # no userinfo@ separator — nothing to swap, returns empty (caller skips)
        (
            "postgresql+asyncpg://db.internal:5432/modulo",
            "",
        ),
    ],
)
def test_derive_system_database_url_swaps_username(
    bootstrap_db,
    runtime_url: str,
    expected: str,
) -> None:
    assert bootstrap_db.derive_system_database_url(runtime_url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgres://modulo:pw@db.internal:5432/modulo",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
        ),
        # sslmode stripped as the only query param (trailing ? removed)
        (
            "postgres://modulo:pw@db.internal:5432/modulo?sslmode=require",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
        ),
        (
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?sslmode=disable",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
        ),
        # sslmode not the first query param — kept params survive
        (
            "postgres://modulo:pw@db.internal:5432/modulo?connect_timeout=10&sslmode=require",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?connect_timeout=10",
        ),
        # already-async URL with no postgres:// prefix is left otherwise untouched
        (
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
        ),
        # sslmode as the FIRST query param followed by others — the leading
        # "?sslmode=require" is stripped, leaving "&connect_timeout=10" attached
        # to the path (pre-existing behavior carried through this PR; the
        # trailing param is currently dropped rather than re-anchored with "?").
        (
            "postgres://modulo:pw@db.internal:5432/modulo?sslmode=require&connect_timeout=10",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo&connect_timeout=10",
        ),
    ],
)
def test_fix_database_url_scheme_and_sslmode(
    bootstrap_db,
    url: str,
    expected: str,
) -> None:
    assert bootstrap_db.fix_database_url(url) == expected


def test_set_path_fixes_system_url_like_database_url(bootstrap_db) -> None:
    # MODULO_SYSTEM_DATABASE_URL set explicitly is fixed exactly like DATABASE_URL.
    set_url = "postgres://modulo_system:s3cret@db.internal:5432/modulo?sslmode=require"
    assert bootstrap_db.fix_database_url(set_url) == (
        "postgresql+asyncpg://modulo_system:s3cret@db.internal:5432/modulo"
    )


def test_derivation_runs_on_the_fixed_database_url(bootstrap_db) -> None:
    # Real boot flow: DATABASE_URL is fixed first, then the system URL is derived
    # from the fixed value (password and host/port preserved, username swapped).
    fixed = bootstrap_db.fix_database_url("postgres://modulo:pw@db.internal:5432/modulo?sslmode=require")
    derived = bootstrap_db.derive_system_database_url(fixed)
    assert derived == "postgresql+asyncpg://modulo_system:pw@db.internal:5432/modulo"
