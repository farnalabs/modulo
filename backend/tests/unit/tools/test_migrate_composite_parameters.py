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
