"""Architecture tests: a no-op workflow run must SAY it did nothing (FAR-2157).

FAR-2152 fixed one instance of a general class: a run that concludes SUCCESS
while performing none of the work its name implies, so a consumer reads the
green run as "the action happened" (four "successful" deploy runs shipped
nothing on 2026-10-25; FAR-528 was the same shape over two days).

Every workflow was inventoried (``docs/operations/workflow-noop-inventory.md``)
and classified legitimate no-op / masking. This module pins the three
`masking` cases the sweep fixed, plus the signalling convention they share:

**The durable machine-readable outcome is a named STEP, not a job output.**
The Actions jobs API exposes each job's ``steps`` (name + conclusion) but NOT
its ``outputs`` - verified against the live API on 2026-10-26, where a job's
keys contained ``steps`` and no ``outputs``. So "this run did nothing" is
recorded as a step that runs ONLY on the no-op path (the pattern
``deploy_run_attempts.py`` and the k8s-conformance staleness job already use),
and every consumer reads step presence, never a job output. A future edit that
replaces the marker with a job output silently disarms the signal.

Three fixes:

* ``merge-queue.yml`` - a pass that merged nothing writes a ``NO MERGE``
  summary; the step itself runs only on that path.
* ``audit-chain-monitor.yml`` - a cycle with no credentials runs no check at
  all; it now records a ``NOT RUN`` marker in addition to its warning.
* ``deploy-staleness-check.yml`` / ``deploy-watchdog.yml`` - a tick that could
  measure nothing skipped the verdict step, which the transition email read
  back as a HEALTHY verdict and used to swallow the recovery email. The tick
  now records an UNKNOWN marker step, and ``deploy_health.parse_previous_verdict``
  reads it.

Every test reports actionable violations instead of a bare failure.
"""

from __future__ import annotations

import re
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

import yaml

BACKEND = Path(__file__).resolve().parent.parent.parent
REPO = BACKEND.parent
WORKFLOWS = REPO / ".github" / "workflows"

MERGE_QUEUE_YML = WORKFLOWS / "merge-queue.yml"
AUDIT_CHAIN_YML = WORKFLOWS / "audit-chain-monitor.yml"
DEPLOY_STALENESS_YML = WORKFLOWS / "deploy-staleness-check.yml"
DEPLOY_WATCHDOG_YML = WORKFLOWS / "deploy-watchdog.yml"
PR_CONFLICT_YML = WORKFLOWS / "pr-conflict-reconcile.yml"
NOTIFY_SCRIPT = REPO / ".github" / "scripts" / "deploy-health-notify.sh"

# The step each escalation workflow runs ONLY when its verdict was UNKNOWN.
UNKNOWN_TICK_STEP = "Record the unmeasured tick (FAR-2157)"
# The step the audit-chain monitor runs ONLY when it had no credentials.
AUDIT_NOT_RUN_STEP = "Record the NOT RUN monitor cycle (FAR-2157)"


def _health_module() -> Any:
    """Load ``deploy_health.py`` so the tests compare its real constants."""
    path = next(
        parent / ".github" / "scripts" / "deploy_health.py"
        for parent in Path(__file__).resolve().parents
        if (parent / ".github" / "scripts" / "deploy_health.py").exists()
    )
    loader = SourceFileLoader("deploy_health", str(path))
    # dataclasses resolves the defining module through sys.modules.
    module = module_from_spec(spec_from_loader("deploy_health", loader))
    sys.modules["deploy_health"] = module
    loader.exec_module(module)
    return module


def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: YAML root is not a mapping"
    return data


def _steps(path: Path) -> list[dict]:
    steps: list[dict] = []
    for job in (_load_yaml(path).get("jobs") or {}).values():
        if isinstance(job, dict):
            steps.extend(step for step in job.get("steps") or [] if isinstance(step, dict))
    return steps


def _step_named(path: Path, name: str) -> dict:
    for step in _steps(path):
        if step.get("name") == name:
            return step
    raise AssertionError(f"{path.name}: no step named {name!r}")


# --- merge-queue: a pass that merged nothing says so ------------------------


