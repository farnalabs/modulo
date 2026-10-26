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
and every consumer reads that step's CONCLUSION, never a job output. Presence
alone is not a signal: the API returns every defined step, including the ones a
step-level ``if:`` skipped, so a consumer that reads presence calls every
measured tick a no-op (FAR-2176, measured against live run 40316916204). A
future edit that replaces the marker with a job output silently disarms the
signal.

Fixes:

* ``merge-queue.yml`` - a pass that merged nothing writes a ``NO MERGE``
  summary; the step itself runs only on that path.
* ``audit-chain-monitor.yml`` - a cycle with no credentials runs no check at
  all; it now records a ``NOT RUN`` marker in addition to its warning.
* ``deploy-staleness-check.yml`` / ``deploy-watchdog.yml`` - a tick that could
  measure nothing skipped the verdict step, which the transition email read
  back as a HEALTHY verdict and used to swallow the recovery email. The tick
  now records an UNKNOWN marker step, and ``deploy_health.parse_previous_verdict``
  reads it.

The same class had one pure instance (FAR-2161): ``native-release-smoke.yml``
accepted a manual ``workflow_dispatch`` with NEITHER ``canary_version`` nor
``rollback_version``. Every verification job is then ineligible - release-smoke
is push-only, canary requires ``canary_version``, rollback-verify requires
``rollback_version`` - so only ``canary-housekeeping`` ran, its steps skipped on
the skipped canary result, and the run concluded SUCCESS having verified
nothing. Unlike the cases above this one is not a legitimate no-op (the operator
asked for a release smoke), so it is REJECTED by a ``dispatch-guard`` job rather
than marked: ``.github/scripts/native_smoke_dispatch_guard.py`` exits 1 with an
``::error::`` naming both accepted inputs. Push and schedule behaviour must stay
untouched, including the ``canary-housekeeping`` consumer of the
"no bundle published" skip.

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
NATIVE_SMOKE_YML = WORKFLOWS / "native-release-smoke.yml"
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


# --- native-release-smoke: an argument-free dispatch is REJECTED -------------


def _native_smoke_guard_module() -> Any:
    """Load ``native_smoke_dispatch_guard.py`` so the tests exercise its logic."""
    path = next(
        parent / ".github" / "scripts" / "native_smoke_dispatch_guard.py"
        for parent in Path(__file__).resolve().parents
        if (parent / ".github" / "scripts" / "native_smoke_dispatch_guard.py").exists()
    )
    loader = SourceFileLoader("native_smoke_dispatch_guard", str(path))
    module = module_from_spec(spec_from_loader("native_smoke_dispatch_guard", loader))
    sys.modules["native_smoke_dispatch_guard"] = module
    loader.exec_module(module)
    return module


def _native_smoke_guard_step() -> dict:
    """The `dispatch-guard` step that runs the rejection script."""
    for step in _steps(NATIVE_SMOKE_YML):
        if "native_smoke_dispatch_guard.py" in str(step.get("run") or ""):
            return step
    raise AssertionError(
        "native-release-smoke.yml: no step runs .github/scripts/native_smoke_dispatch_guard.py - "
        "an argument-free manual dispatch would conclude green without verifying anything"
    )


