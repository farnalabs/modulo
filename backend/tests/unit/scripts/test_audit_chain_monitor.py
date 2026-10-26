"""Unit tests for ``.github/scripts/audit_chain_monitor.py`` (FAR-2160).

A CI workflow's decision logic is never exercised by pytest, so the audit-chain
monitor's blind-cycle escalation was extracted into a testable script. These
tests drive every threshold edge and every transition, and they assert the REAL
subject/body/ticket an operator receives - not just a boolean.

They run without a database, a network or GitHub.
"""

from __future__ import annotations

import json
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

for parent in Path(__file__).resolve().parents:
    _script_path = parent / ".github" / "scripts" / "audit_chain_monitor.py"
    if _script_path.exists():
        break
else:
    raise RuntimeError("Could not find .github/scripts/audit_chain_monitor.py")

_REPO_ROOT = parent

_loader = SourceFileLoader("audit_chain_monitor", str(_script_path))
monitor = module_from_spec(spec_from_loader("audit_chain_monitor", _loader))
# dataclasses resolves the defining module through sys.modules, so the loaded
# module must be registered before it executes.
sys.modules["audit_chain_monitor"] = monitor
_loader.exec_module(monitor)

_RUN_URL = "https://github.com/farnalabs/modulo/actions/runs/40316916204"


def _jobs(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"jobs": [{"name": "Check app.modulo.run audit chain continuity", "steps": list(steps)}]}


def _blind_jobs(*, escalated: bool = False, recovered: bool = False) -> dict[str, Any]:
    """A blind cycle's jobs payload: auth skipped-credential, NOT RUN marker ran."""
    steps: list[dict[str, Any]] = [
        {"name": monitor.AUTH_STEP_NAME, "conclusion": "success"},
        {"name": monitor.BLIND_MARKER_STEP_NAME, "conclusion": "success"},
    ]
    if escalated:
        steps.append({"name": monitor.ESCALATION_STEP_NAME, "conclusion": "success"})
    if recovered:
        steps.append({"name": monitor.RECOVERY_STEP_NAME, "conclusion": "success"})
    return _jobs(*steps)


def _authenticated_jobs(*, recovered: bool = False) -> dict[str, Any]:
    """An authenticated cycle: auth ran, both checks ran, NOT RUN marker skipped."""
    steps: list[dict[str, Any]] = [
        {"name": monitor.AUTH_STEP_NAME, "conclusion": "success"},
        {"name": "Check 1 - verify audit chain integrity", "conclusion": "success"},
        {"name": "Check 2 - newest audit event within 24h", "conclusion": "success"},
        {"name": monitor.BLIND_MARKER_STEP_NAME, "conclusion": "skipped"},
    ]
    if recovered:
        steps.append({"name": monitor.RECOVERY_STEP_NAME, "conclusion": "success"})
    return _jobs(*steps)


def _record(run_id: int, *, state: str, escalated: bool = False) -> dict[str, Any]:
    return {"id": str(run_id), "state": state, "escalated": escalated, "recovered": False}


def _state(run_id: int, *, blind: bool, escalated: bool = False, recovered: bool = False) -> dict[str, Any]:
    payload = (
        _blind_jobs(escalated=escalated, recovered=recovered) if blind else _authenticated_jobs(recovered=recovered)
    )
    return monitor.describe_run(jobs_payload=payload, run_id=str(run_id), run_url=f"https://example/{run_id}")


def _evaluate(
    *,
    current_blind: bool | None,
    history: list[dict[str, Any]] | None = None,
    threshold: int = monitor.BLIND_THRESHOLD_DEFAULT,
    history_read: bool = True,
    history_exhausted: bool = False,
) -> Any:
    return monitor.evaluate(
        current_blind=current_blind,
        history=history if history is not None else [],
        threshold=threshold,
        history_read=history_read,
        history_exhausted=history_exhausted,
    )


# ---------------------------------------------------------------------------
# Reading the durable signal: conclusions, never mere presence
# ---------------------------------------------------------------------------


def test_blind_marker_success_is_blind() -> None:
    record = _state(1, blind=True)
    assert record["state"] == monitor.BLIND
    assert record["escalated"] is False


