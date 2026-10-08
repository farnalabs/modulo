"""FAR-1597: api-conftest autouse fixtures must survive collection order.

pytest keys an autouse fixture to the collector node that loaded its
conftest. A directory-level ``Package`` pytest node for ``tests/unit/api/``
is NOT guaranteed to exist when the module is revisited after an argv
section that detours out of the directory — a node that ALREADY exists for
the second visit stops re-collecting and its (long-loaded) conftest
fixtures are dropped from later sections. The slack module's
``_provisioned_system_engine`` provisioning was registered exactly that way
(inspected via the re-visting Package), so a session collecting
``tests/unit/test_analytics_builder.py`` BETWEEN two slack-surrounding api
file sections read a DEGRADED system engine and 503'd every Slack delivery
test (``slack.system_bootstrap_degraded``).

The fix registers the provisioning in ``tests/unit/conftest.py`` — the
unit-level conftest always loads, because conftest.py of every directory
containing an initial argv item is loaded before any test runs, so the
fixtures hold regardless of the argv order.

This module reproduces the failing argv shape (three api sections, a unit
detour, then the Slack module) and asserts the Slack module passes in that
exact order; the middle sections are pinned to test nodes to keep the child
run cheap while the directory/section sequence stays identical. The child
session is a real pytest run in a subprocess — in-process collection would
inherit the parent's collector state.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# The failing argv shape (FAR-1597), preserved section-for-section: sections in
# ``tests/unit/api/``, a detour section OUT of the directory into
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

_BACKEND_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.timeout(300)
def test_slack_module_passes_after_unit_detour_order() -> None:
    """The Slack module must pass when collected after a unit-level detour.

    Before the FAR-1597 registration fix the child run failed with 503s
    (``slack.system_bootstrap_degraded``) on every Slack delivery path.
    """
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
        "--timeout=120",
        "-p",
        "no:cacheprovider",
        "-q",
        "-x",
    ]
    # The command is a hardcoded list of pytest paths (no untrusted input);
    # S603 flags any subprocess.run regardless, so opt out on the call.
    try:
        completed = subprocess.run(  # noqa: S603
            cmd,
            cwd=_BACKEND_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=240,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            "child pytest run (FAR-1597 order) hung past 240s without concluding; "
            f"stdout tail:\n{(exc.stdout or b'')[-4000:]!r}"
        )
    if completed.returncode != 0:
        pytest.fail(
            "child pytest run (FAR-1597 order) FAILED — the api conftest autouse\n"
            "fixtures did not hold across the unit detour. stdout tail:\n" + completed.stdout[-4000:]
        )
