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
  ``integration``, ``bdd``, ``architecture``, ``connectors``,
  ``conformance``, ``docker``) the hook lands an ``error`` filter ahead of the
  ini ``default`` and a warned ``PytestUnraisableExceptionWarning`` actually
  raises;
- the deliberate SCOPE: an item in an UNAUDITED suite (``determination``,
  ``load``) must not install the filter;
- the exact audited set in ``GATED_SUITE_ROOTS``;
- every workflow command that names a gated-suite path still carries the
  byte-identical escalation and no longer disables the warnings plugin (BDD
  workflow, main-integration workflow, deploy pre-deploy unit and integration
  steps, CI email-settings/step-rot BDD steps, CI integration-changed, CI
  tier1b-light, CI architecture and connector-contract steps, merge-queue unit
  run, schemathesis and Tier 1b nightly runs, both conformance-suite commands
  in the k8s-conformance workflow, and the runner-ci docker harness suite);
- the runner-ci harness command runs EVERY module in ``tests/docker`` (a gated
  test that no CI job runs is a test the gate cannot protect);
- EVERY pytest invocation in ``.github/scripts/coverage-thresholds.sh`` still
  carries the byte-identical escalation. The unit suite is a single run over
  ``tests/unit/``; the guard locates the invocation(s) by the ``pytest``
  command itself and checks the flag per invocation, so a future split that
  drops the flag on one shard fails too - and that single run covers the whole
  suite from that root;
- the ``backend-test`` job in ``.github/workflows/ci.yml`` still invokes that
  script.

Prove-it-fails: deleting the ``warnings.filterwarnings(...)`` line from
``tests/conftest.py`` makes the escalation tests fail; deleting a suite from
``GATED_SUITE_ROOTS`` makes its parametrised case fail; dropping the ``-W`` flag
from any pinned CI command makes its command test fail, and adding a new
gated-suite command without the flag fails the exhaustive workflow scan;
deleting the ``-W`` flag from any pytest invocation in the coverage script makes
``test_ci_unit_command_carries_the_escalation`` fail.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
K8S_CONFORMANCE_WORKFLOW = REPO / ".github" / "workflows" / "k8s-conformance.yml"
SCHEMATHESIS_WORKFLOW = REPO / ".github" / "workflows" / "schemathesis-nightly.yml"
TIER1B_NIGHTLY_WORKFLOW = REPO / ".github" / "workflows" / "tier1b-nightly.yml"
RUNNER_CI_WORKFLOW = REPO / ".github" / "workflows" / "runner-ci-harness.yml"
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
    "architecture": ("test_workflow_structure.py",),
    "connectors": ("test_connector_contract.py",),
    "conformance": ("runtime_provider", "test_k8s_conformance.py"),
    "docker": ("test_runner_bundled.py",),
}

#: One representative item per UNAUDITED suite. ``load`` and ``determination``
#: hold no test modules at all (only support files and fixture data) but are
#: named so a future test module cannot enter either tree ungated.
_UNGATED_SAMPLES = {
    "determination": ("test_determination.py",),
    "load": ("test_load_profile.py",),
}

# A pytest COMMAND (not ``pytest-cov``, ``pytest.PytestUnraisableExceptionWarning``
# or a prose mention): the word followed by whitespace.
_PYTEST_COMMAND = re.compile(r"(?<![\w./-])pytest\s")

# The ``tests/unit/`` root as a WHOLE path token: a later component (``api/``)
# or a longer prefix must not satisfy it, because either would silently shrink
# the suite the coverage report claims to describe.
_UNIT_ROOT = re.compile(r"(?<![\w./-])tests/unit/(?=\s|$)")


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


def _baseline_without_the_escalation() -> list[Any]:
    """The ini filter state with any hook-installed escalation removed.

    This guard lives in ``tests/architecture``, itself a gated tree, so the
    root conftest's ``pytest_runtest_setup`` has ALREADY installed the
    escalation for this very item before the test body runs. Capturing
    ``warnings.filters`` as-is would make the behavioural assertions vacuous
    (the filter is present no matter which path is passed). Strip any matching
    ``error`` filter to model the un-gated starting state.
    """
    return [
        entry
        for entry in warnings.filters
        if not (entry[0] == "error" and entry[2] is not None and issubclass(_CATEGORY, entry[2]))
    ]