def test_merge_queue_no_approved_prs_pass_is_marked() -> None:
    """The empty-queue pass must write a NO MERGE summary, not exit silently.

    An empty queue is a legitimate no-op. What was wrong is that the green run
    was indistinguishable from a pass that merged twelve PRs: nothing in the
    run said "nothing was merged". The step runs only when the collection found
    no approved PR, so its presence is also the machine-readable outcome.
    """
    step = _step_named(MERGE_QUEUE_YML, "Exit early if no approved PRs")
    run = str(step.get("run") or "")
    violations: list[str] = []
    if "NO MERGE" not in run:
        violations.append("  the no-approved-PRs pass no longer writes a NO MERGE marker")
    if "GITHUB_STEP_SUMMARY" not in run:
        violations.append("  the NO MERGE marker is not written to the run summary")
    if "exit 0" not in run:
        violations.append("  the no-op pass must still exit 0 (an empty queue is legitimate)")
    if "::error::" in run:
        violations.append("  a legitimate empty queue must not be reported as an error")
    if "steps.collect.outputs.pr_count == '0'" not in str(step.get("if") or ""):
        violations.append("  the marker step no longer runs only on the no-op path")
    assert not violations, "the empty-queue pass is not legible:\n" + "\n".join(violations)


def test_merge_queue_nothing_mergeable_pass_is_marked() -> None:
    """The other no-op shape - approved PRs but none mergeable - is marked too.

    Collapsing this into "Exit if nothing to merge" without a summary is how a
    genuine stall (approved PRs all stuck behind CI) looked like a healthy
    pass.
    """
    step = _step_named(MERGE_QUEUE_YML, "Exit if nothing to merge")
    run = str(step.get("run") or "")
    violations: list[str] = []
    if "NO MERGE" not in run:
        violations.append("  the nothing-to-merge pass no longer writes a NO MERGE marker")
    if "GITHUB_STEP_SUMMARY" not in run:
        violations.append("  the NO MERGE marker is not written to the run summary")
    if "steps.merge.outputs.merged_count == '0'" not in str(step.get("if") or ""):
        violations.append("  the marker step no longer runs only when nothing merged")
    assert not violations, "the nothing-to-merge pass is not legible:\n" + "\n".join(violations)


# --- audit-chain-monitor: a blind cycle says NOT RUN ------------------------


def test_audit_chain_monitor_missing_credentials_is_marked_not_run() -> None:
    """A cycle with no credentials runs no check, so it must say NOT RUN.

    The monitor's whole product is its green run. With the admin secrets
    absent both checks skip and the job succeeds - previously with only a log
    warning, so the run history claimed the chain had been verified. The job
    still succeeds (a missing secret must not red-queue the monitor); the
    marker is what makes it honest.
    """
    auth = _step_named(AUDIT_CHAIN_YML, "Authenticate to app.modulo.run (admin login)")
    auth_run = str(auth.get("run") or "")
    marker = _step_named(AUDIT_CHAIN_YML, AUDIT_NOT_RUN_STEP)
    marker_run = str(marker.get("run") or "")
    violations: list[str] = []
    if "authenticated=false" not in auth_run:
        violations.append("  the auth step no longer records authenticated=false on the no-credential path")
    if "authenticated=true" not in auth_run:
        violations.append("  the auth step no longer records authenticated=true after a successful login")
    if "steps.auth.outputs.authenticated == 'false'" not in str(marker.get("if") or ""):
        violations.append("  the NOT RUN marker is not gated on the no-credential path")
    if "NOT RUN" not in marker_run:
        violations.append("  the NOT RUN marker text disappeared")
    if "GITHUB_STEP_SUMMARY" not in marker_run:
        violations.append("  the NOT RUN marker is not written to the run summary")
    if "exit 0" not in marker_run:
        violations.append("  the marker step must not fail the run (the convention is fail-open)")
    assert not violations, "a blind audit-chain cycle is not legible:\n" + "\n".join(violations)


# --- deploy health: an unmeasured tick is UNKNOWN, never healthy -----------


def test_unknown_tick_marker_name_matches_the_parser_constant() -> None:
    """Both workflows and the parser must agree on the marker step's name.

    The parser matches on the step name. A rename on either side turns a real
    UNKNOWN tick back into a healthy verdict - silently, because the skipped
    verdict step still maps to HEALTHY.
    """
    health = _health_module()
    violations: list[str] = []
    if health.UNKNOWN_VERDICT_STEP_NAME != UNKNOWN_TICK_STEP:
        violations.append(
            "  deploy_health.UNKNOWN_VERDICT_STEP_NAME is "
            f"{health.UNKNOWN_VERDICT_STEP_NAME!r}, this suite expects {UNKNOWN_TICK_STEP!r}"
        )
    for path, output in ((DEPLOY_STALENESS_YML, "health"), (DEPLOY_WATCHDOG_YML, "watchdog")):
        step = _step_named(path, health.UNKNOWN_VERDICT_STEP_NAME)
        if f"steps.{output}.outputs.healthy == ''" not in str(step.get("if") or ""):
            violations.append(f"  {path.name}: the UNKNOWN marker is not gated on steps.{output}.outputs.healthy == ''")
    assert not violations, "the UNKNOWN-tick marker is not wired:\n" + "\n".join(violations)


