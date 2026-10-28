"""Coverage tests for the small Fly deployment helper scripts."""

from __future__ import annotations

import asyncio
import importlib.util
import runpy
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEPLOY = _REPO_ROOT / "deploy" / "fly"


def _load_script_with_fake_asyncpg(monkeypatch: pytest.MonkeyPatch, filename: str) -> tuple[Any, list[Any]]:
    """Load a script while preventing its import-time asyncio.run call."""
    connects: list[Any] = []

    class FakeAsyncpg:
        async def connect(self, *args: object, **kwargs: object) -> Any:
            connects.append((args, kwargs))
            return _FakeConnection()

    class FakeConnection:
        async def execute(self, query: str) -> str:
            return query

        async def fetch(self, query: str) -> list[dict[str, object]]:
            return []

        async def close(self) -> None:
            return None

    class FakeAsyncio:
        @staticmethod
        def run(coro: Any) -> None:
            coro.close()

    fake_asyncpg = FakeAsyncpg()
    original_asyncpg = sys.modules.get("asyncpg")
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
    original_asyncio_run = asyncio.run
    monkeypatch.setattr(asyncio, "run", FakeAsyncio.run)
    try:
        spec = importlib.util.spec_from_file_location(filename, _DEPLOY / filename)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        monkeypatch.setattr(asyncio, "run", original_asyncio_run)
        if original_asyncpg is not None:
            monkeypatch.setitem(sys.modules, "asyncpg", original_asyncpg)
        else:
            monkeypatch.delitem(sys.modules, "asyncpg", raising=False)
    return module, connects


class _FakeConnection:
    async def execute(self, query: str) -> str:
        return query

    async def fetch(self, query: str) -> list[dict[str, object]]:
        return []

    async def close(self) -> None:
        return None


def test_bootstrap_creates_table_with_normalized_url_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, connects = _load_script_with_fake_asyncpg(monkeypatch, "bootstrap.py")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:password@db.internal:5432/modulo?sslmode=disable")

    asyncio.run(module.main())

    assert connects == [(("postgres://user:password@db.internal:5432/modulo",), {"ssl": False})]
    output = capsys.readouterr().out
    assert "Connecting to: postgres://user:password@db.internal:5432/modulo" in output
    assert "Created alembic_version table" in output