@pytest.mark.parametrize("suite", sorted(_GATED_SAMPLES))
def test_gated_suite_item_escalates_unraisable_warning(suite: str) -> None:
    """An item in each audited suite turns the warning class into an error."""
    with warnings.catch_warnings():
        warnings.filters[:] = _baseline_without_the_escalation()
        _apply_hook(_item_at(TESTS.joinpath(suite, *_GATED_SAMPLES[suite])))
        assert _has_error_filter()
        with pytest.raises(_CATEGORY):
            warnings.warn(_CATEGORY("probe: leaked coroutine"), stacklevel=1)


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
    with warnings.catch_warnings():
        warnings.filters[:] = _baseline_without_the_escalation()
        _apply_hook(_item_at(TESTS / "unit" / "test_dispatch.py"))
        assert _has_error_filter()
        warnings.filters[:] = _baseline_without_the_escalation()
        _apply_hook(_item_at(TESTS / "integration" / "test_run_crud.py"))
        assert _has_error_filter()


@pytest.mark.parametrize("suite", sorted(_UNGATED_SAMPLES))
def test_unaudited_suite_item_does_not_install_the_filter(suite: str) -> None:
    """Every unaudited suite is deliberately out of scope - the hook declines."""
    with warnings.catch_warnings():
        warnings.filters[:] = _baseline_without_the_escalation()
        baseline = warnings.filters[:]
        _apply_hook(_item_at(TESTS.joinpath(suite, *_UNGATED_SAMPLES[suite])))
        assert warnings.filters == baseline


def test_gated_suite_roots_are_the_verified_set() -> None:
    """The audited set is exactly unit + integration + BDD + architecture + connectors + conformance + docker.

    Widening the gate is a claim that a suite was audited under the escalation
    and ran green; that must be a deliberate edit here, never a side effect.
    ``conformance`` (a live single-node kind cluster, 13/13 under the escalation
    on 2026-10-23) and ``docker`` (green after the FAR-2095 stop-vs-remove fix
    plus the leaked exec/socket-proxy resources) are both gated; ``load`` and
    ``determination`` (no test modules) stay excluded.
    """
    assert tuple(_root_conftest.GATED_SUITE_ROOTS) == (
        "unit",
        "integration",
        "bdd",
        "architecture",
        "connectors",
        "conformance",
        "docker",
    )


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
        (CI_WORKFLOW, "pytest tests/architecture/test_product_map_consistency.py"),
        (CI_WORKFLOW, "pytest tests/architecture/ -q"),
        (CI_WORKFLOW, "pytest tests/connectors/"),
        # integration-changed expands a computed file list, so it names no
        # gated path - pin it by the expansion itself.
        (CI_WORKFLOW, 'pytest "${TEST_FILES[@]}"'),
        (MERGE_QUEUE_WORKFLOW, "pytest tests/unit/"),
        (SCHEMATHESIS_WORKFLOW, "pytest tests/integration/test_schemathesis.py"),
        (TIER1B_NIGHTLY_WORKFLOW, "tests/integration/test_tier1b_connectors_heavy.py"),
        (RUNNER_CI_WORKFLOW, "pytest tests/docker/test_bundled_runner_harness.py"),
        # Both the kind and the managed leg run the conformance suite - the
        # parametrised check asserts EVERY matching command carries the flag.
        (K8S_CONFORMANCE_WORKFLOW, "pytest tests/conformance/"),
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
        "ci-product-map-consistency",
        "ci-architecture",
        "ci-connectors",
        "ci-integration-changed",
        "merge-queue-unit",
        "schemathesis-nightly",
        "tier1b-nightly",
        "runner-ci-docker",
        "k8s-conformance",
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


