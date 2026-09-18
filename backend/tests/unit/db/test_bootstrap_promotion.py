"""Promotion tests for modulo.db.bootstrap (FAR-671).

Locks that the promoted module behaves identically to the pre-promotion
deploy/fly/bootstrap_db.py: the deploy file is now a thin importing shim and
every helper it re-exports IS the promoted implementation. Also proves the
dependency-light contract — the promoted bootstrap module is importable in a
fresh interpreter WITHOUT SQLAlchemy or any modulo app/model module.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import modulo.db.bootstrap as bootstrap_module
from modulo.db.url_utils import derive_system_database_url, fix_database_url

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEPLOY_SHIM = _REPO_ROOT / "deploy" / "fly" / "bootstrap_db.py"


@pytest.fixture(scope="module")
def deploy_shim() -> Any:
    """Load deploy/fly/bootstrap_db.py exactly like the legacy test suite does."""
    spec = importlib.util.spec_from_file_location("bootstrap_db_shim", _DEPLOY_SHIM)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgres://modulo:pw@db.internal:5432/modulo", "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo"),
        (
            "postgres://modulo:pw@db.internal:5432/modulo?sslmode=require",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
        ),
        (
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo?sslmode=disable",
            "postgresql+asyncpg://modulo:pw@db.internal:5432/modulo",
        ),
    ],
)
def test_shim_reexports_are_the_promoted_helpers(url: str, expected: str, deploy_shim: Any) -> None:
    assert deploy_shim.fix_database_url is fix_database_url
    assert deploy_shim.derive_system_database_url is derive_system_database_url
    assert deploy_shim.fix_database_url(url) == expected


def test_shim_main_is_the_promoted_main(deploy_shim: Any) -> None:
    assert deploy_shim.main is bootstrap_module.main


def test_promoted_bootstrap_main_writes_fixed_env_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterization: the promoted main() keeps the container boot flow.

    Env-fix behaviour and the three /tmp-style env files (paths injected by
    patching _write_env_file so the test is platform-independent) are locked.
    """
    monkeypatch.setenv("DATABASE_ADMIN_URL", "postgres://admin:pw@db.internal:5432/modulo?sslmode=require")
    monkeypatch.setenv("DATABASE_URL", "postgres://app:pw@db.internal:5432/modulo")
    # Pre-registering via setenv guarantees monkeypatch restores whatever
    # main() writes directly into os.environ (it mutates the env, not a copy).
    monkeypatch.setenv("MODULO_SYSTEM_DATABASE_URL", "")

    def _skip_bootstrap(coro: Any) -> None:
        coro.close()  # avoid the "coroutine was never awaited" RuntimeWarning

    monkeypatch.setattr(bootstrap_module.asyncio, "run", _skip_bootstrap)
    written: dict[str, str] = {}
    monkeypatch.setattr(bootstrap_module, "_write_env_file", lambda path, content: written.setdefault(path, content))

    bootstrap_module.main()

    assert written["/tmp/database_admin_url.env"] == "postgresql+asyncpg://admin:pw@db.internal:5432/modulo"
    assert written["/tmp/database_url.env"] == "postgresql+asyncpg://app:pw@db.internal:5432/modulo"
    assert written["/tmp/system_database_url.env"] == "postgresql+asyncpg://modulo_system:pw@db.internal:5432/modulo"
    assert os.environ["DATABASE_ADMIN_URL"] == "postgresql+asyncpg://admin:pw@db.internal:5432/modulo"
    assert os.environ["MODULO_SYSTEM_DATABASE_URL"] == "postgresql+asyncpg://modulo_system:pw@db.internal:5432/modulo"


def test_promoted_main_warns_when_system_url_derivation_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_ADMIN_URL", "postgresql+asyncpg://admin:pw@db.internal:5432/modulo")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://db.internal:5432/modulo")
    monkeypatch.delenv("MODULO_SYSTEM_DATABASE_URL", raising=False)

    def _skip_bootstrap(coro: Any) -> None:
        coro.close()

    monkeypatch.setattr(bootstrap_module.asyncio, "run", _skip_bootstrap)
    monkeypatch.setattr(bootstrap_module, "_write_env_file", lambda _path, _content: None)

    bootstrap_module.main()

    err = capsys.readouterr().err
    assert "cannot derive MODULO_SYSTEM_DATABASE_URL" in err
    assert "no usable password/userinfo" in err


_IMPORT_LIGHT_SCRIPT = (
    "import sys\n"
    "import modulo.db.bootstrap\n"
    "import modulo.db.url_utils\n"
    "import modulo.db.health_checks\n"
    "banned = [m for m in sys.modules if m.startswith(('sqlalchemy', 'modulo.api', 'modulo.db.models', "
    "'modulo.core', 'modulo.auth'))]\n"
    "assert not banned, f'promotion modules must stay dependency-light: {banned}'\n"
    "print('OK')\n"
)


def test_promoted_modules_import_without_sqlalchemy_or_app_imports() -> None:
    """Prove the dependency-light contract in a FRESH interpreter.

    bootstrap.py and url_utils.py must import with NO SQLAlchemy and NO app
    (api/models/core/auth) modules pulled in — the native launcher imports
    them before the heavy stack. (The subprocess env inherits the venv; no
    application settings are read at import time.)
    """
    result = subprocess.run(  # noqa: S603 — fixed argv, no user input
        [sys.executable, "-c", _IMPORT_LIGHT_SCRIPT],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert result.stdout.strip().endswith("OK")
