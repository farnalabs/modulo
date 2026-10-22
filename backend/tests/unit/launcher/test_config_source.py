"""Unit tests for the launcher config source (FAR-671 slice 2).

Locks: the env > state.json(+secrets) > defaults priority (both via the
pydantic-settings source class and via the pinned dotenv file), the import
hygiene rule (no ``modulo.settings`` import at module import time), the
0600 pinned-config write with symlink rejection, and the un-bootstrapped
refusal.
"""

import ast
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from modulo.launcher.config_source import (
    CONFIG_ENV_FILENAME,
    LauncherConfigError,
    LauncherConfigSource,
    compose_config,
    load_launcher_config_inputs,
    render_env_file,
    write_pinned_env_file,
)
from modulo.launcher.secrets_file import LauncherSecrets
from modulo.launcher.state import LauncherState, save_state
from modulo.settings import get_settings
from tests.unit.launcher.test_launcher_shared import bypass_platform_seams, launcher_state_reset

FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
SECRET_KEY = "a" * 32


@pytest.fixture(autouse=True)
def _reset_launcher_state() -> Iterator[None]:
    """The shared snapshot/restore body lives in the launcher-test module."""
    yield from launcher_state_reset()


@pytest.fixture(autouse=True)
def _bypass_platform_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the Windows-refusal seams this module's tests exercise."""
    bypass_platform_seams(monkeypatch, "modulo.launcher.secrets_file")


def _secrets() -> LauncherSecrets:
    return LauncherSecrets(postgres_password="pg-pw", redis_password="redis-pw", state_hmac_key=bytes(range(32)))


def _state() -> LauncherState:
    return LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)


# ---------------------------------------------------------------------------
# Import hygiene
# ---------------------------------------------------------------------------


def test_no_settings_import_at_module_time_by_ast() -> None:
    from modulo.launcher import config_source

    source = Path(config_source.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("modulo.settings"):
            raise AssertionError("config_source must not import modulo.settings at module level")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("modulo.settings")


def test_no_settings_module_loaded_after_import_by_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys, modulo.launcher.config_source; print('modulo.settings' in sys.modules)"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_compose_config_derives_all_bundled_endpoints() -> None:
    composed = compose_config(_state(), _secrets())
    assert composed["DATABASE_URL"] == "postgresql+asyncpg://modulo:pg-pw@127.0.0.1:15432/modulo"
    assert composed["DATABASE_ADMIN_URL"] == composed["DATABASE_URL"]
    assert composed["REDIS_URL"] == "redis://:redis-pw@127.0.0.1:16379/0"
    system_url = composed["MODULO_SYSTEM_DATABASE_URL"]
    assert system_url.startswith("postgresql+asyncpg://modulo_system:pg-pw@127.0.0.1:15432/modulo")


def test_compose_config_is_password_free_on_state_alone() -> None:
    payload = _state().to_payload()
    joined = repr(payload)
    assert "pg-pw" not in joined
    assert "redis-pw" not in joined


# ---------------------------------------------------------------------------
# Source class (direct Settings construction)
# ---------------------------------------------------------------------------


def test_source_omits_keys_the_environment_already_sets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import modulo.launcher.secrets_file as sf

    real_create = sf.load_or_create
    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    monkeypatch.setenv("DATABASE_URL", "postgres://operator-overridden/db")
    source = LauncherConfigSource(data_dir)
    resolved = source()
    assert "database_url" not in resolved  # env wins — the state value is dropped
    assert resolved["redis_url"] == "redis://:redis-pw@127.0.0.1:16379/0"
    real_create(data_dir / "secrets.json")  # keep the tmp dir realistic for later asserts


def test_source_redis_url_environment_wins_over_composed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An operator REDIS_URL env var must drop the composed redis value.

    The source returns init-kwargs (which outrank env), so any composed key
    whose environment variable is already set must be omitted — otherwise the
    bundled Redis password would shadow the operator's explicit choice.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import modulo.launcher.secrets_file as sf

    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODULO_SYSTEM_DATABASE_URL", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://operator:secret@cache.example.com:6379/7")
    resolved = LauncherConfigSource(data_dir)()
    assert "redis_url" not in resolved  # the operator's env wins
    assert resolved["database_url"] == "postgresql+asyncpg://modulo:pg-pw@127.0.0.1:15432/modulo"


