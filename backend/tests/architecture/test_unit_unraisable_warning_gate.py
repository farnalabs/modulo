"""Architecture guard: the unit suite's PytestUnraisableExceptionWarning gate.

A leaked never-awaited coroutine (a bare ``AsyncMock`` whose async children are
read synchronously) only surfaces at garbage collection as
``PytestUnraisableExceptionWarning``. At the ``default`` action the resulting
multi-line warnings summary can grow large enough to wedge pytest's terminal
writer (``BlockingIOError: [Errno 11] write could not complete without
blocking``) and fail a CI step with no test named - the 2026-10-21
merge-queue outage. The unit suite now escalates the class to an ERROR from
``tests/unit/conftest.py::pytest_runtest_setup``, and the CI unit command
carries the matching ``-W`` flag for defence in depth.

The hook is per-ITEM (``pytest_runtest_setup``), not ``pytest_configure``: the
warnings plugin re-applies the ini/cmdline ``filterwarnings`` around every
item, after configure time, so a configure-time filter is overridden by the
project-wide ``default::pytest.PytestUnraisableExceptionWarning`` entry. That
defect was observed - the class was merely printed, the run exited 0.

A gate that can be deleted silently is not a gate. These tests pin:

- the effective behaviour: for an item under ``tests/unit`` the hook lands an
  ``error`` filter ahead of the ini ``default`` and a warned
  ``PytestUnraisableExceptionWarning`` actually raises;
- the deliberate SCOPE: an item outside ``tests/unit`` (integration, BDD, a
  mixed collection) must not install the filter - those suites have not been
  audited for this class;
- EVERY pytest invocation in ``.github/scripts/coverage-thresholds.sh`` still
  carries the byte-identical escalation. The unit suite is run as shards, so
  the script passes an enumerated file list rather than a literal
  ``tests/unit/`` path - the guard therefore locates the invocation(s) by the
  ``pytest`` command itself and checks the flag per invocation, so a future
  split that drops the flag on one shard fails too.
- the ``backend-test`` job in ``.github/workflows/ci.yml`` still invokes that
  script.

Prove-it-fails: deleting the ``warnings.filterwarnings(...)`` line from
``tests/unit/conftest.py`` makes the escalation tests fail; deleting the ``-W``
flag from any pytest invocation in the coverage script makes
``test_ci_unit_command_carries_the_escalation`` fail.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import tests.unit.conftest as _unit_conftest

BACKEND = Path(__file__).resolve().parent.parent.parent
REPO = BACKEND.parent
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
COVERAGE_SCRIPT = REPO / ".github" / "scripts" / "coverage-thresholds.sh"
UNIT_ROOT = BACKEND / "tests" / "unit"

_CATEGORY = pytest.PytestUnraisableExceptionWarning

# A pytest COMMAND (not ``pytest-cov``, ``pytest.PytestUnraisableExceptionWarning``
# or a prose mention): the word followed by whitespace.
_PYTEST_COMMAND = re.compile(r"(?<![\w./-])pytest\s")


def _item_at(path: Path) -> SimpleNamespace:
    """Minimal ``pytest.Item`` stand-in exposing only ``path``.

    ``pytest_runtest_setup`` reads nothing else, which is itself part of the
    contract - a hook that needed a live Item could not be exercised here.
    """
    return SimpleNamespace(path=path)


def _apply_hook(item: SimpleNamespace) -> None:
    _unit_conftest.pytest_runtest_setup(item)  # type: ignore[arg-type]


def _has_error_filter() -> bool:
    return any(
        action == "error" and category is not None and issubclass(_CATEGORY, category)
        for action, _message, category, _module, _lineno in warnings.filters
    )


def test_unit_item_escalates_unraisable_warning() -> None:
    """An item under ``tests/unit`` turns the warning class into an error."""
    baseline = warnings.filters[:]
    try:
        _apply_hook(_item_at(UNIT_ROOT / "api" / "test_audit.py"))
        assert _has_error_filter()
        with pytest.raises(_CATEGORY):
            warnings.warn(_CATEGORY("probe: leaked coroutine"), stacklevel=1)
    finally:
        warnings.filters[:] = baseline


def test_escalation_wins_over_the_ini_default_entry() -> None:
    """The error filter is installed ahead of the project-wide ``default`` entry.

    pytest re-applies the ini list around each item, so the hook's filter must
    sit at the front for the class to actually fail. Model the ini state that
    ``catch_warnings_for_item`` installs, then apply the hook.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("default", _CATEGORY)
        _apply_hook(_item_at(UNIT_ROOT / "conftest.py"))
        assert warnings.filters[0][0] == "error"
        assert warnings.filters[0][2] is _CATEGORY
        with pytest.raises(_CATEGORY):
            warnings.warn(_CATEGORY("probe: leaked coroutine"), stacklevel=1)


def test_root_level_unit_file_is_escalated() -> None:
    """A file directly under ``tests/unit`` (not a package) is in scope too."""
    baseline = warnings.filters[:]
    try:
        _apply_hook(_item_at(UNIT_ROOT / "test_dispatch.py"))
        assert _has_error_filter()
    finally:
        warnings.filters[:] = baseline