def test_bootstrap_propagates_connection_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    module, _connects = _load_script_with_fake_asyncpg(monkeypatch, "bootstrap.py")

    async def fail_connect(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(module.asyncpg, "connect", fail_connect)
    monkeypatch.setenv("DATABASE_URL", "postgres://user:password@db.internal/modulo")

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(module.main())


def test_check_users_prints_count_and_rows(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    module, connects = _load_script_with_fake_asyncpg(monkeypatch, "check_users.py")
    module.url = "postgres://user:password@db.internal:5432/modulo"

    class Connection(_FakeConnection):
        async def fetch(self, query: str) -> list[dict[str, object]]:
            if query.startswith("SELECT COUNT"):
                return [{"cnt": 2}]
            return [
                {"email": "a@example.test", "org_role": "admin"},
                {"email": "b@example.test", "org_role": "viewer"},
            ]

    async def connect(*args: object, **kwargs: object) -> Connection:
        connects.append((args, kwargs))
        return Connection()

    monkeypatch.setattr(module.asyncpg, "connect", connect)
    asyncio.run(module.main())

    output = capsys.readouterr().out
    assert "Users: 2" in output
    assert "a@example.test (admin)" in output
    assert "b@example.test (viewer)" in output
    assert connects == [((module.url,), {})]


def test_check_users_prints_count_without_listing_when_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module, _connects = _load_script_with_fake_asyncpg(monkeypatch, "check_users.py")
    module.url = "postgres://user:password@db.internal:5432/modulo"

    class EmptyConnection(_FakeConnection):
        async def fetch(self, query: str) -> list[dict[str, object]]:
            assert query.startswith("SELECT COUNT")
            return [{"cnt": 0}]

    async def connect(*_args: object, **_kwargs: object) -> EmptyConnection:
        return EmptyConnection()

    monkeypatch.setattr(module.asyncpg, "connect", connect)
    asyncio.run(module.main())

    output = capsys.readouterr().out
    assert "Users: 0" in output
    assert "SELECT email" not in output


def test_check_users_closes_connection_after_query_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module, _connects = _load_script_with_fake_asyncpg(monkeypatch, "check_users.py")
    module.url = "postgres://user:password@db.internal:5432/modulo"
    closed = False

    class BrokenConnection(_FakeConnection):
        async def fetch(self, _query: str) -> list[dict[str, object]]:
            raise RuntimeError("query failed")

        async def close(self) -> None:
            nonlocal closed
            closed = True

    async def connect(*_args: object, **_kwargs: object) -> BrokenConnection:
        return BrokenConnection()

    monkeypatch.setattr(module.asyncpg, "connect", connect)
    asyncio.run(module.main())

    assert "Error: query failed" in capsys.readouterr().out
    assert closed


def test_bootstrap_db_shim_reexports_promoted_module() -> None:
    spec = importlib.util.spec_from_file_location("bootstrap_db_test", _DEPLOY / "bootstrap_db.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    import modulo.db.bootstrap as promoted

    assert module.main is promoted.main
    assert module.__all__ == ["asyncio", "derive_system_database_url", "fix_database_url", "main"]


def test_extract_machine_id_cli_success_and_failure() -> None:
    script = _DEPLOY / "extract_machine_id.py"
    success = subprocess.run(  # noqa: S603 — fixed interpreter and repository script
        [sys.executable, str(script)],
        input="launching\n Machine ID: abc123\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert success.returncode == 0
    assert success.stdout == "abc123"

    failure = subprocess.run(  # noqa: S603 — fixed interpreter and repository script
        [sys.executable, str(script)],
        input="launch failed\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert failure.returncode == 1
    assert not failure.stdout
    assert not failure.stderr


def test_extract_machine_id_main_reads_stdin_and_writes_id(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("extract_machine_id_main", _DEPLOY / "extract_machine_id.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module.sys, "stdin", types.SimpleNamespace(read=lambda: "Machine ID: cli123"))
    output: Any = types.SimpleNamespace(written="", write=lambda value: setattr(output, "written", value))
    monkeypatch.setattr(module.sys, "stdout", output)

    assert module.main() == 0
    assert output.written == "cli123"


def test_bootstrap_db_script_entrypoint_calls_promoted_main(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.db.bootstrap as promoted

    called: list[bool] = []
    monkeypatch.setattr(promoted, "main", lambda: called.append(True))
    runpy.run_path(str(_DEPLOY / "bootstrap_db.py"), run_name="__main__")

    assert called == [True]


def test_extract_machine_id_main_returns_error_without_id(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("extract_machine_id_missing", _DEPLOY / "extract_machine_id.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module.sys, "stdin", types.SimpleNamespace(read=lambda: "launch failed"))

    assert module.main() == 1


def test_extract_machine_id_script_entrypoint_exits_with_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(read=lambda: "Machine ID: entry123"))
    output: Any = types.SimpleNamespace(written="", write=lambda value: setattr(output, "written", value))
    monkeypatch.setattr(sys, "stdout", output)
    with pytest.raises(SystemExit, match="0"):
        runpy.run_path(str(_DEPLOY / "extract_machine_id.py"), run_name="__main__")
    assert output.written == "entry123"


def test_bootstrap_db_main_is_callable_without_running_at_import() -> None:
    spec = importlib.util.spec_from_file_location("bootstrap_db_callable", _DEPLOY / "bootstrap_db.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.main)
    assert isinstance(module.asyncio, types.ModuleType)