def test_source_system_database_url_environment_wins_over_composed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MODULO_SYSTEM_DATABASE_URL set (without DATABASE_URL) drops the composed one."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import modulo.launcher.secrets_file as sf

    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("MODULO_SYSTEM_DATABASE_URL", "postgresql://system:op@db.example.com:5432/modulo")
    resolved = LauncherConfigSource(data_dir)()
    assert "modulo_system_database_url" not in resolved  # the operator's env wins
    assert resolved["database_url"] == "postgresql+asyncpg://modulo:pg-pw@127.0.0.1:15432/modulo"
    assert resolved["redis_url"] == "redis://:redis-pw@127.0.0.1:16379/0"


def test_source_derives_system_url_from_the_operators_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The app/system URL pair is atomic: an operator DATABASE_URL must never
    end up paired with a system URL still pointing at the bundled Postgres."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import modulo.launcher.secrets_file as sf

    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:op-pass@db.example.com:5432/modulo")
    monkeypatch.delenv("MODULO_SYSTEM_DATABASE_URL", raising=False)
    resolved = LauncherConfigSource(data_dir)()
    assert "database_url" not in resolved
    system = resolved.get("modulo_system_database_url", "")
    expected_prefixes = (
        "postgresql://modulo_system:op-pass@db.example.com:5432/modulo",
        "postgresql+asyncpg://modulo_system:op-pass@db.example.com:5432/modulo",
    )
    assert system.startswith(expected_prefixes)
    assert "127.0.0.1" not in system
    assert "pg-pw" not in system