def test_unknown_tick_marker_is_a_summary_and_never_fails_the_run() -> None:
    """The marker records the state; it must not invent a deploy failure.

    An unmeasurable tick is not an unhealthy tick. The run still concludes
    success (the ::warning::s name what could not be read) - the SECURITY
    here is the summary marker plus the parser, not a red run.
    """
    violations: list[str] = []
    for path in (DEPLOY_STALENESS_YML, DEPLOY_WATCHDOG_YML):
        run = str(_step_named(path, UNKNOWN_TICK_STEP).get("run") or "")
        if "NOT MEASURED" not in run:
            violations.append(f"  {path.name}: the UNKNOWN marker text disappeared")
        if "GITHUB_STEP_SUMMARY" not in run:
            violations.append(f"  {path.name}: the UNKNOWN marker is not written to the run summary")
        if "exit 0" not in run:
            violations.append(f"  {path.name}: the marker must not fail the run")
        if "::error::" in run:
            violations.append(f"  {path.name}: an unmeasured tick is not an error-level condition")
    assert not violations, "the UNKNOWN-tick marker is not honest:\n" + "\n".join(violations)


def test_deploy_health_consumer_walks_back_past_unmeasured_ticks() -> None:
    """The transition email must read the last MEASURED verdict, not the last run.

    This is the consumer side of the fix: the state vector is walked backwards
    past UNKNOWN ticks, and only a non-UNKNOWN verdict is accepted. Stopping at
    the newest run is what recorded an unmeasured tick as healthy and swallowed
    the recovery email.
    """
    script = NOTIFY_SCRIPT.read_text(encoding="utf-8")
    violations: list[str] = []
    if "while IFS= read -r PREV_ID" not in script:
        violations.append("  the previous-verdict lookup no longer walks the run history")
    # The skip comparison may name the sentinel directly or through a shell
    # variable (a de-duplication of the literal). Either is fine as long as the
    # value it compares against really is "unknown" - naming a variable that
    # holds something else would silently stop the walk.
    skip = re.search(r'if \[\[ "\$RUN_VERDICT" != "(\$[A-Za-z_][A-Za-z0-9_]*|unknown)" \]\]', script)
    if skip is None:
        violations.append("  the walk no longer skips UNKNOWN verdicts to reach a measured one")
    elif skip.group(1).startswith("$"):
        sentinel = re.escape(skip.group(1)[1:])
        if not re.search(rf'^{sentinel}="unknown"$', script, re.MULTILINE):
            violations.append(
                f'  the walk compares RUN_VERDICT against {skip.group(1)}, which the script never defines as "unknown"'
            )
    if "previous-verdict --jobs-json" not in script:
        violations.append("  the walk no longer asks the evaluator for the verdict")
    if "completed[0]" in script:
        violations.append("  the lookup stopped at the single newest completed run again")
    assert not violations, "the transition email can misread an unmeasured tick:\n" + "\n".join(violations)


# --- pr-conflict-reconcile: a blind cycle says NOTHING RECONCILED ----------


def test_pr_conflict_reconcile_api_failure_is_marked() -> None:
    """A cycle that could not read the PR list must say it reconciled nothing.

    The fail-open is deliberate (a transient API blip must not red the
    schedule), but before this marker the green run was indistinguishable from
    a pass that examined every open PR.
    """
    script = PR_CONFLICT_YML.read_text(encoding="utf-8")
    violations: list[str] = []
    if "NOTHING RECONCILED" not in script:
        violations.append("  the API-failure path no longer writes a NOTHING RECONCILED marker")
    if 'echo "api_failed=true"' not in script:
        violations.append("  the API-failure signal downstream steps gate on disappeared")
    assert not violations, "a blind reconcile cycle is not legible:\n" + "\n".join(violations)
