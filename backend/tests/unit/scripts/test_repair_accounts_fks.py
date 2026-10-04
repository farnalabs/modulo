"""Unit tests for repair_accounts_fks.py — sslmode translation (FAR-1441).

The script reuses ``modulo.db.bootstrap.split_postgres_sslmode`` (a stdlib-only
helper, so the "asyncpg + stdlib only" runtime contract is preserved). These
tests pin the script's fail-closed contract: the operator's ``sslmode`` is
HONOURED on the asyncpg connect, never silently stripped or downgraded.
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

import pytest

# repair_accounts_fks.py lives in backend/scripts/, which is NOT a package
# (no __init__.py) and shares the bare name ``scripts`` with the real
# repo-root scripts/ package (which DOES have __init__.py). Importing it as
# ``scripts.repair_accounts_fks`` therefore collides with the repo-root
# package and fails collection depending on sys.path order. Load it directly
# from its file path instead, mirroring test_generate_sbom.py.
for parent in Path(__file__).resolve().parents:
    _script_path = parent / "backend" / "scripts" / "repair_accounts_fks.py"
    if _script_path.exists():
        break
else:
    raise RuntimeError("Could not find backend/scripts/repair_accounts_fks.py")

_loader = SourceFileLoader("repair_accounts_fks", str(_script_path))
_mod = module_from_spec(spec_from_loader("repair_accounts_fks", _loader))
_loader.exec_module(_mod)

split_postgres_sslmode = _mod.split_postgres_sslmode
_resolve_db_url = _mod._resolve_db_url


@pytest.mark.parametrize(
    ("url", "expected_url", "expected_ssl"),
    [
        ("postgresql://u:p@h:5432/db", "postgresql://u:p@h:5432/db", False),
        ("postgresql://u:p@h:5432/db?sslmode=disable", "postgresql://u:p@h:5432/db", False),
        ("postgresql://u:p@h:5432/db?sslmode=require", "postgresql://u:p@h:5432/db", "require"),
        ("postgresql://u:p@h:5432/db?sslmode=verify-full", "postgresql://u:p@h:5432/db", "verify-full"),
        (
            "postgresql://u:p@h:5432/db?connect_timeout=10&sslmode=require",
            "postgresql://u:p@h:5432/db?connect_timeout=10",
            "require",
        ),
    ],
)
def test_split_postgres_sslmode_translates(url: str, expected_url: str, expected_ssl: bool | str) -> None:
    assert split_postgres_sslmode(url) == (expected_url, expected_ssl)


@pytest.mark.parametrize("mode", ["prefer", "allow", "verify-bogus"])
def test_split_postgres_sslmode_rejects_downgrading_modes(mode: str) -> None:
    """prefer/allow silently downgrade to plaintext — refused."""
    with pytest.raises(ValueError, match="sslmode"):
        split_postgres_sslmode(f"postgresql://u:p@h:5432/db?sslmode={mode}")


def test_split_postgres_sslmode_rejects_non_postgres_scheme() -> None:
    with pytest.raises(ValueError, match="Postgres URL"):
        split_postgres_sslmode("mysql://u:p@h:3306/db")


@pytest.mark.parametrize(
    ("raw", "expected_url", "expected_ssl"),
    [
        # asyncpg driver prefix: rewritten AND sslmode honoured.
        (
            "postgresql+asyncpg://u:p@h:5432/db?sslmode=require",
            "postgresql://u:p@h:5432/db",
            "require",
        ),
        # legacy psycopg prefix behaves the same.
        (
            "postgresql+psycopg://u:p@h:5432/db?sslmode=verify-ca",
            "postgresql://u:p@h:5432/db",
            "verify-ca",
        ),
        # bare postgres:// prefix: rewritten, no sslmode → explicit plaintext.
        ("postgres://u:p@h:5432/db", "postgresql://u:p@h:5432/db", False),
        # sslmode=disable stays explicit plaintext.
        (
            "postgres://u:p@h:5432/db?sslmode=disable",
            "postgresql://u:p@h:5432/db",
            False,
        ),
    ],
)
def test_resolve_db_url_honours_sslmode(raw: str, expected_url: str, expected_ssl: bool | str | None) -> None:
    """FAR-1441: _resolve_db_url returns the ssl kwarg value, never strips it."""
    assert _resolve_db_url(raw) == (expected_url, expected_ssl)


def test_resolve_db_url_passes_non_postgres_through_without_ssl() -> None:
    """A non-Postgres URL passes through unchanged with no ssl kwarg."""
    url, ssl_arg = _resolve_db_url("mysql://u:p@h:3306/db")
    assert url == "mysql://u:p@h:3306/db"
    assert ssl_arg is None


def test_resolve_db_url_fails_closed_on_downgrading_sslmode() -> None:
    with pytest.raises(ValueError, match="sslmode"):
        _resolve_db_url("postgresql+asyncpg://u:p@h:5432/db?sslmode=prefer")


@pytest.mark.asyncio
async def test_dispatch_passes_ssl_kwarg_to_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ssl value resolved from the URL reaches asyncpg.connect (FAR-1441).

    Uses an unknown command so _dispatch returns 1 without running any SQL —
    the connect (and its kwargs) is what this test pins.
    """
    mod = _mod

    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _Conn:
        async def close(self) -> None:
            return None

    async def fake_connect(*args: object, **kwargs: object) -> Any:
        captured.append((args, kwargs))
        return _Conn()

    monkeypatch.setattr(mod.asyncpg, "connect", fake_connect)

    rc = await mod._dispatch("no-such-command", "postgresql://u:p@h:5432/db", ssl_arg="require")

    assert rc == 1
    assert len(captured) == 1
    _, kwargs = captured[0]
    assert kwargs["ssl"] == "require"


@pytest.mark.asyncio
async def test_dispatch_passes_no_ssl_kwarg_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _mod

    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _Conn:
        async def close(self) -> None:
            return None

    async def fake_connect(*args: object, **kwargs: object) -> Any:
        captured.append((args, kwargs))
        return _Conn()

    monkeypatch.setattr(mod.asyncpg, "connect", fake_connect)

    await mod._dispatch("no-such-command", "postgresql://u:p@h:5432/db", ssl_arg=None)

    assert len(captured) == 1
    _, kwargs = captured[0]
    assert "ssl" not in kwargs
