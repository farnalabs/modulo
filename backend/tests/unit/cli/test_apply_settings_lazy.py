"""FAR-1586: the ``apply`` CLI path must not require database/secret settings.

``modulo`` is a published console script (``modulo.cli.main:cli``). Importing
it executes the ``modulo.cli`` package ``__init__`` FIRST, and until FAR-1586
that module eagerly imported ``modulo.cli.migrate_org`` -> ``modulo.db.session``,
whose async engine is built AT IMPORT TIME through ``get_settings()``. The whole
``modulo apply`` surface therefore demanded ``DATABASE_URL`` / ``SECRET_KEY`` /
``FERNET_KEY`` at startup, even though ``apply`` only talks to a remote instance
over ``MODULO_URL`` + ``MODULO_API_KEY``.

The test below runs the REAL import path in a fresh subprocess with the
database/secret variables stripped and a working directory containing no
``.env``, so it fails on the import error if the eager settings/DB import ever
comes back.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

import modulo.cli as cli_package
from modulo.settings import Settings

#: Every variable that makes ``Settings`` constructible — all absent in the
#: subprocess, so an eager ``get_settings()`` cannot be satisfied.
_DB_OR_SECRET_ENV_VARS = (
    "DATABASE_URL",
    "SECRET_KEY",
    "FERNET_KEY",
    "FERNET_KEY_OLD",
)

#: Mirrors the only two variables ``apply`` legitimately reads.
_APPLY_ENV = {
    "MODULO_URL": "https://modulo.example.test",
    "MODULO_API_KEY": "mk_apply_only",
}

# Executed in a fresh interpreter; prints a marker only on full success.
_APPLY_IMPORT_SCRIPT = """
import os
import sys

import click
from click.testing import CliRunner

for _name in ("DATABASE_URL", "SECRET_KEY", "FERNET_KEY"):
    assert os.environ.get(_name) is None, f"{_name} leaked into the child env"
assert os.environ.get("MODULO_URL")
assert os.environ.get("MODULO_API_KEY")

import modulo.cli.main as cli_main
from modulo.settings import get_settings

apply_cmd = cli_main.cli.commands.get("apply")
assert apply_cmd is not None, "apply command is not registered on the group"
assert isinstance(apply_cmd, click.Command), type(apply_cmd)

# No settings object may have been constructed while importing the CLI.
assert get_settings.cache_info().currsize == 0

# ...and the module that builds the async engine at import time must not load.
assert "modulo.db.session" not in sys.modules

result = CliRunner().invoke(cli_main.cli, ["apply", "--help"])
assert result.exit_code == 0, result.output

print("APPLY_CONSTRUCTED_WITHOUT_DB_SETTINGS")
"""


def _child_env() -> dict[str, str]:
    """Ambient env minus every DB/secret variable, plus apply's own pair."""
    env = {key: value for key, value in os.environ.items() if key not in _DB_OR_SECRET_ENV_VARS}
    env.update(_APPLY_ENV)
    return env


def test_apply_cli_imports_with_only_modulo_url_and_api_key(tmp_path: Path) -> None:
    """Importing the console script needs only MODULO_URL + MODULO_API_KEY.

    The subprocess runs with ``DATABASE_URL``/``SECRET_KEY``/``FERNET_KEY``
    stripped and ``cwd`` set to an empty temp dir (so no ambient ``.env`` can
    satisfy ``Settings``). Before FAR-1586 this raised
    ``pydantic ValidationError`` while importing ``modulo.cli.migrate_org``.
    """
    result = subprocess.run(  # noqa: S603 — test driver
        [sys.executable, "-c", _APPLY_IMPORT_SCRIPT],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=tmp_path,
        env=_child_env(),
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "APPLY_CONSTRUCTED_WITHOUT_DB_SETTINGS" in result.stdout


def test_settings_validation_is_unchanged_without_db_or_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAR-1586 defers the settings read — it does not weaken it."""
    for name in _DB_OR_SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    message = str(excinfo.value)
    assert "database_url" in message
    assert "secret_key" in message
    assert "fernet_key" in message


def _uncache_lazy_attr(name: str) -> ModuleType:
    """Return ``modulo.cli`` with ``name`` un-cached.

    PEP 562 only invokes ``__getattr__`` when normal attribute lookup misses,
    so a name already cached in the package namespace would bypass the very
    code path under test. Each test uncaches ONLY the name it exercises: the
    ``main`` re-export deliberately collides with the ``modulo.cli.main``
    submodule, so popping it would clobber that submodule attribute for later
    tests.
    """
    cli_package.__dict__.pop(name, None)
    return cli_package


def test_lazy_getattr_imports_and_caches_on_first_access() -> None:
    """A lazy re-export is imported on first access and then cached flat."""
    from modulo.cli.migrate_org import build_parser as migrate_org_build_parser

    package = _uncache_lazy_attr("build_parser")
    assert package.build_parser is migrate_org_build_parser
    # Subsequent lookups are served from the package namespace, not __getattr__.
    assert vars(package)["build_parser"] is migrate_org_build_parser


def test_lazy_getattr_raises_attribute_error_for_unknown_name() -> None:
    """An unknown name is a genuine AttributeError, never a KeyError leak."""
    unknown_name = "not_a_cli_symbol"
    with pytest.raises(AttributeError, match=f"has no attribute '{unknown_name}'"):
        getattr(cli_package, unknown_name)


def test_dir_exposes_lazy_reexports() -> None:
    """``__dir__`` advertises the lazy names before they are imported."""
    names = dir(cli_package)
    for expected in ("break_glass_cli", "build_parser", "cmd_export", "cmd_import", "main"):
        assert expected in names
