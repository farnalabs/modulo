"""Unit tests for the promoted zero-dependency URL helpers (FAR-671).

``modulo.db.url_utils`` is the SINGLE implementation of the boot URL contract
(container bootstrap, Settings validator, native launcher). These tests lock:

* the unified ``fix_database_url`` semantics (legacy prefix rewrites + strip
  ALL sslmode params) against frozen copies of BOTH historical variants
  (deploy + settings) on their own fixture inputs;
* the documented superset difference (a non-``disable`` sslmode is now also
  stripped from Settings URLs);
* ``derive_system_database_url`` verbatim behaviour.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import pytest

from modulo.db.url_utils import derive_system_database_url, fix_database_url

# ---------------------------------------------------------------------------
# Frozen legacy reference implementations (characterization anchors).
# These are verbatim copies of the pre-promotion variants; they exist ONLY in
# this test file so drift between old and new behaviour is caught here.
# ---------------------------------------------------------------------------


def _legacy_deploy_fix_database_url(url: str) -> str:
    # Verbatim from deploy/fly/bootstrap_db.py pre-FAR-671.
    sslmode_re = re.compile(r"[?&]sslmode=[^&]*")
    fixed = url.replace("postgres://", "postgresql+asyncpg://", 1)
    return sslmode_re.sub("", fixed).rstrip("?")


def _legacy_settings_fix_database_url(url: str) -> str:
    # Verbatim from modulo.settings._fix_database_url pre-FAR-671
    # (postgres-rewrite branch; the asyncmy branch is covered separately).
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    return url.replace("?sslmode=disable", "").replace("&sslmode=disable", "")


FIX_DATABASE_URL_CASES = [
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
    # sslmode FIRST followed by others — known wart carried through: the
    # remaining param keeps its leading & (pre-existing behaviour).
    (
        "postgres://modulo:pw@db.internal:5432/modulo?sslmode=require&connect_timeout=10",
        "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo&connect_timeout=10",
    ),
    # mid-chain sslmode removal keeps the other params intact
    (
        "postgres://u:p@h:5432/db?application_name=mod&sslmode=verify-full&connect_timeout=2",
        "postgresql+asyncpg://u:p@h:5432/db?application_name=mod&connect_timeout=2",
    ),
]


@pytest.mark.parametrize(("url", "expected"), FIX_DATABASE_URL_CASES)
def test_fix_database_url_matches_legacy_deploy_variant(url: str, expected: str) -> None:
    assert fix_database_url(url) == expected
    assert fix_database_url(url) == _legacy_deploy_fix_database_url(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Fixtures where sslmode is absent or `disable` — the two historical
        # variants agreed here, so the unified semantics must match BOTH.
        (
            "postgres://modulo:pw@db.internal:5432/modulo",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
        ),
        (
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?sslmode=disable",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
        ),
        (
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
        ),
        (
            "postgres://modulo:pw@db.internal:5432/modulo?connect_timeout=10&sslmode=disable",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?connect_timeout=10",
        ),
    ],
)
def test_fix_database_url_matches_legacy_settings_variant(url: str, expected: str) -> None:
    assert fix_database_url(url) == expected
    assert fix_database_url(url) == _legacy_settings_fix_database_url(url)


@pytest.mark.parametrize("url", [case[0] for case in FIX_DATABASE_URL_CASES if "sslmode=" in case[0]])
def test_fix_database_url_superset_vs_legacy_settings_on_non_disable(url: str) -> None:
    # For non-disable sslmode fixtures the historical settings variant left
    # the parameter in place; the unified semantics strips it (documented
    # superset) while matching the deploy variant exactly.
    unified = fix_database_url(url)
    assert unified == _legacy_deploy_fix_database_url(url)
    assert "sslmode=" not in unified
    if "sslmode=disable" not in url:
        assert _legacy_settings_fix_database_url(url) != unified


def test_fix_database_url_superset_difference_vs_settings_variant() -> None:
    # Documented superset: the settings variant left a non-disable sslmode in
    # the URL (which asyncpg then rejects at parse time); the unified
    # semantics strips it. The deploy variant already stripped it.
    url = "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?sslmode=require"
    assert _legacy_settings_fix_database_url(url) == url  # historical: left as-is
    assert fix_database_url(url) == "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo"


def test_fix_database_url_rewrites_legacy_asyncmy_prefix() -> None:
    url = "mysql+asyncmy://modulo:modulo@localhost:3306/modulo"
    assert fix_database_url(url) == "mysql+aiomysql://modulo:modulo@localhost:3306/modulo"


def test_fix_database_url_prefix_only_postgres_rewrite() -> None:
    # A mid-URL 'postgres://' (e.g. inside a credential) is NOT rewritten —
    # the unified semantics only rewrites a prefix (documented narrowing vs
    # the historical deploy variant's str.replace).
    url = "postgresql://u:postgres://pw@h:5432/db"
    assert fix_database_url(url) == url


def test_fix_database_url_mysql_url_sslmode_stripped() -> None:
    # The unified strip applies to any driver prefix (aiomysql does not
    # accept sslmode either). Known wart carried through: sslmode FIRST among
    # params leaves the remaining param with its leading &.
    url = "mysql+aiomysql://u:p@h:3306/db?sslmode=require&charset=utf8"
    assert fix_database_url(url) == "mysql+aiomysql://u:p@h:3306/db&charset=utf8"


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
    # query-string preserved unchanged
    (
        "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?connect_timeout=10",
        "postgresql+asyncpg://modulo_system:pw@db.internal:5432/modulo?connect_timeout=10",
    ),
    # no userinfo@ separator — nothing to swap, returns empty (caller skips)
    ("postgresql+asyncpg://db.internal:5432/modulo", ""),
]


@pytest.mark.parametrize(("runtime_url", "expected"), DERIVE_CASES)
def test_derive_system_database_url_swaps_username(runtime_url: str, expected: str) -> None:
    assert derive_system_database_url(runtime_url) == expected


def test_derive_matches_reference_implementation() -> None:
    """Lock the promoted implementation against its historical reference shape.

    The reference here re-derives the swap via urlsplit the way the original
    body did — identical on every fixture input (including the @-containing
    password, which rpartition must split on the LAST @).
    """

    def _reference(runtime_url: str) -> str:
        parts = urlsplit(runtime_url)
        userinfo, sep, hostport = parts.netloc.rpartition("@")
        if sep:
            _, _, password = userinfo.partition(":")
            if password:
                return urlunsplit(parts._replace(netloc=f"modulo_system:{password}@{hostport}"))
        return ""

    for runtime_url, expected in DERIVE_CASES:
        promoted = derive_system_database_url(runtime_url)
        reference = _reference(runtime_url)
        assert promoted == reference
        assert reference == expected


def test_derivation_runs_on_the_fixed_database_url() -> None:
    # Real boot flow: DATABASE_URL is fixed first, then the system URL is
    # derived from the fixed value (password and host/port preserved).
    fixed = fix_database_url("postgres://modulo:pw@db.internal:5432/modulo?sslmode=require")
    derived = derive_system_database_url(fixed)
    assert derived == "postgresql+asyncpg://modulo_system:pw@db.internal:5432/modulo"
