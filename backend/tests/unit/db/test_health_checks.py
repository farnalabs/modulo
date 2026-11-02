"""Unit tests for the promoted migration-head predicate (FAR-671).

``modulo.db.health_checks.db_is_at_migration_head`` is the single
implementation of the boot fast-path check; ``modulo.api.main`` keeps a thin
wrapper as its test seam. Both fail-safe branches (head unresolvable, query
error, ScriptDirectory failure) and the true/mismatch paths are locked here,
plus the module-relative alembic.ini resolution.
"""

import os
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

# Minimal env so importing ``modulo.api.main`` (which builds the lazy engine
# from Settings at import time) works outside the tests/unit/api conftest.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)

import pytest

import modulo.api.main as main_module
from modulo.db.health_checks import (
    db_is_at_migration_head,
    resolve_alembic_ini,
    resolve_migration_script_location,
)


def _engine_with_rows(rows: list[tuple[str, ...]]) -> MagicMock:
    engine = MagicMock()
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=rows)))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    engine.connect = MagicMock(return_value=cm)
    return engine


def _patch_head(monkeypatch: pytest.MonkeyPatch, head: str | Exception | None) -> None:
    script_dir = MagicMock()
    if isinstance(head, Exception):
        script_dir.get_current_head = MagicMock(side_effect=head)
    else:
        script_dir.get_current_head = MagicMock(return_value=head)
    monkeypatch.setattr("alembic.script.ScriptDirectory.from_config", MagicMock(return_value=script_dir))


def test_resolve_alembic_ini_finds_backend_config() -> None:
    path = resolve_alembic_ini()
    assert path.name == "alembic.ini"
    assert path.exists() is True


def test_resolve_migration_script_location_is_absolute_and_exists(tmp_path: Path) -> None:
    backend_root = tmp_path / "Installed App.app" / "Contents" / "Resources" / "bundle" / "backend"
    migration_tree = backend_root / "src" / "modulo" / "db" / "migrations"
    migration_tree.mkdir(parents=True)
    ini_path = backend_root / "alembic.ini"
    ini_path.write_text("[alembic]\n")

    resolved = resolve_migration_script_location(ini_path)

    assert resolved == migration_tree.resolve()
    assert resolved.is_absolute()
    assert resolved.is_dir()


def test_resolve_migration_script_location_fails_when_tree_missing(tmp_path: Path) -> None:
    ini_path = tmp_path / "backend" / "alembic.ini"
    ini_path.parent.mkdir()
    ini_path.write_text("[alembic]\n")

    with pytest.raises(FileNotFoundError, match="Alembic migration tree does not exist"):
        resolve_migration_script_location(ini_path)


@pytest.mark.anyio
async def test_db_is_at_migration_head_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_head(monkeypatch, "head123")
    engine = _engine_with_rows([("head123",)])
    assert await db_is_at_migration_head(engine) is True


@pytest.mark.anyio
async def test_db_is_at_migration_head_false_on_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_head(monkeypatch, "head123")
    engine = _engine_with_rows([("old123",)])
    assert await db_is_at_migration_head(engine) is False


@pytest.mark.anyio
async def test_db_is_at_migration_head_false_when_multiple_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two rows in alembic_version (branch fork) can never equal the single
    # head set — the fast path must not fire.
    _patch_head(monkeypatch, "head123")
    engine = _engine_with_rows([("head123",), ("head456",)])
    assert await db_is_at_migration_head(engine) is False


@pytest.mark.anyio
async def test_db_is_at_migration_head_false_when_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_head(monkeypatch, "head123")
    engine = MagicMock()
    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("no table"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    engine.connect = MagicMock(return_value=cm)
    assert await db_is_at_migration_head(engine) is False


@pytest.mark.anyio
async def test_db_is_at_migration_head_false_when_head_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_head(monkeypatch, None)
    assert await db_is_at_migration_head(_engine_with_rows([])) is False


@pytest.mark.anyio
async def test_db_is_at_migration_head_false_when_scriptdir_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_head(monkeypatch, RuntimeError("bad ini"))
    assert await db_is_at_migration_head(_engine_with_rows([])) is False


class _FakeConfig:
    """Minimal alembic Config double that records the pinned script_location."""

    instances: ClassVar[list["_FakeConfig"]] = []

    def __init__(self, path: str) -> None:
        self.config_file_name = path
        self._options: dict[str, str] = {}
        _FakeConfig.instances.append(self)

    def set_main_option(self, key: str, value: str) -> None:
        self._options[key] = value

    def get_main_option(self, key: str) -> str:
        return self._options.get(key, "")


@pytest.mark.anyio
async def test_explicit_alembic_ini_is_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ini = tmp_path / "alembic.ini"
    ini.write_text("[alembic]\n")
    (tmp_path / "src" / "modulo" / "db" / "migrations").mkdir(parents=True)
    _FakeConfig.instances = []

    script_dir = MagicMock()
    script_dir.get_current_head = MagicMock(return_value="head123")
    monkeypatch.setattr("alembic.script.ScriptDirectory.from_config", MagicMock(return_value=script_dir))
    monkeypatch.setattr("alembic.config.Config", _FakeConfig)

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[("head123",)])))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect = MagicMock(return_value=cm)

    assert await db_is_at_migration_head(engine, alembic_ini=ini) is True
    config = _FakeConfig.instances[-1]
    assert config.config_file_name == str(ini)
    assert config.get_main_option("script_location") == str(
        (ini.parent / "src" / "modulo" / "db" / "migrations").resolve()
    )


@pytest.mark.anyio
async def test_main_wrapper_delegates_to_promoted_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API wrapper resolves the engine through main's own seam."""
    _patch_head(monkeypatch, "head123")
    engine = _engine_with_rows([("head123",)])
    monkeypatch.setattr(main_module, "get_or_create_engine", lambda _settings: engine)
    settings = MagicMock()
    assert await main_module._db_is_at_migration_head(settings) is True
