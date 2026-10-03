"""Unit tests for the promoted zero-dependency URL helpers (FAR-671, FAR-1440).

``modulo.db.url_utils`` is the SINGLE implementation of the boot URL contract
(container bootstrap, Settings validator, native launcher). These tests lock:

* ``fix_database_url`` semantics — legacy prefix rewrites, ``sslmode``
  PRESERVED on Postgres-family URLs (an operator's TLS setting is honoured,
  FAR-1440) and stripped from MySQL-family URLs (aiomysql rejects it);
* ``split_postgres_sslmode`` — the sslmode→asyncpg ``ssl`` translation with
  a fail-closed posture (require/verify-* kept, prefer/allow rejected,
  absent/disable → explicit plaintext ``False``);
* ``derive_system_database_url`` verbatim behaviour.
"""

from __future__ import annotations

import pytest

from modulo.db.url_utils import derive_system_database_url, fix_database_url, split_postgres_sslmode

# ---------------------------------------------------------------------------
# fix_database_url — prefix rewrites + per-family sslmode handling
# ---------------------------------------------------------------------------

POSTGRES_PRESERVE_CASES = [
    # plain legacy postgres:// is rewritten to the asyncpg prefix
    (
        "postgres://modulo:pw@db.internal:5432/modulo",
        "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
    ),
    # an operator's sslmode is honoured: PRESERVED verbatim on every shape
    (
        "postgres://modulo:pw@db.internal:5432/modulo?sslmode=require",
        "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?sslmode=require",
    ),
    (
        "postgres://modulo:pw@db.internal:5432/modulo?sslmode=disable",
        "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?sslmode=disable",
    ),
    # sslmode not the first query param — untouched
    (
        "postgres://modulo:pw@db.internal:5432/modulo?connect_timeout=10&sslmode=require",
        "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?connect_timeout=10&sslmode=require",
    ),
    # mid-chain sslmode — untouched (the engine factory removes it later, cleanly)
    (
        "postgres://u:p@h:5432/db?application_name=mod&sslmode=verify-full&connect_timeout=2",
        "postgresql+asyncpg://u:p@h:5432/db?application_name=mod&sslmode=verify-full&connect_timeout=2",
    ),
    # already-driver URL with sslmode — untouched
    (
        "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?sslmode=verify-ca",
        "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?sslmode=verify-ca",
    ),
    # prefix-only rewriting: a credential containing 'postgres://' is not a URL prefix
    (
        "postgresql://u:postgres://pw@h:5432/db",
        "postgresql://u:postgres://pw@h:5432/db",
    ),
]


@pytest.mark.parametrize(("url", "expected"), POSTGRES_PRESERVE_CASES)
def test_fix_database_url_preserves_postgres_sslmode(url: str, expected: str) -> None:
    assert fix_database_url(url) == expected


MYSQL_STRIP_CASES = [
    ("mysql+asyncmy://modulo:modulo@localhost:3306/modulo", "mysql+aiomysql://modulo:modulo@localhost:3306/modulo"),
    # aiomysql rejects sslmode — stripped, with the known wart when first
    ("mysql+aiomysql://u:p@h:3306/db?sslmode=require&charset=utf8", "mysql+aiomysql://u:p@h:3306/db&charset=utf8"),
    (
        "mysql+aiomysql://u:p@h:3306/db?connect_timeout=3&sslmode=disable",
        "mysql+aiomysql://u:p@h:3306/db?connect_timeout=3",
    ),
]


@pytest.mark.parametrize(("url", "expected"), MYSQL_STRIP_CASES)
def test_fix_database_url_strips_mysql_sslmode(url: str, expected: str) -> None:
    assert fix_database_url(url) == expected


# ---------------------------------------------------------------------------
# split_postgres_sslmode — the sslmode→asyncpg ssl translation (FAR-1440)
# ---------------------------------------------------------------------------

