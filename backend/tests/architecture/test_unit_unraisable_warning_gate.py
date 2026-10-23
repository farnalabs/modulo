"""Architecture guard: the audited suites' PytestUnraisableExceptionWarning gate.

A leaked never-awaited coroutine (a bare ``AsyncMock`` whose async children are
read synchronously) or an unclosed connection/socket only surfaces at garbage
collection as ``PytestUnraisableExceptionWarning``. At the ``default`` action
the resulting multi-line warnings summary can grow large enough to wedge
pytest's terminal writer (``BlockingIOError: [Errno 11] write could not
complete without blocking``) and fail a CI step with no test named - the
2026-10-21 merge-queue outage. The audited suites now escalate the class to an
ERROR from ``tests/conftest.py::pytest_runtest_setup``, and every workflow
command that NAMES a gated-suite path carries the matching ``-W`` flag for
defence in depth. The one command that runs gated tests without naming them -
``ci.yml`` integration-changed, which expands a computed file list - is pinned
explicitly by its ``${TEST_FILES[@]}`` needle; a command that names no gated
path cannot be identified statically and inherits the in-process hook only.

The hook is per-ITEM (``pytest_runtest_setup``), not ``pytest_configure``: the
warnings plugin re-applies the ini/cmdline ``filterwarnings`` around every
item, after configure time, so a configure-time filter is overridden by the
project-wide ``default::pytest.PytestUnraisableExceptionWarning`` entry. That
defect was observed - the class was merely printed, the run exited 0.

The hook lives in the ROOT conftest rather than a suite conftest because pytest
only loads a directory's conftest when that directory is on the collection
path: ``tests/unit/conftest.py`` is never imported by a ``pytest
tests/integration`` or ``pytest tests/bdd`` run, so a hook there cannot gate
those suites.

A gate that can be deleted silently is not a gate. These tests pin:

- the effective behaviour: for an item in a gated suite (``unit``,
  ``integration``, ``bdd``) the hook lands an ``error`` filter ahead of the ini
  ``default`` and a warned ``PytestUnraisableExceptionWarning`` actually raises;
- the deliberate SCOPE: an item in an UNAUDITED suite (``architecture``,
  ``conformance``, ``connectors``, ``determination``, ``load``, ``docker``)
  must not install the filter;
- the exact audited set in ``GATED_SUITE_ROOTS``;
- every workflow command that names a gated-suite path still carries the
  byte-identical escalation and no longer disables the warnings plugin (unit
  coverage script, BDD workflow, main-integration workflow, deploy pre-deploy
  unit and integration steps, CI email-settings/step-rot BDD steps, CI
  integration-changed, CI tier1b-light, merge-queue unit run, schemathesis and
  Tier 1b nightly runs).

Prove-it-fails: deleting the ``warnings.filterwarnings(...)`` line from
``tests/conftest.py`` makes the escalation tests fail; deleting a suite from
``GATED_SUITE_ROOTS`` makes its parametrised case fail; dropping the ``-W`` flag
from any pinned CI command makes its command test fail, and adding a new
gated-suite command without the flag fails the exhaustive workflow scan.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import tests.conftest as _root_conftest

BACKEND = Path(__file__).resolve().parent.parent.parent
REPO = BACKEND.parent
TESTS = BACKEND / "tests"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
BDD_WORKFLOW = REPO / ".github" / "workflows" / "bdd.yml"
MAIN_INTEGRATION_WORKFLOW = REPO / ".github" / "workflows" / "main-integration.yml"
DEPLOY_WORKFLOW = REPO / ".github" / "workflows" / "deploy.yml"
MERGE_QUEUE_WORKFLOW = REPO / ".github" / "workflows" / "merge-queue.yml"
SCHEMATHESIS_WORKFLOW = REPO / ".github" / "workflows" / "schemathesis-nightly.yml"
TIER1B_NIGHTLY_WORKFLOW = REPO / ".github" / "workflows" / "tier1b-nightly.yml"
WORKFLOWS_DIR = REPO / ".github" / "workflows"
COVERAGE_SCRIPT = REPO / ".github" / "scripts" / "coverage-thresholds.sh"

_CATEGORY = pytest.PytestUnraisableExceptionWarning
_ESCALATION = "error::pytest.PytestUnraisableExceptionWarning"

#: One representative item per gated suite (the path need not exist - the hook
#: only resolves the path string).
_GATED_SAMPLES = {
    "unit": ("api", "test_audit.py"),
    "integration": ("test_run_crud.py",),
    "bdd": ("steps", "test_auth.py"),
}

#: One representative item per UNAUDITED suite.
_UNGATED_SAMPLES = {
    "architecture": ("test_workflow_structure.py",),
    "conformance": ("test_pipeline_conformance.py",),
    "connectors": ("test_connector_contract.py",),
    "determination": ("test_determination.py",),
    "load": ("test_load_profile.py",),
    "docker": ("test_docker_compose.py",),
}


def _item_at(path: Path) -> SimpleNamespace:
    """Minimal ``pytest.Item`` stand-in exposing only ``path``.

    ``pytest_runtest_setup`` reads nothing else, which is itself part of the
    contract - a hook that needed a live Item could not be exercised here.
    """
    return SimpleNamespace(path=path)


def _apply_hook(item: SimpleNamespace) -> None:
    _root_conftest.pytest_runtest_setup(item)  # type: ignore[arg-type]


def _has_error_filter() -> bool:
    return any(
        action == "error" and category is not None and issubclass(_CATEGORY, category)
        for action, _message, category, _module, _lineno in warnings.filters
    )


@pytest.mark.parametrize("suite", sorted(_GATED_SAMPLES))
def test_gated_suite_item_escalates_unraisable_warning(suite: str) -> None:
    """An item in each audited suite turns the warning class into an error."""
    baseline = warnings.filters[:]
    try:
        _apply_hook(_item_at(TESTS.joinpath(suite, *_GATED_SAMPLES[suite])))
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
        _apply_hook(_item_at(TESTS / "unit" / "conftest.py"))
        assert warnings.filters[0][0] == "error"
        assert warnings.filters[0][2] is _CATEGORY
        with pytest.raises(_CATEGORY):
            warnings.warn(_CATEGORY("probe: leaked coroutine"), stacklevel=1)


def test_root_level_gated_file_is_escalated() -> None:
    """A file directly under a gated root (not a package) is in scope too."""
    baseline = warnings.filters[:]
    try:
        _apply_hook(_item_at(TESTS / "unit" / "test_dispatch.py"))
        assert _has_error_filter()
        _apply_hook(_item_at(TESTS / "integration" / "test_run_crud.py"))
        assert _has_error_filter()
    finally:
        warnings.filters[:] = baseline


@pytest.mark.parametrize("suite", sorted(_UNGATED_SAMPLES))
def test_unaudited_suite_item_does_not_install_the_filter(suite: str) -> None:
    """Every unaudited suite is deliberately out of scope - the hook declines."""
    baseline = warnings.filters[:]
    try:
        _apply_hook(_item_at(TESTS.joinpath(suite, *_UNGATED_SAMPLES[suite])))
        assert warnings.filters == baseline
    finally:
        warnings.filters[:] = baseline


def test_gated_suite_roots_are_the_verified_set() -> None:
    """The audited set is exactly unit + integration + BDD.

    Widening the gate is a claim that a suite was audited under the escalation
    and ran green; that must be a deliberate edit here, never a side effect.
    """
    assert tuple(_root_conftest.GATED_SUITE_ROOTS) == ("unit", "integration", "bdd")


def test_escalation_constant_matches_the_pytest_filter_spelling() -> None:
    """The shared constant is the exact ``-W`` string the CI commands carry."""
    assert _root_conftest.UNRAISABLE_ESCALATION == _ESCALATION


def _workflow_run_commands(path: Path) -> Iterator[str]:
    """Every ``run:`` command in a workflow file, across all jobs/steps."""
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                yield step["run"]


@pytest.mark.parametrize(
    ("workflow", "needle"),
    [
        (BDD_WORKFLOW, "pytest tests/bdd/"),
        (MAIN_INTEGRATION_WORKFLOW, "pytest tests/integration/ -m integration"),
        (DEPLOY_WORKFLOW, "pytest tests/integration/ -m integration"),
        (DEPLOY_WORKFLOW, "pytest tests/unit/"),
        (CI_WORKFLOW, "pytest tests/integration/test_break_glass_gate.py"),
        (CI_WORKFLOW, "pytest tests/bdd/steps/test_email_settings.py"),
        (CI_WORKFLOW, "pytest tests/bdd/steps/test_auth.py"),
        (CI_WORKFLOW, "tests/integration/test_tier1b_connectors_light.py"),
        # integration-changed expands a computed file list, so it names no
        # gated path - pin it by the expansion itself.
        (CI_WORKFLOW, 'pytest "${TEST_FILES[@]}"'),
        (MERGE_QUEUE_WORKFLOW, "pytest tests/unit/"),
        (SCHEMATHESIS_WORKFLOW, "pytest tests/integration/test_schemathesis.py"),
        (TIER1B_NIGHTLY_WORKFLOW, "tests/integration/test_tier1b_connectors_heavy.py"),
    ],
    ids=[
        "bdd",
        "main-integration",
        "deploy-integration",
        "deploy-unit",
        "ci-break-glass",
        "ci-bdd-email-settings",
        "ci-bdd-step-rot",
        "ci-tier1b-light",
        "ci-integration-changed",
        "merge-queue-unit",
        "schemathesis-nightly",
        "tier1b-nightly",
    ],
)
def test_ci_command_for_a_gated_suite_carries_the_escalation(workflow: Path, needle: str) -> None:
    """Every pinned CI command that runs a gated suite carries the escalation."""
    commands = [command for command in _workflow_run_commands(workflow) if needle in command]
    assert commands, f"no `{needle}` command found in {workflow.name}"
    for command in commands:
        assert _ESCALATION in command, f"{workflow.name} command lost the escalation: {command}"
        assert "-p no:warnings" not in command, f"{workflow.name} command disables the warnings plugin: {command}"
        assert "--disable-warnings" in command, f"{workflow.name} command lacks --disable-warnings: {command}"


def _without_shell_comments(command: str) -> str:
    """Drop ``#`` comment lines so a path mentioned in prose is not a command.

    The integration-changed Detect step's comments name
    ``backend/tests/integration/`` while the step runs no pytest; scanning the
    raw text would treat it as a gated-suite command.
    """
    return "\n".join(line for line in command.splitlines() if not line.lstrip().startswith("#"))


def test_every_workflow_command_naming_a_gated_suite_carries_the_escalation() -> None:
    """No workflow may run a gated-suite path without the escalation.

    This is the exhaustive backstop behind the docstring's universal claim: the
    parametrised pins are per-command and can be forgotten when a new command is
    added, whereas this scan fails as soon as one is.
    """
    gated_commands = [
        (workflow.name, command)
        for workflow in sorted(WORKFLOWS_DIR.glob("*.yml"))
        for command in _workflow_run_commands(workflow)
        if "pytest" in _without_shell_comments(command)
        and any(f"tests/{suite}" in _without_shell_comments(command) for suite in _root_conftest.GATED_SUITE_ROOTS)
    ]
    assert len(gated_commands) >= 10, f"expected the gated-suite command set, found {len(gated_commands)}"
    for workflow_name, command in gated_commands:
        assert _ESCALATION in command, f"{workflow_name} command lost the escalation: {command}"
        assert "-p no:warnings" not in command, f"{workflow_name} command disables the warnings plugin: {command}"


def test_ci_unit_command_carries_the_escalation() -> None:
    """The CI unit pytest invocation carries the same escalation as the hook."""
    script = COVERAGE_SCRIPT.read_text(encoding="utf-8")
    unit_invocations = [
        line for line in script.splitlines() if "pytest tests/unit/" in line and not line.lstrip().startswith("#")
    ]
    assert unit_invocations, "no `pytest tests/unit/` invocation found in the coverage script"
    # The invocation is a backslash-continued command, so join the continuation
    # lines before looking for the flag.
    joined = script.replace("\\\n", " ")
    assert _ESCALATION in joined


def test_ci_backend_test_job_runs_the_unit_coverage_script() -> None:
    """The ``backend-test`` job still delegates the unit run to the pinned script."""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "backend-test" in jobs
    runs = [step.get("run", "") for step in jobs["backend-test"]["steps"] if isinstance(step, dict)]
    assert any("coverage-thresholds.sh" in run for run in runs)