def test_source_includes_everything_without_environment(
    clean_launcher_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import modulo.launcher.secrets_file as sf

    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    resolved = LauncherConfigSource(data_dir)()
    assert resolved["database_url"] == "postgresql+asyncpg://modulo:pg-pw@127.0.0.1:15432/modulo"
    assert resolved["redis_url"] == "redis://:redis-pw@127.0.0.1:16379/0"


def test_source_skips_empty_composed_values(
    clean_launcher_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A present-but-blank composed key is omitted rather than injected blank.

    De-stubbed (review finding): the composition with a NO-PASSWORD URL is
    the case that caused the regression — ``compose_config`` returns a
    real dict whose ``MODULO_SYSTEM_DATABASE_URL`` is present-but-blank —
    and the source must omit it, never write ``modulo_system_database_url=''``.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import modulo.launcher.secrets_file as sf

    password_free = LauncherSecrets(postgres_password="", redis_password="redis-pw", state_hmac_key=bytes(range(32)))
    monkeypatch.setattr(sf, "load_or_create", lambda _path: password_free)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODULO_SYSTEM_DATABASE_URL", raising=False)
    composed = compose_config(_state(), password_free)
    assert composed["MODULO_SYSTEM_DATABASE_URL"] == ""  # blank key IS present in the composition
    resolved = LauncherConfigSource(data_dir)()
    assert "modulo_system_database_url" not in resolved  # blank value omitted, not injected
    assert resolved  # the non-blank composed values are still injected
    assert all(resolved.values())


def test_source_settings_roundtrip_honours_priority(
    clean_launcher_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from modulo.settings import Settings

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import modulo.launcher.secrets_file as sf

    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    resolved = LauncherConfigSource(data_dir)()
    settings = Settings(
        secret_key=SECRET_KEY,
        fernet_key=FERNET_KEY,
        _env_file=None,
        **resolved,  # type: ignore[arg-type]
    )
    assert settings.database_url == "postgresql+asyncpg://modulo:pg-pw@127.0.0.1:15432/modulo"
    assert settings.redis_url == "redis://:redis-pw@127.0.0.1:16379/0"


def test_source_refuses_unbootstrapped_data_dir(tmp_path: Path) -> None:
    with pytest.raises(LauncherConfigError, match="not bootstrapped"):
        LauncherConfigSource(tmp_path / "data")()


def test_load_inputs_refuses_missing_state(tmp_path: Path) -> None:
    with pytest.raises(LauncherConfigError, match="not bootstrapped"):
        load_launcher_config_inputs(tmp_path / "data")


# ---------------------------------------------------------------------------
# Pinned env file
# ---------------------------------------------------------------------------


def test_render_env_file_is_sorted_dotenv() -> None:
    composed = compose_config(_state(), _secrets())
    rendered = render_env_file(composed)
    lines = rendered.splitlines()
    keys = [line.partition("=")[0] for line in lines]
    assert keys == sorted(keys)


def test_write_pinned_env_file_content_and_mode(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = write_pinned_env_file(data_dir, _state(), _secrets())
    assert path.name == CONFIG_ENV_FILENAME
    content = path.read_text(encoding="utf-8")
    assert "DATABASE_URL=postgresql+asyncpg://modulo:pg-pw@127.0.0.1:15432/modulo" in content
    assert "REDIS_URL=redis://:redis-pw@127.0.0.1:16379/0" in content
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600


def test_write_pinned_env_file_warns_when_clobbering_different_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    first = write_pinned_env_file(data_dir, _state(), _secrets())
    first.write_text("MANUALLY EDITED=1\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="modulo.launcher.config_source"):
        write_pinned_env_file(data_dir, _state(), _secrets())
    assert any("config_env_overwritten" in record.message for record in caplog.records)
    rewritten = first.read_text(encoding="utf-8")
    assert "DATABASE_URL=" in rewritten  # the composed content won


def test_write_pinned_env_file_no_warning_on_identical_rewrite(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    first = write_pinned_env_file(data_dir, _state(), _secrets())
    with caplog.at_level(logging.WARNING, logger="modulo.launcher.config_source"):
        second = write_pinned_env_file(data_dir, _state(), _secrets())
    assert not any("config_env_overwritten" in record.message for record in caplog.records)
    assert second.read_text(encoding="utf-8") == first.read_text(encoding="utf-8")


def test_write_pinned_env_file_refuses_symlink_squat(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("symlink semantics are POSIX-only")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("innocent")
    squat = data_dir / CONFIG_ENV_FILENAME
    squat.symlink_to(victim)
    with pytest.raises(RuntimeError, match="symlink"):
        write_pinned_env_file(data_dir, _state(), _secrets())
    assert victim.read_text(encoding="utf-8") == "innocent"


def test_pinned_env_file_feeds_get_settings(
    clean_launcher_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from modulo.settings import pin_env_file

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = write_pinned_env_file(data_dir, _state(), _secrets())
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("FERNET_KEY", FERNET_KEY)
    pin_env_file(str(path))
    settings = get_settings()
    assert settings.database_url == "postgresql+asyncpg://modulo:pg-pw@127.0.0.1:15432/modulo"
    assert settings.redis_url == "redis://:redis-pw@127.0.0.1:16379/0"


def test_environment_overrides_the_pinned_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """env > state.json: an operator DATABASE_URL beats the composed one."""
    from modulo.settings import pin_env_file

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = write_pinned_env_file(data_dir, _state(), _secrets())
    monkeypatch.setenv("DATABASE_URL", "postgres://operator-override/override")
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("FERNET_KEY", FERNET_KEY)
    pin_env_file(str(path))
    settings = get_settings()
    assert settings.database_url == "postgresql+asyncpg://operator-override/override"