SPLIT_PASS_CASES = [
    pytest.param(
        "postgresql+asyncpg://u:p@h/db",
        "postgresql+asyncpg://u:p@h/db",
        False,
        id="no_sslmode_explicit_plaintext",
    ),
    pytest.param(
        "postgres://u:p@h/db",
        "postgres://u:p@h/db",
        False,
        id="plain_postgres_refused_default",
    ),
    pytest.param(
        "postgresql+asyncpg://u:p@h/db?sslmode=disable",
        "postgresql+asyncpg://u:p@h/db",
        False,
        id="disable_removed_plaintext",
    ),
    pytest.param(
        "postgresql+asyncpg://u:p@h/db?sslmode=require",
        "postgresql+asyncpg://u:p@h/db",
        "require",
        id="require_only_param",
    ),
    pytest.param(
        "postgresql+asyncpg://u:p@h/db?connect_timeout=10&sslmode=verify-ca",
        "postgresql+asyncpg://u:p@h/db?connect_timeout=10",
        "verify-ca",
        id="verify_ca_last_param",
    ),
    # sslmode FIRST: the following param is re-anchored with '?' (no wart)
    pytest.param(
        "postgresql+asyncpg://u:p@h/db?sslmode=verify-full&connect_timeout=10",
        "postgresql+asyncpg://u:p@h/db?connect_timeout=10",
        "verify-full",
        id="sslmode_first_param_neighbours_kept",
    ),
    pytest.param(
        "postgresql+asyncpg://u:p@h/db?application_name=mod&sslmode=require&connect_timeout=2",
        "postgresql+asyncpg://u:p@h/db?application_name=mod&connect_timeout=2",
        "require",
        id="sslmode_mid_param_neighbours_kept",
    ),
    pytest.param(
        "postgresql+asyncpg://u:p%40ss@h/db?sslmode=require",
        "postgresql+asyncpg://u:p%40ss@h/db",
        "require",
        id="encoded_password_preserved",
    ),
    pytest.param(
        "postgresql+asyncpg://u:ab+cd@h/db?sslmode=require",
        "postgresql+asyncpg://u:ab+cd@h/db",
        "require",
        id="plus_encoding_preserved",
    ),
    pytest.param(
        "postgresql+psycopg://u:p@h/db?sslmode=require",
        "postgresql+psycopg://u:p@h/db",
        "require",
        id="psycopg_scheme_translated",
    ),
]


@pytest.mark.parametrize(("url", "expected_url", "expected_ssl"), SPLIT_PASS_CASES)
def test_split_postgres_sslmode_pass(url: str, expected_url: str, expected_ssl: str | bool) -> None:
    assert split_postgres_sslmode(url) == (expected_url, expected_ssl)


@pytest.mark.parametrize(
    ("url", "bad_mode"),
    [
        pytest.param("postgresql+asyncpg://u:p@h/db?sslmode=prefer", "prefer", id="prefer_rejected"),
        pytest.param("postgresql+asyncpg://u:p@h/db?sslmode=allow", "allow", id="allow_rejected"),
        pytest.param("postgresql+asyncpg://u:p@h/db?sslmode=bogus", "bogus", id="unknown_mode_rejected"),
    ],
)
def test_split_postgres_sslmode_rejects_unsafe_or_unknown(url: str, bad_mode: str) -> None:
    # prefer/allow are silent downgrades in asyncpg (CERT_NONE SSLContext);
    # unknown modes cannot guarantee a posture — refuse the boot instead.
    with pytest.raises(ValueError, match="sslmode"):
        split_postgres_sslmode(url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("mysql+aiomysql://u:p@h/db?sslmode=require", id="mysql_scheme"),
        pytest.param("sqlite+aiosqlite:///./test.db?sslmode=require", id="sqlite_scheme"),
    ],
)
def test_split_postgres_sslmode_refuses_non_postgres_scheme(url: str) -> None:
    with pytest.raises(ValueError, match="scheme"):
        split_postgres_sslmode(url)


# ---------------------------------------------------------------------------
# derive_system_database_url — username swap (unchanged by FAR-1440)
# ---------------------------------------------------------------------------

DERIVE_CASES = [
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
    # no password — cannot derive a matching modulo_system credential
    ("postgresql+asyncpg://modulo@db.internal:5432/modulo", ""),
    # explicit empty password (user:@host) — same as no password
    ("postgresql+asyncpg://modulo:@db.internal:5432/modulo", ""),
    # query-string preserved unchanged (incl. the operator's sslmode)
    (
        "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?connect_timeout=10&sslmode=require",
        "postgresql+asyncpg://modulo_system:pw@db.internal:5432/modulo?connect_timeout=10&sslmode=require",
    ),
    # no userinfo@ separator — nothing to swap, returns empty (caller skips)
    ("postgresql+asyncpg://db.internal:5432/modulo", ""),
]


@pytest.mark.parametrize(("runtime_url", "expected"), DERIVE_CASES)
def test_derive_system_database_url_swaps_username(runtime_url: str, expected: str) -> None:
    assert derive_system_database_url(runtime_url) == expected


def test_derivation_runs_on_the_fixed_database_url() -> None:
    # Real boot flow: DATABASE_URL is fixed first, then the system URL is
    # derived from the fixed value (password, host/port AND sslmode preserved).
    fixed = fix_database_url("postgres://modulo:pw@db.internal:5432/modulo?sslmode=require")
    assert fixed == "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?sslmode=require"
    derived = derive_system_database_url(fixed)
    assert derived == "postgresql+asyncpg://modulo_system:pw@db.internal:5432/modulo?sslmode=require"
