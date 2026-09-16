"""Unit tests for scripts/run_affected_tests.py selection + suite grouping.

Guards two regressions in the pre-push affected-tests gate:

1. Over-selection by stem: ``backend/src/api/me.py`` must not glob-match
   ``test_metrics_ingest.py`` (bare ``test_{stem}*`` prefix matching used to).
2. Cross-suite process mixing: suites (unit / integration / bdd) must be run
   as SEPARATE pytest processes — ``tests/bdd/conftest.py`` sets
   ``DATABASE_URL``/``MODULO_DB`` at import time, and when a BDD file and an
   integration file ran in one process, the BDD env leaked into the
   integration tests (sqlite engine vs testcontainers Postgres). The runner's
   grouping loop maps each suite to one ``_run_pytest`` call, so the output
   of ``_group_by_suite`` directly determines the process split.
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_affected_tests.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_affected_tests.py)")

_loader = SourceFileLoader("run_affected_tests", str(script_path))
_spec = spec_from_loader("run_affected_tests", _loader)
assert _spec is not None
mod = module_from_spec(_spec)
_loader.exec_module(mod)


def _make_backend_tree(tmp_path: Path) -> None:
    (tmp_path / "backend" / "tests" / "unit" / "api").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "integration").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "bdd" / "steps").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "api").mkdir(parents=True, exist_ok=True)


def test_me_stem_matches_genuinely_affected_tests(tmp_path, monkeypatch):
    _make_backend_tree(tmp_path)
    for name in (
        "test_me.py",
        "test_me_password.py",
        "test_me_hitl_email_preferences.py",
        "test_metrics_ingest.py",
    ):
        (tmp_path / "backend" / "tests" / "unit" / "api" / name).touch()
    (tmp_path / "backend" / "src" / "api" / "me.py").touch()

    monkeypatch.chdir(tmp_path)
    selected = mod._select_test_files(["backend/src/api/me.py"])

    assert sorted(Path(p).name for p in selected) == [
        "test_me.py",
        "test_me_hitl_email_preferences.py",
        "test_me_password.py",
    ]


def test_metrics_only_tree_selects_nothing_for_me_change(tmp_path, monkeypatch):
    _make_backend_tree(tmp_path)
    (tmp_path / "backend" / "tests" / "integration" / "test_metrics_ingest.py").touch()
    (tmp_path / "backend" / "src" / "api" / "me.py").touch()

    monkeypatch.chdir(tmp_path)
    selected = mod._select_test_files(["backend/src/api/me.py"])

    assert not selected


def test_changed_test_files_still_selected_directly(tmp_path, monkeypatch):
    _make_backend_tree(tmp_path)
    test_file = "backend/tests/unit/api/test_me_password.py"
    (tmp_path / test_file).touch()

    monkeypatch.chdir(tmp_path)
    selected = mod._select_test_files([test_file])

    assert test_file in selected


def test_suite_grouping_separates_bdd_from_integration():
    paths = [
        "backend/tests/bdd/steps/test_email_settings.py",
        "backend/tests/integration/test_metrics_ingest.py",
        "backend/tests/unit/api/test_me_password.py",
    ]

    grouped = mod._group_by_suite(paths)

    assert sorted(grouped) == ["bdd", "integration", "unit"]
    assert grouped["bdd"] == ["backend/tests/bdd/steps/test_email_settings.py"]
    assert grouped["integration"] == ["backend/tests/integration/test_metrics_ingest.py"]
    assert grouped["unit"] == ["backend/tests/unit/api/test_me_password.py"]


def test_suite_grouping_covers_integration_bdd_subdir_as_integration():
    grouped = mod._group_by_suite(["backend/tests/integration/bdd/test_x.py"])

    assert grouped == {"integration": ["backend/tests/integration/bdd/test_x.py"]}


def test_grouping_loop_runs_one_pytest_process_per_suite(monkeypatch):
    """Each suite group maps to exactly one _run_pytest call, mirroring CI timeouts."""
    calls: list[tuple[list[str], int]] = []

    def fake_run(paths, timeout=120):
        calls.append((paths, timeout))
        return 0

    monkeypatch.setattr(mod, "_run_pytest", fake_run)

    test_paths = [
        "backend/tests/bdd/steps/test_email_settings.py",
        "backend/tests/integration/test_metrics_ingest.py",
        "backend/tests/unit/api/test_me_password.py",
    ]
    grouped = mod._group_by_suite(test_paths)
    worst = 0
    for suite, paths in grouped.items():
        code = mod._run_pytest(paths, timeout=mod.SUITE_TIMEOUTS.get(suite, 120))
        if code != 0:
            worst = code

    assert worst == 0
    assert len(calls) == 3
    for paths, _timeout in calls:
        assert len(paths) == 1
    integration_calls = [c for c in calls if c[0][0].startswith("backend/tests/integration")]
    assert integration_calls[0][1] == 300


def test_main_propagates_worst_group_exit_code(monkeypatch):
    monkeypatch.setattr(mod, "_collect_changed_files", lambda base: ["backend/tests/unit/api/x.py"])
    monkeypatch.setattr(mod, "_select_test_files", lambda changed: ["backend/tests/unit/api/x.py"])
    monkeypatch.setattr(mod, "_group_by_suite", lambda paths: {"unit": paths})
    monkeypatch.setattr(mod, "_run_pytest", lambda paths, timeout: 3)

    rc = mod.main(["--base", "origin/main"])

    assert rc == 3


def test_matches_stem_word_boundary():
    assert mod._matches_stem("test_me.py", "me")
    assert mod._matches_stem("test_me_password.py", "me")
    assert not mod._matches_stem("test_metrics_ingest.py", "me")
    assert mod._matches_stem("test_me_2023.py", "me")
