"""Architecture tests: the audit-chain monitor ESCALATES when it is blind (FAR-2160).

FAR-2157 made a blind audit-chain cycle legible (``authenticated=false`` and a
``NOT RUN`` marker step). It did not make anyone act: a removed or revoked admin
credential would leave a compliance check permanently green and permanently
blind.

These tests pin the wiring that turns that signal into an incident:

* the blind-cycle state is decided from this workflow's own run history, and the
  durable signal is a named step's **conclusion** (the jobs API exposes
  ``steps``, not job ``outputs`` - FAR-2157);
* the first blind cycle opens a Linear ticket, a recovery closes it, and an
  email is sent only on a transition;
* the blind condition itself never fails the run (a red monitor every 6h is
  noise), while a broken escalation channel is allowed to fail loudly;
* the step names the state machine depends on match the workflow exactly.

Removing the escalation step, or renaming a marker, fails here rather than
silently disarming the monitor.
"""

from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

import yaml

BACKEND = Path(__file__).resolve().parent.parent.parent
REPO = BACKEND.parent
AUDIT_YML = REPO / ".github" / "workflows" / "audit-chain-monitor.yml"
DRIVER = REPO / ".github" / "scripts" / "audit-chain-notify.sh"

# Step names the state machine reads (kept as literals so a rename on either
# side is a visible, actionable violation).
BLIND_MARKER_STEP = "Record the NOT RUN monitor cycle (FAR-2157)"
ESCALATION_STEP = "Record the blind-cycle escalation (FAR-2160)"
EMAIL_STEP = "Email the operator on an audit-monitor visibility transition (FAR-2160)"
RECOVERY_STEP = "Record the recovered monitor cycle (FAR-2160)"
EVALUATE_STEP = "Evaluate blind-cycle escalation state (FAR-2160)"


def _health_module() -> Any:
    """Load ``audit_chain_monitor.py`` so the tests compare its real constants."""
    path = next(
        parent / ".github" / "scripts" / "audit_chain_monitor.py"
        for parent in Path(__file__).resolve().parents
        if (parent / ".github" / "scripts" / "audit_chain_monitor.py").exists()
    )
    loader = SourceFileLoader("audit_chain_monitor", str(path))
    module = module_from_spec(spec_from_loader("audit_chain_monitor", loader))
    sys.modules["audit_chain_monitor"] = module
    loader.exec_module(module)
    return module


def _workflow() -> dict[str, Any]:
    data = yaml.safe_load(AUDIT_YML.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{AUDIT_YML.name}: YAML root is not a mapping"
    return data


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for job in (_workflow().get("jobs") or {}).values():
        if isinstance(job, dict):
            steps.extend(step for step in job.get("steps") or [] if isinstance(step, dict))
    return steps


def _step_named(name: str) -> dict[str, Any]:
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"{AUDIT_YML.name}: no step named {name!r}")


def _violations(checks: list[tuple[bool, str]]) -> list[str]:
    return [message for passed, message in checks if not passed]


# --- the signal and its consumers -------------------------------------------


def test_state_machine_step_names_match_the_workflow() -> None:
    """A rename on either side silently disarms the escalation.

    The module reads step names; the workflow defines them. They must agree
    exactly, and the blind/recovery/escalation markers must all be present.
    """
    monitor = _health_module()
    checks = [
        (monitor.BLIND_MARKER_STEP_NAME == BLIND_MARKER_STEP, "the blind marker step was renamed"),
        (monitor.ESCALATION_STEP_NAME == ESCALATION_STEP, "the escalation marker step was renamed"),
        (monitor.RECOVERY_STEP_NAME == RECOVERY_STEP, "the recovery marker step was renamed"),
        (monitor.BLIND_THRESHOLD_DEFAULT == 1, "the documented escalation default is no longer 1"),
    ]
    violations = _violations(checks)
    for name in (BLIND_MARKER_STEP, ESCALATION_STEP, EMAIL_STEP, RECOVERY_STEP, EVALUATE_STEP):
        try:
            _step_named(name)
        except AssertionError:
            violations.append(f"  the workflow no longer defines the step {name!r}")
    assert not violations, "the blind-cycle state machine is not wired:\n" + "\n".join(violations)


