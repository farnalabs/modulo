"""Unit tests for the ``modulo start`` entry orchestration (FAR-671 slice 2).

Locks: the import-order contract (env_safety FIRST, scrub before any other
modulo import), the boot-order smoke path with mocked children, ambient-URL
refusal before any bootstrap artefact, lock refusal naming the holder, the
degraded (crash-cap) nonzero exit, SAQ children gated on migration-head
readiness, and the POSIX detach dance.

Settings/launcher module state is global — every test restores the pin,
guard, and get_settings cache via the autouse fixture.
"""

import ast
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

import modulo.launcher.entry as entry_module
import modulo.launcher.secrets_file as secrets_file_module
import modulo.settings as settings_module
from modulo.launcher.entry import BootError, default_data_dir, resolve_bin_dir, run_start
from modulo.launcher.env_safety import AmbientEnvironmentError
from modulo.launcher.supervisor import DataDirLockError
from modulo.settings import get_settings

FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
SECRET_KEY = "a" * 32


@pytest.fixture(autouse=True)
def _reset_launcher_state() -> Iterator[None]:
    """Snapshot + restore the module-level pin/guard and the get_settings cache."""
    get_settings.cache_clear()
    yield
    settings_module._pinned_env_file = None
    settings_module._first_boot_guard = None
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _bypass_platform_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logic tests run on any OS: bypass the Windows-refusal seams."""
    if sys.platform == "win32":
        monkeypatch.setattr(secrets_file_module, "assert_supported_platform", lambda: None)
        import modulo.launcher.initdb as initdb_module

        monkeypatch.setattr(initdb_module, "assert_supported_platform", lambda: None)


def _raise_boot_error(message: str) -> Path:
    raise BootError(message)


# ---------------------------------------------------------------------------
# Import-order contract (AST)
# ---------------------------------------------------------------------------


def _entry_tree() -> ast.Module:
    source = Path(entry_module.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


def _top_level_modulo_imports(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names if alias.name.startswith("modulo"))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.startswith("modulo"):
            names.extend(f"{node.module}.{alias.name}" if alias.name != "*" else node.module for alias in node.names)
    return names


def test_entry_first_modulo_import_is_env_safety() -> None:
    tree = _entry_tree()
    imports = _top_level_modulo_imports(tree)
    assert imports == ["modulo.launcher.env_safety"]


def test_run_start_scrub_is_the_first_action() -> None:
    tree = _entry_tree()
    run_fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_start")
    body = list(run_fn.body)
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # skip the docstring
    first = body[0]
    assert isinstance(first, ast.Expr)
    call = first.value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Attribute)
    assert call.func.attr == "scrub_os_environment"


def test_entry_has_no_top_level_settings_or_cli_imports() -> None:
    tree = _entry_tree()
    forbidden = ("modulo.settings", "modulo.cli", "modulo.api", "modulo.db")
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif node.module:
                targets = [node.module]
            for target in targets:
                for prefix in forbidden:
                    assert not target.startswith(prefix), f"entry must not import {prefix} at module level"


# ---------------------------------------------------------------------------
# Scrub-first behaviour
# ---------------------------------------------------------------------------


def test_run_start_scrubs_environment_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    from modulo.launcher import env_safety

    calls: list[str] = []

    def recorded_scrub() -> None:
        calls.append("scrub")

    monkeypatch.setattr(env_safety, "scrub_os_environment", recorded_scrub)
    monkeypatch.setattr(entry_module, "default_data_dir", lambda: _raise_boot_error("stop-here"))
    with pytest.raises(BootError, match="stop-here"):
        run_start()
    assert calls == ["scrub"]


def test_run_start_really_scrubs_hostile_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "foreign.example")
    monkeypatch.setenv("PYTHONPATH", "/foreign/tree")
    monkeypatch.setattr(entry_module, "default_data_dir", lambda: _raise_boot_error("stop-here"))
    with pytest.raises(BootError, match="stop-here"):
        run_start()
    assert "PGHOST" not in os.environ
    assert "PYTHONPATH" not in os.environ


# ---------------------------------------------------------------------------
# Boot-order smoke test (mocked children, real supervisor)
# ---------------------------------------------------------------------------


class FakeProc:
    all_procs: ClassVar[list["FakeProc"]] = []

    def __init__(self) -> None:
        self.pid = 777000 + len(FakeProc.all_procs)
        FakeProc.all_procs.append(self)

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _install_boot_mocks(monkeypatch: pytest.MonkeyPatch, order: list[str], *, mock_saq: bool = True) -> None:
    import modulo.launcher.config_source as config_source_module
    import modulo.launcher.initdb as initdb_module
    import modulo.launcher.state as state_mod
    import modulo.launcher.supervisor as supervisor_mod
    import modulo.settings as settings_mod

    class FakeLock:
        def __init__(self, data_dir: Path, mode: str = "serve") -> None:
            order.append("lock")

        def acquire(self) -> None:
            order.append("lock-acquired")

        def release(self) -> None:
            order.append("lock-released")

    monkeypatch.setattr(supervisor_mod, "DataDirLock", FakeLock)
    monkeypatch.setattr(supervisor_mod, "knobs_from_env", lambda: supervisor_mod.SupervisorKnobs(tick_seconds=1000.0))
    monkeypatch.setattr(supervisor_mod, "_default_spawner", lambda argv, env: FakeProc())

    def recorded_secrets(path: Path) -> secrets_file_module.LauncherSecrets:
        order.append("secrets")
        return secrets_file_module.LauncherSecrets(
            postgres_password="pg-pass", redis_password="redis-pass", state_hmac_key=bytes(range(32))
        )

    monkeypatch.setattr(secrets_file_module, "load_or_create", recorded_secrets)

    def recorded_load_state(path: Path, key: bytes) -> state_mod.LauncherState:
        order.append("state")
        return state_mod.LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)

    monkeypatch.setattr(state_mod, "load_state", recorded_load_state)
    monkeypatch.setattr(
        state_mod,
        "initial_state",
        lambda: state_mod.LauncherState(postgres_port=15432, redis_port=16379, api_port=18000),
    )

    def recorded_save_state(state: state_mod.LauncherState, path: Path, key: bytes) -> None:
        order.append("save-state")

    monkeypatch.setattr(state_mod, "save_state", recorded_save_state)

    def recorded_reconcile(pgdata: Path) -> str | None:
        order.append("reconcile")
        return None

    monkeypatch.setattr(supervisor_mod, "reconcile_orphans", recorded_reconcile)

    def recorded_initdb(pgdata: Path, *, password: str, bin_dir: Path | None = None, **kwargs: object) -> Path:
        order.append("initdb")
        pgdata.mkdir(parents=True, exist_ok=True)
        (pgdata / "PG_VERSION").write_text("16\n")
        return pgdata

    monkeypatch.setattr(initdb_module, "bootstrap_pgdata", recorded_initdb)

    monkeypatch.setattr(
        config_source_module,
        "write_pinned_env_file",
        lambda data_dir, state, secrets: order.append("pin") or (data_dir / "config.env"),
    )
    monkeypatch.setattr(
        config_source_module,
        "compose_config",
        lambda state, secrets: {"DATABASE_URL": "postgresql://x", "REDIS_URL": "redis://x"},
    )

    def recorded_pin(path: object) -> None:
        order.append("pin-env")

    def recorded_guard(guard: object) -> None:
        order.append("guard")

    monkeypatch.setattr(settings_mod, "pin_env_file", recorded_pin)
    monkeypatch.setattr(settings_mod, "set_first_boot_guard", recorded_guard)

    monkeypatch.setattr(entry_module, "_pg_isready_probe", lambda bin_dir, port: lambda: True)
    monkeypatch.setattr(entry_module, "_redis_ping_probe", lambda bin_dir, port, pw: lambda: True)
    monkeypatch.setattr(entry_module, "_ensure_app_database", lambda state, secrets: order.append("ensure-db"))
    monkeypatch.setattr(entry_module, "_prepare_database_env", lambda composed: order.append("db-env"))
    monkeypatch.setattr(
        entry_module,
        "_build_settings",
        lambda: (
            order.append("settings")
            or settings_mod.Settings(
                database_url="postgresql+asyncpg://localhost/test",
                secret_key=SECRET_KEY,
                fernet_key=FERNET_KEY,
            )
        ),
    )
    if mock_saq:

        def record_saq(supervisor: object, settings: object, composed: dict[str, str]) -> None:
            order.append("saq")

        monkeypatch.setattr(entry_module, "_add_saq_children", record_saq)


def test_boot_order_smoke_with_mocked_children(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []
    _install_boot_mocks(monkeypatch, order)
    # Other test packages' conftests setdefault ambient DATABASE_URL at
    # collection time; the launcher must see a clean environment here.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    serve_calls: list[tuple[str, int]] = []

    def serve(host: str, port: int, stop_event: object) -> None:
        order.append("serve")
        serve_calls.append((host, port))

    code = run_start(tmp_path / "data", serve=serve)
    assert code == 0
    expected_prefix = [
        "lock",
        "lock-acquired",
        "secrets",
        "save-state",
        "reconcile",
        "initdb",
        "pin",
        "pin-env",
        "guard",
        "ensure-db",
        "db-env",
        "settings",
        "saq",
        "serve",
        "lock-released",
    ]
    assert order == expected_prefix
    assert serve_calls == [("127.0.0.1", 18000)]


def test_lock_refusal_propagates_naming_holder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import modulo.launcher.supervisor as supervisor_mod

    class RefusingLock:
        def __init__(self, data_dir: Path, mode: str = "serve") -> None:
            pass

        def acquire(self) -> None:
            raise DataDirLockError("data dir locked by another launcher (holder PID 4242, mode 'serve')")

        def release(self) -> None:
            pass

    monkeypatch.setattr(supervisor_mod, "DataDirLock", RefusingLock)
    with pytest.raises(DataDirLockError, match="holder PID 4242"):
        run_start(tmp_path / "data")


def test_ambient_url_refusal_precedes_any_bootstrap_artefact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import modulo.launcher.supervisor as supervisor_mod

    created: list[Path] = []

    class OkLock:
        def __init__(self, data_dir: Path, mode: str = "serve") -> None:
            pass

        def acquire(self) -> None:
            pass

        def release(self) -> None:
            pass

    monkeypatch.setattr(supervisor_mod, "DataDirLock", OkLock)
    real_load = secrets_file_module.load_or_create

    def spy_load(path: Path) -> object:
        created.append(path)
        return real_load(path)

    monkeypatch.setattr(secrets_file_module, "load_or_create", spy_load)
    monkeypatch.setenv("DATABASE_URL", "postgres://foreign/db")
    with pytest.raises(AmbientEnvironmentError, match="first boot refused"):
        run_start(tmp_path / "data")
    assert not created


def test_degraded_boot_exits_nonzero_with_doctor_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import modulo.launcher.config_source as config_source_module
    import modulo.launcher.initdb as initdb_module
    import modulo.launcher.supervisor as supervisor_mod

    class FakeLock:
        def __init__(self, data_dir: Path, mode: str = "serve") -> None:
            pass

        def acquire(self) -> None:
            pass

        def release(self) -> None:
            pass

    monkeypatch.setattr(supervisor_mod, "DataDirLock", FakeLock)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(
        supervisor_mod,
        "knobs_from_env",
        lambda: supervisor_mod.SupervisorKnobs(
            tick_seconds=0.01,
            restart_backoff_initial=0.001,
            restart_backoff_max=0.003,
            crash_window_seconds=2.0,
            crash_cap=3,
            pg_fast_shutdown_timeout=0.01,
            shutdown_grace_seconds=0.01,
            health_check_timeout=0.5,
            health_check_interval=0.01,
        ),
    )

    class DyingProc:
        pid = 888000

        def poll(self) -> int | None:
            return 7

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 7

    monkeypatch.setattr(supervisor_mod, "_default_spawner", lambda argv, env: DyingProc())

    def fake_initdb(pgdata: Path, *, password: str, bin_dir: Path | None = None, **kwargs: object) -> Path:
        pgdata.mkdir(parents=True, exist_ok=True)
        (pgdata / "PG_VERSION").write_text("16\n")
        return pgdata

    monkeypatch.setattr(initdb_module, "bootstrap_pgdata", fake_initdb)
    monkeypatch.setattr(
        config_source_module,
        "write_pinned_env_file",
        lambda data_dir, state, secrets: data_dir / "config.env",
    )
    monkeypatch.setattr(
        config_source_module,
        "compose_config",
        lambda state, secrets: {"DATABASE_URL": "postgresql://x", "REDIS_URL": "redis://x"},
    )
    monkeypatch.setattr(settings_module, "pin_env_file", lambda path: None)
    monkeypatch.setattr(settings_module, "set_first_boot_guard", lambda guard: None)
    monkeypatch.setattr(entry_module, "_pg_isready_probe", lambda bin_dir, port: lambda: True)
    monkeypatch.setattr(entry_module, "_redis_ping_probe", lambda bin_dir, port, pw: lambda: True)
    monkeypatch.setattr(entry_module, "_ensure_app_database", lambda state, secrets: None)
    monkeypatch.setattr(entry_module, "_prepare_database_env", lambda composed: None)
    monkeypatch.setattr(
        entry_module,
        "_build_settings",
        lambda: settings_module.Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key=SECRET_KEY,
            fernet_key=FERNET_KEY,
        ),
    )
    monkeypatch.setattr(entry_module, "_add_saq_children", lambda supervisor, settings, composed: None)

    def serve(host: str, port: int, stop_event: threading.Event) -> None:
        assert stop_event.wait(timeout=10)

    code = run_start(tmp_path / "data", serve=serve)
    captured = capsys.readouterr()
    assert code == 1
    assert "launcher degraded" in captured.err
    assert "modulo doctor" in captured.err


def test_saq_children_gated_on_migration_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The SAQ specs registered by the entry carry the head-readiness gate."""
    registered: dict[str, object] = {}

    class RecordingSupervisor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.children: dict[str, object] = {}
            self.degraded_reason = None

        def add(self, spec: object) -> None:
            registered[spec.name] = spec

        def start(self, start_monitor: bool = True) -> None:
            pass

        def shutdown(self) -> None:
            pass

        def request_stop(self) -> None:
            pass

    import modulo.api.dependencies as deps_module
    import modulo.db.health_checks as health_module
    import modulo.launcher.supervisor as supervisor_mod

    class FakeLock:
        def __init__(self, data_dir: Path, mode: str = "serve") -> None:
            pass

        def acquire(self) -> None:
            pass

        def release(self) -> None:
            pass

    monkeypatch.setattr(supervisor_mod, "DataDirLock", FakeLock)
    monkeypatch.setattr(supervisor_mod, "Supervisor", RecordingSupervisor)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(supervisor_mod, "knobs_from_env", lambda: supervisor_mod.SupervisorKnobs(tick_seconds=1000.0))
    head_calls = {"n": 0}

    async def fake_head(engine: object, alembic_ini: object = None) -> bool:
        head_calls["n"] += 1
        return True

    monkeypatch.setattr(health_module, "db_is_at_migration_head", fake_head)
    monkeypatch.setattr(deps_module, "get_or_create_engine", lambda settings: object())

    _install_boot_mocks(monkeypatch, [], mock_saq=False)
    run_start(tmp_path / "data", serve=lambda host, port, ev: None)
    for name in ("saq-runs", "saq-system"):
        spec = registered[name]
        condition = spec.start_condition
        assert condition() is True
    assert head_calls["n"] == 1


