"""Unit tests for the new helpers in backend/tools/migrate_composite_parameters.py."""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

for parent in Path(__file__).resolve().parents:
    script_path = parent / "backend" / "tools" / "migrate_composite_parameters.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find backend/tools/migrate_composite_parameters.py")

_loader = SourceFileLoader("migrate_composite_parameters", str(script_path))
mod = module_from_spec(spec_from_loader("migrate_composite_parameters", _loader))
_loader.exec_module(mod)


class _BeginCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self):
        self.added = []
        self.executed = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def begin(self):
        return _BeginCM()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def execute(self, stmt, params):
        self.executed = (stmt, params)


def _make_session_factory():
    holder: dict = {}

    def _factory():
        session = _FakeSession()
        holder["session"] = session
        return session

    return _factory, holder


def test_default_values_picks_non_null_defaults():
    ports = [
        {"name": "a", "default_value": 1},
        {"name": "b", "default_value": None},
        {"name": "c", "type": "str"},  # no default_value key
        {"name": "d", "default_value": "x"},
    ]
    assert mod._default_values(ports) == {"a": 1, "d": "x"}


def test_default_values_empty():
    assert not mod._default_values([])


def test_print_template_details(capsys):
    ports = [{"name": "p1", "type": "str"}, {"name": "p2", "type": "int"}]
    mod._print_template_details("tpl", "id-1", "tpl Parameters", ports)
    out = capsys.readouterr().out
    assert "tpl" in out
    assert "p1" in out
    assert "p2" in out


async def test_migrate_row_empty_ports_short_circuits():
    args = SimpleNamespace(verbose=False, dry_run=False)
    session_factory = MagicMock()
    row = {
        "id": "t1",
        "name": "tpl",
        "organisation_id": "o1",
        "account_id": "a1",
        "parameter_ports_json": [],
    }
    result = await mod._migrate_row(args, session_factory, row)
    assert result == (0, 0, 0)
    session_factory.assert_not_called()


async def test_migrate_row_dry_run_counts_without_migrating():
    args = SimpleNamespace(verbose=False, dry_run=True)
    session_factory = MagicMock()
    row = {
        "id": "t1",
        "name": "tpl",
        "organisation_id": "o1",
        "account_id": "a1",
        "parameter_ports_json": [{"name": "x"}],
    }
    result = await mod._migrate_row(args, session_factory, row)
    assert result == (0, 1, 0)
    session_factory.assert_not_called()


async def test_migrate_one_template_creates_schema_and_set():
    session_factory, holder = _make_session_factory()
    await mod._migrate_one_template(
        session_factory,
        "t1",
        "tpl",
        "org1",
        "acct1",
        "tpl Parameters",
        [{"name": "x", "default_value": 1}],
    )
    # Two persisted objects (ParameterSchema + ParameterSet) were added.
    assert len(holder["session"].added) == 2
    # The UPDATE against composite_templates ran.
    assert holder["session"].executed is not None


async def test_migrate_row_success_path_migrates():
    args = SimpleNamespace(verbose=False, dry_run=False)
    session_factory, holder = _make_session_factory()
    row = {
        "id": "t1",
        "name": "tpl",
        "organisation_id": "o1",
        "account_id": "a1",
        "parameter_ports_json": [{"name": "x", "default_value": 1}],
    }
    result = await mod._migrate_row(args, session_factory, row)
    assert result == (1, 0, 0)
    assert len(holder["session"].added) == 2


async def test_migrate_row_verbose_prints_details(capsys):
    args = SimpleNamespace(verbose=True, dry_run=False)
    session_factory, _holder = _make_session_factory()
    row = {
        "id": "t1",
        "name": "tpl",
        "organisation_id": "o1",
        "account_id": "a1",
        "parameter_ports_json": [{"name": "x", "default_value": 1}],
    }
    result = await mod._migrate_row(args, session_factory, row)
    assert result == (1, 0, 0)
    out = capsys.readouterr().out
    assert "tpl" in out


async def test_migrate_row_error_path_records_error(monkeypatch):
    args = SimpleNamespace(verbose=False, dry_run=False)
    session_factory, _ = _make_session_factory()

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(mod, "_migrate_one_template", _boom)
    row = {
        "id": "t1",
        "name": "tpl",
        "organisation_id": "o1",
        "account_id": "a1",
        "parameter_ports_json": [{"name": "x", "default_value": 1}],
    }
    result = await mod._migrate_row(args, session_factory, row)
    assert result == (0, 0, 1)