def test_blind_marker_skipped_is_not_blind() -> None:
    """The marker step is PRESENT on every run; only its success means blind.

    GitHub returns every defined step in the jobs API - a skipped step has
    conclusion ``skipped``. Reading presence alone would mark every run blind,
    which is exactly the defect this asserts against.
    """
    record = _state(1, blind=False)
    assert record["state"] == monitor.AUTHENTICATED


def test_auth_failure_is_unknown_not_blind() -> None:
    """A failed login is a red run with its own ticket - it is not the blind path."""
    payload = _jobs(
        {"name": monitor.AUTH_STEP_NAME, "conclusion": "failure"},
        {"name": monitor.BLIND_MARKER_STEP_NAME, "conclusion": "skipped"},
    )
    assert monitor.cycle_state(monitor._steps(payload)) == monitor.UNKNOWN


def test_a_run_without_the_marker_steps_is_unknown() -> None:
    """A run from before this workflow definition existed is never guessed healthy."""
    payload = _jobs({"name": monitor.AUTH_STEP_NAME, "conclusion": "skipped"})
    assert monitor.cycle_state(monitor._steps(payload)) == monitor.UNKNOWN


def test_escalation_marker_requires_success_not_presence() -> None:
    """A failed ticket step must retry next cycle, so only success counts."""
    skipped = _jobs(
        {"name": monitor.AUTH_STEP_NAME, "conclusion": "success"},
        {"name": monitor.BLIND_MARKER_STEP_NAME, "conclusion": "success"},
        {"name": monitor.ESCALATION_STEP_NAME, "conclusion": "skipped"},
    )
    failed = _jobs(
        {"name": monitor.AUTH_STEP_NAME, "conclusion": "success"},
        {"name": monitor.BLIND_MARKER_STEP_NAME, "conclusion": "success"},
        {"name": monitor.ESCALATION_STEP_NAME, "conclusion": "failure"},
    )
    succeeded = _jobs(
        {"name": monitor.AUTH_STEP_NAME, "conclusion": "success"},
        {"name": monitor.BLIND_MARKER_STEP_NAME, "conclusion": "success"},
        {"name": monitor.ESCALATION_STEP_NAME, "conclusion": "success"},
    )
    assert monitor.describe_run(jobs_payload=skipped, run_id="1")["escalated"] is False
    assert monitor.describe_run(jobs_payload=failed, run_id="1")["escalated"] is False
    assert monitor.describe_run(jobs_payload=succeeded, run_id="1")["escalated"] is True


# ---------------------------------------------------------------------------
# The threshold boundary: N-1 does not escalate, N does
# ---------------------------------------------------------------------------


def test_threshold_n_minus_one_does_not_escalate() -> None:
    """Exactly-N does not fire at N-1: one blind cycle under a threshold of 2 waits."""
    state = _evaluate(
        current_blind=True,
        history=[_record(1, state=monitor.AUTHENTICATED)],
        threshold=2,
    )
    assert state.consecutive_blind_cycles == 1
    assert state.escalate is False


def test_threshold_n_escalates() -> None:
    """The second consecutive blind cycle reaches the threshold of 2 and fires."""
    state = _evaluate(
        current_blind=True,
        history=[_record(2, state=monitor.BLIND), _record(1, state=monitor.AUTHENTICATED)],
        threshold=2,
    )
    assert state.consecutive_blind_cycles == 2
    assert state.escalate is True


def test_first_blind_cycle_escalates_by_default() -> None:
    """Default threshold 1: the FIRST blind cycle opens the ticket and emails."""
    state = _evaluate(current_blind=True, history=[])
    assert state.threshold == 1
    assert state.consecutive_blind_cycles == 1
    assert state.escalate is True
    assert state.email_action == monitor.EMAIL_BLIND_TRANSITION


def test_threshold_below_one_is_clamped() -> None:
    """A zero/negative configuration must not disable the escalation."""
    state = _evaluate(current_blind=True, history=[], threshold=0)
    assert state.threshold == 1
    assert state.escalate is True


# ---------------------------------------------------------------------------
# Dedupe: one escalation per blind incident, not one per cycle
# ---------------------------------------------------------------------------


