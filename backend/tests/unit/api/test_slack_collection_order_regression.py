"""FAR-1597: api-conftest autouse fixtures must survive collection order.

pytest keys an autouse fixture to the collector node that loaded its
conftest. A directory-level ``Package`` pytest node for ``tests/unit/api/``
is NOT guaranteed to exist when the module is revisited after an argv
section that detours out of the directory — a node that ALREADY exists for
the second visit stops re-collecting and its (long-loaded) conftest
fixtures are dropped from later sections. The Slack module's
provisioned-system-engine settings patch was registered exactly that way
(lived in ``tests/unit/api/conftest.py``, keyed to the revisiting Package),
so a session collecting ``tests/unit/test_analytics_builder.py`` BETWEEN two
slack-surrounding api file sections read a DEGRADED system engine and 503'd
every Slack delivery test (``slack.system_bootstrap_degraded``).

The fix has TWO layers as of main (and this regression module pins both):

* the provisioning fixture was moved to ``tests/unit/conftest.py`` — the
  unit-level conftest of every directory containing an initial argv item
  loads before any test runs, so the patch holds regardless of argv order;
* FAR-1569 added the module-level autouse ``_system_engine_isolated``
  fixture to the Slack module itself, which re-applies the provisioned
  settings for every one of its tests no matter what the conftest layer
  did.

Honest reproduction note: on the pinned pytest version the single-layer
drop CANNOT be reproduced in a child session in this argv shape — a fully
restored fragile api-conftest registration (control verified 2026-10-09)
left both probes below green. What still regresses loudly was verified the
same day: removing the provisioning entirely (no conftest-layer fixture and
no module-level re-application) 503s every Slack delivery test and fails
both probes. This module therefore pins two durable invariants under the
exact incident argv order:

* ``test_provisioned_system_engine_fixture_holds_under_detour_order`` runs
  ``pytest --setup-plan`` — collection only, no execution — and asserts the
  ``_provisioned_system_engine`` autouse fixture appears in the SETUP plan
  of the Slack module's nodes. A missing provisioning collapses here
  immediately; and if pytest ever regresses the "late-detour conftest
  fixture" behaviour, the plan again shows it.
* ``test_slack_module_passes_after_unit_detour_order`` runs the real
  child session and asserts the module passes end-to-end — the last line of
  defence against any engine-bootstrap regression on these delivery paths.

The child session is a real pytest run in a subprocess — in-process
collection would inherit the parent's collector state.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# The incident argv shape (FAR-1597), preserved section-for-section: sections
# in ``tests/unit/api/``, a detour section OUT of the directory into
# ``tests/unit/``, then the Slack module back in ``tests/unit/api/``. Each
# middle section is pinned to its first test node — the drop reproduces with
# single nodes (verified: the pinned-but-unprovisioned variant failed the
# Slack module with the identical degraded-engine signature) and one test per
# section keeps the child run cheap. The Slack module is passed WHOLE: the
# incident broke every Slack delivery path, and each is an assertion here.
_ORDER: tuple[str, ...] = (
    "tests/unit/api/test_dashboard.py::TestDashboardSummary::test_returns_summary",
    "tests/unit/api/test_viewmodel_endpoint.py::test_me_returns_200_with_username",
    "tests/unit/api/test_execution_origin_surfaces.py::TestMcpRunItemExecutionOrigin::test_dispatched_run_reads_dispatched",
    "tests/unit/test_analytics_builder.py::TestAllowlistRejection::test_group_by_rejects_sql_injection_string",
    "tests/unit/api/test_slack_trigger_endpoint.py",
)
_SLACK_MODULE = "tests/unit/api/test_slack_trigger_endpoint.py"

_BACKEND_ROOT = Path(__file__).resolve().parents[3]


def _run_ordered_pytest(extra_flags: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run pytest over the incident argv order in a clean child session."""
    env = os.environ.copy()
    # Determinism: the scoped run of the test-style scanner must not constrain
    # the child, and no inherited ad-hoc pytest args may reshuffle collection.
    env.pop("MODULO_TEST_STYLE_SCOPE", None)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *_ORDER,
        *extra_flags,
        "--timeout=120",
        "-p",
        "no:cacheprovider",
        "-q",
    ]
    # The command is a hardcoded list of pytest paths (no untrusted input);
    # S603 flags any subprocess.run regardless, so opt out on the call.
    try:
        return subprocess.run(  # noqa: S603
            cmd,
            cwd=_BACKEND_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"child pytest run (FAR-1597 order, flags={extra_flags}) hung past {timeout:.0f}s without "
            f"concluding; stdout tail:\n{(exc.stdout or b'')[-4000:]!r}"
        )


def _slack_plan_nodes(plan_output: str) -> list[str]:
    """Every ``--setup-plan`` line for a Slack-module node (with its fixtures)."""
    pattern = re.escape(_SLACK_MODULE) + r"::"
    return [line for line in plan_output.splitlines() if re.search(pattern, line) and "fixtures used:" in line]


@pytest.mark.timeout(300)
def test_provisioned_system_engine_fixture_holds_under_detour_order() -> None:
    """The provisioning fixture must appear in the SETUP plan of the Slack
    nodes under the incident argv order.

    Fixture application is a collection-time decision, so ``--setup-plan``
    exposes the per-node fixture sets without needing either layer's
    execution: if the provisioning is ever lost — deleted entirely, or
    dropped again for late-section modules the way FAR-1597 observed — the
    Slack node lines in the plan stop citing ``_provisioned_system_engine``
    and this assertion fails with the offending node ids. (Verified
    grey-box 2026-10-09: the control that removed the provisioning failed
    here with exactly that message; the fragile api-conftest-only
    registration stayed green, see the module docstring.)
    """
    completed = _run_ordered_pytest(["--setup-plan"], timeout=240)
    assert completed.returncode == 0, (
        "child pytest --setup-plan (FAR-1597 order) itself failed - collection is broken "
        "before the fixture probe can be judged. stdout tail:\n" + completed.stdout[-4000:]
    )
    slack_nodes = _slack_plan_nodes(completed.stdout)
    assert slack_nodes, (
        "no Slack-module nodes found in the setup-plan output; the plan probe cannot "
        "judge fixture presence. stdout tail:\n" + completed.stdout[-4000:]
    )
    missing = [line.split("(")[0].strip() for line in slack_nodes if "_provisioned_system_engine" not in line]
    assert not missing, (
        "the provisioned-system-engine fixture is MISSING from the setup plan of the\n"
        "Slack module under the incident argv order — the api-conftest autouse\n"
        "fixtures are being dropped for late-section modules again (FAR-1597).\n"
        "First nodes lacking the fixture:\n  " + "\n  ".join(missing[:5])
    )


@pytest.mark.timeout(300)
def test_slack_module_passes_after_unit_detour_order() -> None:
    """The Slack module must pass when collected after a unit-level detour.

    End-to-end detector: with BOTH protection layers removed (the unit-level
    provisioning patch and FAR-1569's module-level ``_system_engine_isolated``)
    the engine reads degraded and every Slack delivery test in the child run
    503s (``slack.system_bootstrap_degraded``). With either layer intact the
    module passes — the single-layer drop is detected by
    ``test_provisioned_system_engine_fixture_holds_under_detour_order`` above.
    """
    completed = _run_ordered_pytest(["-x"], timeout=240)
    assert completed.returncode == 0, (
        "child pytest run (FAR-1597 order) FAILED — the Slack module no longer holds\n"
        "a provisioned system engine across the unit detour. stdout tail:\n" + completed.stdout[-4000:]
    )