def test_runner_ci_command_runs_every_docker_test_module() -> None:
    """The harness job runs EVERY docker module, not only the rig-backed one.

    The `docker` tree is gated in-process, but a docker test that no CI job
    runs is a test the gate cannot protect (the gap this closes: two of the
    four modules were gated yet never executed in CI). Pinning the file list
    against the tree itself means adding a module without wiring it into the
    command fails here, and dropping one fails too.
    """
    commands = [command for command in _workflow_run_commands(RUNNER_CI_WORKFLOW) if "pytest tests/docker/" in command]
    assert len(commands) == 1, f"expected exactly one docker pytest command, found {len(commands)}"
    wired = set(re.findall(r"tests/docker/(\w+\.py)", commands[0]))
    on_disk = {path.name for path in (TESTS / "docker").glob("test_*.py")}
    assert wired == on_disk, (
        "the runner-ci docker command does not run every tests/docker module: "
        f"missing={sorted(on_disk - wired)} extra={sorted(wired - on_disk)}"
    )


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


def _pytest_invocations(script: str) -> list[str]:
    """Every pytest command in ``script``, as one joined logical line each.

    Continuation lines are joined first: the escalation flag sits on the
    backslash-continued line after the command, so a physical-line scan would
    see the invocation and its flag as separate lines. Comment lines are
    excluded - the script's explanatory comments mention ``pytest``.

    The invocation is located by the ``pytest`` command itself, never by a
    literal ``tests/unit/`` path, so the guard keeps working if the unit suite
    is ever split into shards or re-rooted again.
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

    Since FAR-2112 the unit suite is a single run over ``tests/unit/`` (the
    FAR-2081 shard split was reverted once ``backend-test`` moved to a 16 GB
    runner). What must still hold: the invocation targets the ``tests/unit``
    root - a different root would silently shrink the suite - and EVERY such
    invocation carries the byte-identical escalation.
    """
    script = COVERAGE_SCRIPT.read_text(encoding="utf-8")
    invocations = _pytest_invocations(script)
    assert invocations, "no pytest invocation found in .github/scripts/coverage-thresholds.sh"
    assert any(_UNIT_ROOT.search(line) for line in invocations), (
        "the coverage script's unit pytest invocation no longer targets the tests/unit root"
    )
    # The invocation is a backslash-continued command, so _pytest_invocations
    # joins the continuation lines before looking for the flag. Check every
    # invocation, so a future split that drops the flag from one shard fails
    # here even though a whole-script search would still pass.
    offenders = [line for line in invocations if _ESCALATION not in line]
    assert all(_ESCALATION in line for line in invocations), (
        f"every pytest invocation must carry {_ESCALATION!r}; missing on:\n" + "\n".join(offenders)
    )


def test_ci_unit_run_covers_the_whole_suite() -> None:
    """The single unit run covers the whole suite from the ``tests/unit`` root.

    A narrower root - or a dropped file set - would silently shrink the suite
    and its coverage while the report still claims to describe the whole suite.
    Pinning exactly one invocation rooted at ``tests/unit`` fails both on a
    half-reverted script (shard machinery left behind, so two runs) and on one
    that quietly narrows the root.
    """
    invocations = _pytest_invocations(COVERAGE_SCRIPT.read_text(encoding="utf-8"))
    assert len(invocations) == 1, (
        f"expected exactly one unit pytest invocation, found {len(invocations)}:\n" + "\n".join(invocations)
    )
    assert _UNIT_ROOT.search(invocations[0]), (
        f"the unit pytest invocation no longer covers the whole tests/unit suite: {invocations[0]}"
    )


def test_ci_backend_test_job_runs_the_unit_coverage_script() -> None:
    """The ``backend-test`` job still delegates the unit run to the pinned script."""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "backend-test" in jobs
    runs = [step.get("run", "") for step in jobs["backend-test"]["steps"] if isinstance(step, dict)]
    assert any("coverage-thresholds.sh" in run for run in runs)