def test_repeat_blind_cycle_does_not_re_escalate() -> None:
    """The escalation marker in the previous blind run suppresses a duplicate."""
    state = _evaluate(
        current_blind=True,
        history=[
            _record(3, state=monitor.BLIND, escalated=True),
            _record(2, state=monitor.BLIND),
            _record(1, state=monitor.AUTHENTICATED),
        ],
    )
    assert state.previous_blind is True
    assert state.already_escalated is True
    assert state.escalate is False
    assert state.email_action == monitor.EMAIL_NONE


def test_recovery_is_a_single_transition_email() -> None:
    """A blind -> authenticated cycle sends recovery exactly once."""
    state = _evaluate(
        current_blind=False,
        history=[_record(2, state=monitor.BLIND), _record(1, state=monitor.AUTHENTICATED)],
    )
    assert state.email_action == monitor.EMAIL_RECOVERY
    assert state.escalate is False


def test_a_second_authenticated_cycle_sends_nothing() -> None:
    state = _evaluate(
        current_blind=False,
        history=[_record(2, state=monitor.AUTHENTICATED), _record(1, state=monitor.AUTHENTICATED)],
    )
    assert state.email_action == monitor.EMAIL_NONE


def test_fresh_blind_cycle_after_recovery_escalates_again() -> None:
    """Recovery resets the incident, so the next blindness is a fresh escalation.

    This is the FAR-1404 trap: a new incident must not dedupe onto the previous
    incident's (recovered) ticket.
    """
    state = _evaluate(
        current_blind=True,
        history=[
            _state(3, blind=False, recovered=True),
            _state(2, blind=True, escalated=True),
            _state(1, blind=False),
        ],
    )
    assert state.previous_blind is False
    assert state.already_escalated is False
    assert state.consecutive_blind_cycles == 1
    assert state.escalate is True
    assert state.email_action == monitor.EMAIL_BLIND_TRANSITION


# ---------------------------------------------------------------------------
# State-read failure: fail loud, never "assume healthy"
# ---------------------------------------------------------------------------


def test_unreadable_history_escalates_a_blind_cycle() -> None:
    state = _evaluate(current_blind=True, history=[], history_read=False)
    assert state.history_read is False
    assert state.previous_blind is None
    assert state.escalate is True
    assert state.email_action == monitor.EMAIL_BLIND_TRANSITION
    assert "could not be read from run history" in state.reason


def test_unknown_previous_cycle_never_suppresses() -> None:
    state = _evaluate(current_blind=True, history=[_record(9, state=monitor.UNKNOWN)])
    assert state.previous_blind is None
    assert state.escalate is True
    assert state.already_escalated is False


def test_exhausted_window_is_reported() -> None:
    history = [_record(index, state=monitor.BLIND) for index in range(12, 0, -1)]
    state = _evaluate(current_blind=True, history=history, history_exhausted=True)
    assert state.history_exhausted is True
    assert state.consecutive_blind_cycles == 13


def test_evaluate_is_silent_for_an_unknown_current_cycle() -> None:
    state = _evaluate(current_blind=None, history=[_record(1, state=monitor.BLIND)])
    assert state.email_action == monitor.EMAIL_NONE
    assert state.escalate is False
    assert "could not be classified" in state.reason


# ---------------------------------------------------------------------------
# The messages a human receives (real rendered text, not a boolean)
# ---------------------------------------------------------------------------


def test_blind_email_names_the_monitor_facts_and_repair() -> None:
    state = _evaluate(current_blind=True, history=[_record(40316916204, state=monitor.AUTHENTICATED)])
    subject, body = monitor.build_email(state=state, action=state.email_action, run_url=_RUN_URL)
    assert "AUDIT-CHAIN MONITOR BLIND" in subject
    assert "incident 40316916204" in subject
    assert "Audit chain continuity monitor (audit-chain-monitor.yml)" in body
    assert "Blind cycles in a row   : 1 (escalation threshold 1)" in body
    assert "It could not authenticate" in body
    assert "not evidence the chain is intact" in body
    assert "E2E_PROD_ADMIN_EMAIL" in body
    assert 'gh workflow run "Audit chain continuity monitor" --ref main' in body
    assert _RUN_URL in body


def test_recovery_email_closes_the_incident() -> None:
    state = _evaluate(
        current_blind=False,
        history=[_record(40316916204, state=monitor.BLIND), _record(1, state=monitor.AUTHENTICATED)],
    )
    subject, body = monitor.build_email(state=state, action=state.email_action, run_url=_RUN_URL)
    assert "AUDIT-CHAIN MONITOR RECOVERED" in subject
    assert "No action needed" in body
    assert "Incident id   : 1" in body
    assert _RUN_URL in body