def test_escalation_step_is_gated_and_is_its_own_durable_marker() -> None:
    """Removing the escalation step (or its gate) must fail this test.

    The step's success conclusion is the "already escalated" marker the next
    cycle reads, so it must run only when the state machine says `escalate`.
    """
    step = _step_named(ESCALATION_STEP)
    run = str(step.get("run") or "")
    checks = [
        ("steps.escalation.outputs.escalate == 'true'" in str(step.get("if") or ""), "the gate is gone"),
        ("audit_chain_monitor.py ticket" in run, "the ticket payload is no longer rendered by the evaluator"),
        ("AUDIT-CHAIN MONITOR BLIND" in run, "the Linear dedupe marker is gone"),
        ("incident ${INCIDENT_ID}" in run, "the per-incident dedupe key is gone"),
    ]
    violations = _violations(checks)
    assert not violations, "the blind-cycle escalation step is not wired:\n" + "\n".join(violations)


def test_every_cycle_decides_the_state_from_run_history() -> None:
    """The state must be read from run history, not assumed or hard-coded."""
    step = _step_named(EVALUATE_STEP)
    checks = [
        (str(step.get("if") or "") == "always()", "the state is not evaluated on every cycle"),
        (step.get("id") == "escalation", "downstream steps no longer read this step's outputs"),
        ("audit-chain-notify.sh evaluate" in str(step.get("run") or ""), "the evaluator is not invoked"),
        ("CURRENT_AUTHENTICATED" in (step.get("env") or {}), "the current cycle's auth state is not passed in"),
        (
            "AUDIT_BLIND_ESCALATION_THRESHOLD" in (AUDIT_YML.read_text(encoding="utf-8")),
            "the threshold is not configurable",
        ),
        ("actions: read" in (AUDIT_YML.read_text(encoding="utf-8")), "the workflow cannot read its own run history"),
    ]
    violations = _violations(checks)
    assert not violations, "the blind-cycle state is not derived from run history:\n" + "\n".join(violations)


def test_a_transition_emails_and_only_a_transition() -> None:
    step = _step_named(EMAIL_STEP)
    gate = str(step.get("if") or "")
    checks = [
        ("steps.escalation.outputs.email_action" in gate, "the email is not gated on a transition"),
        ("!= 'none'" in gate, "the email can fire on a non-transition cycle"),
        ("audit-chain-notify.sh email" in str(step.get("run") or ""), "the email driver is not invoked"),
        ("ZOHO_SMTP_USER" in (step.get("env") or {}), "the shared SMTP secrets are not wired"),
    ]
    violations = _violations(checks)
    assert not violations, "the transition email is not wired:\n" + "\n".join(violations)


def test_recovery_is_recorded_and_closes_the_incident() -> None:
    step = _step_named(RECOVERY_STEP)
    run = str(step.get("run") or "")
    checks = [
        ("steps.escalation.outputs.email_action == 'recovery'" in str(step.get("if") or ""), "recovery is not gated"),
        ("issueUpdate" in run, "the incident ticket is never closed"),
        ("recovery-note" in run, "the recovery note is not posted"),
        ("GITHUB_STEP_SUMMARY" in run, "the recovery is not recorded in the run summary"),
    ]
    violations = _violations(checks)
    assert not violations, "recovery is not recorded:\n" + "\n".join(violations)


def test_the_blind_condition_never_fails_the_run() -> None:
    """A red monitor every 6h is noise; the escalation channel may fail loudly.

    The FAR-2157 marker and the two driver steps must not exit non-zero on the
    blind path. The ticket step is deliberately allowed to fail when the
    escalation channel itself is broken, because a silently-lost escalation is
    worse than a red run.
    """
    marker_run = str(_step_named(BLIND_MARKER_STEP).get("run") or "")
    evaluate_run = str(_step_named(EVALUATE_STEP).get("run") or "")
    email_run = str(_step_named(EMAIL_STEP).get("run") or "")
    checks = [
        ("exit 0" in marker_run, "the NOT RUN marker no longer exits 0"),
        ("audit-chain-notify.sh evaluate" in evaluate_run, "the evaluator step does not delegate to the driver"),
        ("audit-chain-notify.sh email" in email_run, "the email step does not delegate to the driver"),
        ("continue-on-error" not in AUDIT_YML.read_text(encoding="utf-8"), "a step masks its failure"),
    ]
    violations = _violations(checks)
    assert not violations, "the blind path is not fail-open:\n" + "\n".join(violations)


def test_the_driver_never_assumes_healthy_on_a_state_read_failure() -> None:
    """A failed history read must escalate, not be skipped."""
    script = DRIVER.read_text(encoding="utf-8")
    checks = [
        ('history_read="false"' in script, "an unreadable history is not marked UNKNOWN"),
        (
            "::error::Could not read the audit-chain monitor's run history" in script,
            "an unreadable history is not loud",
        ),
        ("--history-read" in script, "the UNKNOWN state is not passed to the evaluator"),
    ]
    violations = _violations(checks)
    assert not violations, "the driver can assume healthy:\n" + "\n".join(violations)