def test_non_unit_item_does_not_install_the_filter() -> None:
    """Integration/BDD are deliberately out of scope - the hook must decline."""
    baseline = warnings.filters[:]
    try:
        _apply_hook(_item_at(BACKEND / "tests" / "integration" / "test_run_crud.py"))
        assert warnings.filters == baseline
        _apply_hook(_item_at(BACKEND / "tests" / "bdd" / "steps" / "test_auth.py"))
        assert warnings.filters == baseline
        _apply_hook(_item_at(BACKEND / "tests" / "architecture" / "test_workflow_structure.py"))
        assert warnings.filters == baseline
    finally:
        warnings.filters[:] = baseline


def test_escalation_constant_matches_the_pytest_filter_spelling() -> None:
    """The shared constant is the exact ``-W`` string the CI command carries."""
    assert _unit_conftest.UNRAISABLE_ESCALATION == "error::pytest.PytestUnraisableExceptionWarning"


def _active_script_lines() -> list[str]:
    """The coverage script's executable lines, comments excluded."""
    script = COVERAGE_SCRIPT.read_text(encoding="utf-8")
    return [line for line in script.splitlines() if not line.lstrip().startswith("#")]


def _pytest_invocations(script: str) -> list[str]:
    """Every pytest command in ``script``, as one joined logical line each.

    Continuation lines are joined first: the escalation flag sits on the
    backslash-continued line after the command, so a physical-line scan would
    see the invocation and its flag as separate lines. Comment lines are
    excluded - the script's explanatory comments mention ``pytest``.

    The invocation is located by the ``pytest`` command itself, never by a
    literal ``tests/unit/`` path: the unit suite is passed as an enumerated
    file list, so that literal no longer appears.
    """
    invocations = []
    for line in script.replace("\\\n", " ").splitlines():
        if line.lstrip().startswith("#"):
            continue
        if _PYTEST_COMMAND.search(line):
            invocations.append(line.strip())
    return invocations


def test_ci_unit_command_carries_the_escalation() -> None:
    """Every CI unit pytest invocation carries the same escalation as the hook.

    Since FAR-2081 the unit suite runs as sequential shards over an explicit
    file list, so the pytest line no longer spells ``tests/unit`` itself. What
    must still hold: the file list is enumerated from ``tests/unit`` (a
    different root would silently shrink the suite), pytest is invoked over
    that list, and EVERY such invocation carries the byte-identical escalation.
    """
    active = _active_script_lines()
    assert any("find tests/unit" in line for line in active), (
        "the coverage script no longer enumerates unit test files under tests/unit"
    )
    shard_invocations = [line for line in active if "pytest" in line and "${files[@]}" in line]
    assert shard_invocations, "no `pytest` invocation over the unit file list found in the coverage script"
    script = COVERAGE_SCRIPT.read_text(encoding="utf-8")
    # The invocation is a backslash-continued command, so join the continuation
    # lines before looking for the flag.
    joined = script.replace("\\\n", " ")
    assert _unit_conftest.UNRAISABLE_ESCALATION in joined
    # ...but the whole-script check above only proves the flag appears SOMEWHERE.
    # Check every invocation, so a future split that drops the flag from one
    # shard fails here even though that check would still pass.
    invocations = _pytest_invocations(script)
    assert invocations, "no pytest invocation found in .github/scripts/coverage-thresholds.sh"
    offenders = [line for line in invocations if _unit_conftest.UNRAISABLE_ESCALATION not in line]
    assert all(_unit_conftest.UNRAISABLE_ESCALATION in line for line in invocations), (
        f"every pytest invocation must carry {_unit_conftest.UNRAISABLE_ESCALATION!r}; "
        "missing on:\n" + "\n".join(offenders)
    )


def test_ci_unit_shard_split_is_lossless() -> None:
    """Both shards together run every collected unit test file.

    A split that drops files would also drop their coverage while the combined
    report still claims to describe the whole suite.
    """
    active = "\n".join(_active_script_lines())
    # pytest's default ``python_files`` patterns - pyproject.toml does not
    # override them - so the enumeration must match BOTH or the split is lossy.
    assert "test_*.py" in active, "the shard enumeration must match pytest's default python_files patterns"
    assert "*_test.py" in active, "the shard enumeration must match pytest's default python_files patterns"
    assert "SHARD1_FILES=(" in active, "the coverage script no longer builds two shard file lists"
    assert "SHARD2_FILES=(" in active, "the coverage script no longer builds two shard file lists"
    assert active.count("run_shard ") >= 2, "the coverage script no longer runs two shards"


def test_ci_backend_test_job_runs_the_unit_coverage_script() -> None:
    """The ``backend-test`` job still delegates the unit run to the pinned script."""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "backend-test" in jobs
    runs = [step.get("run", "") for step in jobs["backend-test"]["steps"] if isinstance(step, dict)]
    assert any("coverage-thresholds.sh" in run for run in runs)