def test_native_release_smoke_rejects_an_argument_free_dispatch(monkeypatch: Any, capsys: Any) -> None:
    """A dispatch naming neither input must FAIL with a message naming both.

    The guard is behavioural, not a comment: with both inputs empty the script
    exits 1 and emits an ``::error::`` annotation naming ``canary_version`` and
    ``rollback_version``. A single input is accepted here because the dispatch
    jobs' own ``if:`` gates decide which leg runs (pinned separately below).
    """
    guard = _native_smoke_guard_module()
    violations: list[str] = []

    # The pure verdict: argument-free is the ONLY rejected shape.
    empty = guard.dispatch_error("", "")
    if not empty:
        violations.append("  dispatch_error('', '') no longer rejects an argument-free dispatch")
    else:
        for required in ("canary_version", "rollback_version"):
            if required not in empty:
                violations.append(f"  the rejection message does not name {required!r}")
    for rollback, canary, label in (
        ("", "bundle-v9.9.9", "canary_version only"),
        ("bundle-v9.9.9", "", "rollback_version only"),
        ("bundle-v9.9.9", "bundle-v9.9.9", "both inputs"),
    ):
        if guard.dispatch_error(rollback, canary) is not None:
            violations.append(f"  a dispatch supplying {label} was rejected")
    if guard.dispatch_error("  ", "  ") is None:
        violations.append("  a whitespace-only dispatch was accepted (it names no target)")

    # The env plumbing the workflow step relies on.
    monkeypatch.delenv("CANARY_VERSION", raising=False)
    monkeypatch.delenv("ROLLBACK_VERSION", raising=False)
    if guard.main() != 1:
        violations.append("  main() no longer exits 1 on an argument-free dispatch")
    error_out = capsys.readouterr().out
    if "::error::" not in error_out:
        violations.append("  the rejection is not emitted as an ::error:: annotation")
    for required in ("canary_version", "rollback_version"):
        if required not in error_out:
            violations.append(f"  the emitted error does not name {required!r}")
    monkeypatch.setenv("CANARY_VERSION", "bundle-v9.9.9")
    if guard.main() != 0:
        violations.append("  main() rejects a dispatch that supplied canary_version")

    # The workflow wiring: a standalone, dispatch-only, FIRST job.
    workflow = _load_yaml(NATIVE_SMOKE_YML)
    jobs = workflow.get("jobs") or {}
    guard_job = jobs.get("dispatch-guard")
    if not isinstance(guard_job, dict):
        violations.append("  the dispatch-guard job disappeared")
    else:
        if str(guard_job.get("if") or "") != "github.event_name == 'workflow_dispatch'":
            violations.append("  dispatch-guard must run on workflow_dispatch only (push/schedule untouched)")
        if guard_job.get("needs"):
            violations.append("  dispatch-guard must not depend on another job")
        if next(iter(jobs)) != "dispatch-guard":
            violations.append("  dispatch-guard is no longer the first job in the workflow")
    for job_id, job in jobs.items():
        if isinstance(job, dict) and "dispatch-guard" in str(job.get("needs") or ""):
            violations.append(
                f"  job {job_id!r} now needs dispatch-guard - a skipped needed job would skip the "
                "scheduled canary and change scheduled behaviour"
            )

    step = _native_smoke_guard_step()
    env = step.get("env") or {}
    if env.get("CANARY_VERSION") != "${{ inputs.canary_version }}":
        violations.append("  the guard step no longer maps CANARY_VERSION from inputs.canary_version")
    if env.get("ROLLBACK_VERSION") != "${{ inputs.rollback_version }}":
        violations.append("  the guard step no longer maps ROLLBACK_VERSION from inputs.rollback_version")

    assert not violations, "an argument-free native-release-smoke dispatch can silently verify nothing:\n" + "\n".join(
        violations
    )


def test_native_release_smoke_dispatch_paths_keep_their_own_gates() -> None:
    """Each real path keeps exactly the gate that made it a verification.

    The rejection is justified by these gates: release-smoke is push-only,
    canary needs ``canary_version`` (and no ``rollback_version``), and
    rollback-verify needs ``rollback_version``. The schedule path and the
    ``canary-housekeeping`` consumer of the "no bundle published" skip must be
    untouched, or the FAR-1736 not-applicable signal breaks.
    """
    workflow = _load_yaml(NATIVE_SMOKE_YML)
    jobs = workflow.get("jobs") or {}
    # PyYAML parses the bare key ``on`` as the boolean True.
    on = workflow.get("on", workflow.get(True)) or {}
    violations: list[str] = []

    def job_if(job_id: str) -> str:
        job = jobs.get(job_id)
        if not isinstance(job, dict):
            violations.append(f"  job {job_id!r} disappeared")
            return ""
        return str(job.get("if") or "")

    release_smoke_if = job_if("release-smoke")
    if release_smoke_if != "github.event_name == 'push'":
        violations.append(f"  release-smoke gate changed: {release_smoke_if!r} (expected push-only)")

    canary_if = job_if("canary")
    for fragment in (
        "github.event_name == 'schedule'",
        "inputs.rollback_version == ''",
        "inputs.canary_version != ''",
    ):
        if fragment not in canary_if:
            violations.append(f"  canary gate lost {fragment!r}: {canary_if!r}")

    rollback_if = job_if("rollback-verify")
    for fragment in ("github.event_name == 'workflow_dispatch'", "inputs.rollback_version != ''"):
        if fragment not in rollback_if:
            violations.append(f"  rollback-verify gate lost {fragment!r}: {rollback_if!r}")

    housekeeping = jobs.get("canary-housekeeping")
    if not isinstance(housekeeping, dict):
        violations.append("  canary-housekeeping disappeared - the no-bundle success has no consumer")
    else:
        if housekeeping.get("needs") != "canary":
            violations.append("  canary-housekeeping no longer needs the canary job")
        if str(housekeeping.get("if") or "") != "always()":
            violations.append("  canary-housekeeping must keep `if: always()` to consume a skipped/failed canary")

    for removed in ("push", "schedule", "workflow_dispatch"):
        if removed not in on:
            violations.append(f"  the {removed} trigger disappeared")

    assert not violations, "native-release-smoke dispatch/schedule gating drifted:\n" + "\n".join(violations)
