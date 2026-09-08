"""Integration-of-pieces tests for first-boot safety wiring (FAR-671).

Locks the Settings-side contract of the native launcher: the pinned env_file
(never the CWD ``.env``) and the first-boot ambient-URL refusal hook firing
at Settings construction. These mutate module-level state in
``modulo.settings`` — every test restores the pin/guard and the
``get_settings`` cache via the autouse fixture.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

import modulo.settings as settings_module
from modulo.launcher.env_safety import AmbientEnvironmentError, make_first_boot_guard
from modulo.settings import Settings, get_settings, pin_env_file, pinned_env_file, set_first_boot_guard

_VALID_32 = "a" * 32
_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


@pytest.fixture(autouse=True)
def _reset_launcher_state() -> Iterator[None]:
    """Snapshot + restore the module-level pin/guard and the get_settings cache."""
    get_settings.cache_clear()
    yield
    settings_module._pinned_env_file = None
    settings_module._first_boot_guard = None
    get_settings.cache_clear()


def _settings_kwargs() -> dict[str, object]:
    return {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _FERNET_KEY,
        "fernet_key_old": "",
        "modulo_admin_password": "testpass",
        "redis_url": "redis://localhost:6379/0",
        "modulo_public_url": "http://localhost:8000",
        "watchdog_enabled": False,
    }


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", _VALID_32)
    monkeypatch.setenv("FERNET_KEY", _FERNET_KEY)


def test_pin_env_file_sets_and_pins(tmp_path: Path) -> None:
    config_path = tmp_path / "config.env"
    assert pinned_env_file() is None  # default: Docker/CWD behaviour
    pin_env_file(config_path)
    assert pinned_env_file() == config_path


def test_get_settings_reads_pinned_env_file_not_cwd_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch)
    # Env vars outrank dotenv sources in pydantic-settings — clear DATABASE_URL
    # so the pinned file's value is what get_settings() must surface.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # A stray CWD .env that must be IGNORED once the pin is active.
    cwd_env = tmp_path / "cwd" / ".env"
    cwd_env.parent.mkdir()
    cwd_env.write_text("DATABASE_URL=postgresql+asyncpg://cwd-stray/stray\n")
    monkeypatch.chdir(tmp_path / "cwd")

    pinned = tmp_path / "config.env"
    pinned.write_text("DATABASE_URL=postgresql+asyncpg://pinned-host/pinned\n")
    pin_env_file(pinned)

    settings = get_settings()
    assert settings.database_url == "postgresql+asyncpg://pinned-host/pinned"


def test_unpinned_get_settings_keeps_cwd_dotenv_behaviour(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cwd_env = tmp_path / ".env"
    cwd_env.write_text("DATABASE_URL=postgresql+asyncpg://cwd-default/cwd\n")
    monkeypatch.chdir(tmp_path)
    assert pinned_env_file() is None
    settings = get_settings()
    assert settings.database_url == "postgresql+asyncpg://cwd-default/cwd"


def test_first_boot_guard_fires_at_settings_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://foreign/db")
    set_first_boot_guard(make_first_boot_guard(tmp_path / "data"))
    with pytest.raises(AmbientEnvironmentError, match="DATABASE_URL"):
        Settings(**_settings_kwargs())  # type: ignore[arg-type]


def test_first_boot_guard_passes_when_state_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "data"
    state_dir.mkdir()
    (state_dir / "state.json").write_text("{}")
    monkeypatch.setenv("DATABASE_URL", "postgres://foreign/db")
    monkeypatch.setenv("REDIS_URL", "redis://foreign:6379/0")
    set_first_boot_guard(make_first_boot_guard(state_dir))
    settings = Settings(**_settings_kwargs())  # type: ignore[arg-type]
    assert settings.database_url == "postgresql+asyncpg://localhost/test"


def test_first_boot_guard_refuses_ambient_url_from_pinned_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DATABASE_URL injected only through the pinned env file must refuse.

    The dotenv source never appears in os.environ — the guard must inspect
    the same env_file the Settings construction will read (the pinned config).
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    pinned = tmp_path / "config.env"
    pinned.write_text("DATABASE_URL=postgres://foreign-from-dotenv/db\n")
    pin_env_file(pinned)
    set_first_boot_guard(make_first_boot_guard(tmp_path / "data", env_file=pinned))
    with pytest.raises(AmbientEnvironmentError, match="DATABASE_URL"):
        Settings(**_settings_kwargs())  # type: ignore[arg-type]


def test_first_boot_guard_passes_dotenv_urls_when_state_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "data"
    state_dir.mkdir()
    (state_dir / "state.json").write_text("{}")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    pinned = tmp_path / "config.env"
    pinned.write_text("DATABASE_URL=postgres://foreign-from-dotenv/db\n")
    pin_env_file(pinned)
    set_first_boot_guard(make_first_boot_guard(state_dir, env_file=pinned))
    # No explicit database_url kwarg: explicit kwargs outrank env sources in
    # pydantic-settings, and this test proves the DOTENV value is what the
    # guard (and Settings) see. `_env_file=pinned` mirrors exactly what
    # get_settings() passes when the pin is active.
    kwargs = {key: value for key, value in _settings_kwargs().items() if key != "database_url"}
    settings = Settings(_env_file=pinned, **kwargs)  # type: ignore[arg-type]
    # Post-bootstrap, an explicit URL is an operator override path (and the
    # Settings validator still fixes the scheme).
    assert settings.database_url == "postgresql+asyncpg://foreign-from-dotenv/db"


def test_no_guard_installed_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://foreign/db")
    settings = Settings(**_settings_kwargs())  # type: ignore[arg-type]
    assert settings.database_url == "postgresql+asyncpg://localhost/test"


def test_clear_guard_via_set_first_boot_guard_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://foreign/db")

    def _failing_guard() -> None:
        raise AmbientEnvironmentError("must not fire")

    set_first_boot_guard(_failing_guard)
    set_first_boot_guard(None)
    settings = Settings(**_settings_kwargs())  # type: ignore[arg-type]
    assert settings.database_url == "postgresql+asyncpg://localhost/test"


def test_settings_validator_delegates_to_url_utils(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(**{**_settings_kwargs(), "database_url": "postgres://modulo:pw@db:5432/modulo?sslmode=disable"})  # type: ignore[arg-type]
    assert settings.database_url == "postgresql+asyncpg://modulo:pw@db:5432/modulo"


def test_settings_validator_strips_all_sslmode_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(**{**_settings_kwargs(), "database_url": "postgres://modulo:pw@db:5432/modulo?sslmode=require"})  # type: ignore[arg-type]
    assert settings.database_url == "postgresql+asyncpg://modulo:pw@db:5432/modulo"


def test_settings_validator_rewrites_asyncmy_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(**{**_settings_kwargs(), "database_url": "mysql+asyncmy://modulo:pw@db:3306/modulo"})  # type: ignore[arg-type]
    assert settings.database_url == "mysql+aiomysql://modulo:pw@db:3306/modulo"