def test_ticket_title_carries_the_incident_marker_and_the_repair() -> None:
    state = _evaluate(current_blind=True, history=[_record(40316916204, state=monitor.AUTHENTICATED)])
    title, description = monitor.build_ticket(state=state, run_url=_RUN_URL)
    assert title.startswith("AUDIT-CHAIN MONITOR BLIND [incident 40316916204]")
    assert "FAR-2160" in title
    assert "audit-chain-monitor.yml" in description
    assert "verified" in description
    assert "E2E_PROD_ADMIN_PASSWORD" in description
    assert "One ticket per blind incident" in description
    assert "recovery comment" in description


def test_recovery_and_still_blind_notes_are_actionable() -> None:
    blind_state = _evaluate(current_blind=True, history=[_record(1, state=monitor.AUTHENTICATED)])
    recovery_state = _evaluate(
        current_blind=False,
        history=[_record(2, state=monitor.BLIND), _record(1, state=monitor.AUTHENTICATED)],
    )
    recovery_note = monitor.build_recovery_note(state=recovery_state, run_url=_RUN_URL)
    still_blind_note = monitor.build_still_blind_note(state=blind_state, run_url=_RUN_URL)
    assert "RECOVERED" in recovery_note
    assert "a future blindness" in recovery_note
    assert "STILL BLIND" in still_blind_note
    assert "E2E_PROD_ADMIN_EMAIL" in still_blind_note


# ---------------------------------------------------------------------------
# CLI surface (what the workflow actually invokes)
# ---------------------------------------------------------------------------


def test_cli_describe_run_and_evaluate_write_outputs(tmp_path: Path, capsys: Any) -> None:
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps(_blind_jobs()), encoding="utf-8")

    exit_code = monitor.main(
        ["describe-run", "--jobs-json", str(jobs_file), "--run-id", "7", "--run-url", "https://example/7"]
    )
    described = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert described["state"] == monitor.BLIND
    assert described["id"] == "7"

    history_file = tmp_path / "history.json"
    history_file.write_text(json.dumps([_record(6, state=monitor.AUTHENTICATED)]), encoding="utf-8")
    github_output = tmp_path / "output"
    state_file = tmp_path / "state.json"
    exit_code = monitor.main(
        [
            "evaluate",
            "--current-blind",
            "true",
            "--history",
            str(history_file),
            "--github-output",
            str(github_output),
            "--state-out",
            str(state_file),
        ]
    )
    assert exit_code == 0
    written = github_output.read_text(encoding="utf-8")
    assert "escalate=true" in written
    assert "email_action=blind_transition" in written
    assert f"state_json={state_file}" in written
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["incident_id"] == "6"


def test_cli_email_ticket_and_notes_render_json(tmp_path: Path, capsys: Any) -> None:
    state = _evaluate(current_blind=True, history=[_record(6, state=monitor.AUTHENTICATED)])
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state.as_dict()), encoding="utf-8")

    email_exit = monitor.main(
        ["email", "--state-json", str(state_file), "--action", "blind_transition", "--run-url", _RUN_URL]
    )
    assert email_exit == 0
    email_payload = json.loads(capsys.readouterr().out)
    assert "AUDIT-CHAIN MONITOR BLIND" in email_payload["subject"]
    assert "E2E_PROD_ADMIN_EMAIL" in email_payload["body"]

    assert monitor.main(["ticket", "--state-json", str(state_file), "--run-url", _RUN_URL]) == 0
    ticket_payload = json.loads(capsys.readouterr().out)
    assert ticket_payload["incident_id"] == "6"
    assert "[incident 6]" in ticket_payload["title"]

    assert monitor.main(["still-blind-note", "--state-json", str(state_file), "--run-url", _RUN_URL]) == 0
    note_payload = json.loads(capsys.readouterr().out)
    assert "STILL BLIND" in note_payload["body"]


def test_cli_rejects_an_unreadable_state_file(tmp_path: Path, capsys: Any) -> None:
    missing = tmp_path / "missing.json"
    assert monitor.main(["ticket", "--state-json", str(missing)]) == 2
    assert "cannot render" in capsys.readouterr().err
