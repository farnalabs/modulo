"""Architecture test: the BDD coverage checker stays honest.

``tests/bdd/check-bdd-coverage.py`` enforces that every ``.feature`` file is
referenced by at least one ``scenarios(...)`` call in a step file. Its
resolver is easy to break silently — a wrong relative path, a missed
co-located step directory, or a dead reporting loop that swallows the
diagnostics — so these tests pin the scanning and reporting behaviour.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent.parent
CHECKER = BACKEND / "tests" / "bdd" / "check-bdd-coverage.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("bdd_coverage_checker", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_scenarios(step_file: Path, *feature_refs: str) -> None:
    step_file.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"scenarios({ref!r})" for ref in feature_refs)
    step_file.write_text(f"from pytest_bdd import scenarios\n{body}\n", encoding="utf-8")


def _write_feature(feature_file: Path, content: str = "Feature: X\n") -> None:
    feature_file.parent.mkdir(parents=True, exist_ok=True)
    feature_file.write_text(content, encoding="utf-8")


def test_collect_covered_features_resolves_shared_and_colocated_steps(tmp_path, monkeypatch):
    """References in both the shared ``steps/`` directory (via ``../features/...``)
    and co-located step files under ``features/<subdir>/`` must resolve."""
    module = _load_checker()
    features = tmp_path / "features"
    steps = tmp_path / "steps"
    _write_feature(features / "a.feature")
    _write_feature(features / "sub" / "b.feature")

    _write_scenarios(steps / "test_shared.py", "../features/sub/b.feature")
    _write_scenarios(features / "sub" / "test_colocated.py", "../a.feature")

    monkeypatch.setattr(module, "FEATURES_DIR", features)
    monkeypatch.setattr(module, "STEPS_DIR", steps)

    assert module.collect_covered_features() == {"a.feature", "sub/b.feature"}


def test_main_returns_zero_when_all_covered(tmp_path, monkeypatch):
    module = _load_checker()
    features = tmp_path / "features"
    steps = tmp_path / "steps"
    _write_feature(features / "a.feature")

    _write_scenarios(steps / "test_shared.py", "../features/a.feature")

    monkeypatch.setattr(module, "FEATURES_DIR", features)
    monkeypatch.setattr(module, "STEPS_DIR", steps)

    assert module.main() == 0


def test_main_reports_uncovered_features_to_stderr(tmp_path, monkeypatch, capsys):
    """A coverage failure must name the offending feature files instead of
    silently returning a bare exit code 1."""
    module = _load_checker()
    features = tmp_path / "features"
    steps = tmp_path / "steps"
    _write_feature(features / "covered.feature")
    _write_feature(features / "orphan.feature")

    _write_scenarios(steps / "test_shared.py", "../features/covered.feature")

    monkeypatch.setattr(module, "FEATURES_DIR", features)
    monkeypatch.setattr(module, "STEPS_DIR", steps)

    assert module.main() == 1
    err = capsys.readouterr().err
    assert "orphan.feature" in err
    assert "covered.feature" not in err