# ---------------------------------------------------------------------------
# Detach + helpers
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fork mechanics")
def test_detach_forks_twice_and_setsid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    forks: list[int] = []

    def fake_fork() -> int:
        forks.append(1)
        return 0  # always the child

    monkeypatch.setattr(os, "fork", fake_fork)
    monkeypatch.setattr(os, "setsid", lambda: None)
    redirected: list[Path] = []
    monkeypatch.setattr(entry_module, "_redirect_stdio", lambda log: redirected.append(log))
    monkeypatch.setattr(entry_module, "_run_foreground", lambda data_dir, bin_dir=None, serve=None: 7)
    code = run_start(tmp_path / "data", detach=True)
    assert code == 7
    assert len(forks) == 2
    assert redirected == [tmp_path / "data" / "launcher.log"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refusal seam")
def test_detach_refused_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(BootError, match=r"TODO\(P3\)"):
        run_start(tmp_path / "data", detach=True)


def test_default_data_dir_is_posix_root(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        with pytest.raises(BootError):
            default_data_dir()
        return
    monkeypatch.setenv("XDG_DATA_HOME", str(Path.home() / "xdg-test"))
    assert default_data_dir() == Path.home() / "xdg-test" / "modulo" / "data"
    monkeypatch.delenv("XDG_DATA_HOME")
    expected = Path.home() / ".local" / "share" / "modulo" / "data"
    assert default_data_dir() == expected


def test_resolve_bin_dir_param_env_and_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(entry_module.BUNDLED_BIN_DIR_ENV, raising=False)
    explicit = tmp_path / "bin"
    assert resolve_bin_dir(explicit) == explicit
    monkeypatch.setenv(entry_module.BUNDLED_BIN_DIR_ENV, str(tmp_path / "from-env"))
    assert resolve_bin_dir() == tmp_path / "from-env"
    monkeypatch.delenv(entry_module.BUNDLED_BIN_DIR_ENV)
    assert resolve_bin_dir() == Path(sys.prefix) / "bundled" / "bin"


def test_redis_server_argv_pins_every_argument(tmp_path: Path) -> None:
    argv = entry_module._redis_server_argv(tmp_path, 16379, "sekrit")
    assert Path(argv[0]).name.startswith("redis-server")
    assert "16379" in argv
    assert "sekrit" in argv
