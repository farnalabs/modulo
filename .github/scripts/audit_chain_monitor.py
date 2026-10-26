#!/usr/bin/env python3
"""Audit-chain monitor escalation state machine (FAR-2160).

``audit-chain-monitor.yml`` runs every 6h and verifies app.modulo.run's audit
chain. When it cannot authenticate it verifies NOTHING: the auth step writes
``authenticated=false``, both checks skip, and the ``Record the NOT RUN monitor
cycle (FAR-2157)`` step runs. FAR-2157 made that legible; nobody was told. A
revoked or removed admin credential would leave a compliance check permanently
green and permanently blind - the failure shape of the 30-hour deploy outage
(FAR-2116), where the condition fired correctly into a channel nobody read.

This module is the escalation decision and every human-facing message for that
signal. CI never exercises a workflow's decision logic, so the logic lives here
where ``backend/tests/unit/scripts/test_audit_chain_monitor.py`` can drive every
threshold, every transition, and the exact subject/body/ticket an operator
receives.

State vector: this workflow's OWN RUN HISTORY - no state store is introduced
---------------------------------------------------------------------------
The durable state is the workflow's own run history, read through the Actions
jobs API by ``.github/scripts/audit-chain-notify.sh``. The jobs API exposes each
job's ``steps`` (name + conclusion) but NOT its ``outputs`` (FAR-2157), so the
signal is a named step and its **conclusion**:

* :data:`BLIND_MARKER_STEP_NAME` - ``success`` means this cycle was BLIND. It
  runs only on the ``authenticated == 'false'`` path. A ``skipped`` conclusion
  is NOT a signal: GitHub returns every defined step, including skipped ones, so
  presence alone can never be read as "it ran" (verified against the live jobs
  API on 2026-10-26).
* :data:`ESCALATION_STEP_NAME` - ``success`` means the blind incident was
  already escalated, so the next cycle does not open another ticket.
* :data:`RECOVERY_STEP_NAME` - ``success`` means a recovery was recorded (the
  incident ticket was closed), so a later blindness is a fresh incident.

The previous cycle's state comes from the newest completed run. The number of
consecutive blind cycles and whether the current blind streak was already
escalated come from walking backwards to the last non-blind run, bounded by the
driver's lookup budget.

Threshold
---------
:data:`BLIND_THRESHOLD_DEFAULT` is **1**: the FIRST blind cycle escalates. A
monitor that has verified nothing must not sit silently blind, and a compliance
gap is not something to wait on. The workflow reads
``vars.AUDIT_BLIND_ESCALATION_THRESHOLD`` to override it (e.g. 2 to tolerate one
transient bad cycle); the value is clamped to a minimum of 1.

An unreadable state vector is UNKNOWN and NEVER suppresses the escalation: the
driver reports the failure loudly and the current blind cycle still escalates,
because "assume healthy" is exactly the failure this module exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Consecutive blind cycles before the Linear escalation ticket opens. Default
#: 1 = the first blind cycle. Overridable via the repository variable
#: ``AUDIT_BLIND_ESCALATION_THRESHOLD``.
BLIND_THRESHOLD_DEFAULT = 1

#: The step that runs ONLY on the ``authenticated == 'false'`` path (FAR-2157).
#: Its ``success`` conclusion is the durable BLIND signal.
BLIND_MARKER_STEP_NAME = "Record the NOT RUN monitor cycle (FAR-2157)"
#: The escalation step (FAR-2160). Its ``success`` conclusion means the blind
#: incident has a ticket, so the next cycle does not open a duplicate.
ESCALATION_STEP_NAME = "Record the blind-cycle escalation (FAR-2160)"
#: The recovery step (FAR-2160). Its ``success`` conclusion records that the
#: incident was closed, so a later blindness is a fresh incident.
RECOVERY_STEP_NAME = "Record the recovered monitor cycle (FAR-2160)"
#: The step that proves the monitor authenticated (FAR-1468).
AUTH_STEP_NAME = "Authenticate to app.modulo.run (admin login)"

#: A stable title marker a human can search for, and the code dedupes on.
TICKET_TITLE_MARKER = "AUDIT-CHAIN MONITOR BLIND"

BLIND = "blind"
AUTHENTICATED = "authenticated"
UNKNOWN = "unknown"

EMAIL_BLIND_TRANSITION = "blind_transition"
EMAIL_RECOVERY = "recovery"
EMAIL_NONE = "none"

#: A step "ran" when the API gave it a terminal, executed conclusion. Skipped
#: steps are returned by the API and are the normal state of every optional
#: step on every non-matching cycle.
_EXECUTED_CONCLUSIONS = frozenset({"success", "failure"})


@dataclass(frozen=True)
class MonitorState:
    """The evaluated blind-cycle state for one monitor cycle."""

    current_blind: bool | None
    previous_blind: bool | None
    consecutive_blind_cycles: int | None
    threshold: int
    already_escalated: bool
    incident_id: str
    history_read: bool
    history_exhausted: bool
    email_action: str
    escalate: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "current_blind": self.current_blind,
            "previous_blind": self.previous_blind,
            "consecutive_blind_cycles": self.consecutive_blind_cycles,
            "threshold": self.threshold,
            "already_escalated": self.already_escalated,
            "incident_id": self.incident_id,
            "history_read": self.history_read,
            "history_exhausted": self.history_exhausted,
            "email_action": self.email_action,
            "escalate": self.escalate,
            "reason": self.reason,
        }

    @property
    def as_outputs(self) -> dict[str, str]:
        """Plain string outputs, as a GitHub Actions step consumes them."""

        def tri(value: bool | None) -> str:
            if value is None:
                return ""
            return "true" if value else "false"

        def _count(value: int | None) -> str:
            return "" if value is None else str(value)

        return {
            "current_blind": tri(self.current_blind),
            "previous_blind": tri(self.previous_blind),
            "consecutive_blind_cycles": _count(self.consecutive_blind_cycles),
            "threshold": str(self.threshold),
            "already_escalated": tri(self.already_escalated),
            "incident_id": self.incident_id,
            "history_read": tri(self.history_read),
            "history_exhausted": tri(self.history_exhausted),
            "email_action": self.email_action,
            "escalate": tri(self.escalate),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Reading one run's durable signal
# ---------------------------------------------------------------------------


def _steps(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for job in (payload or {}).get("jobs") or []:
        if not isinstance(job, dict):
            continue
        steps.extend(step for step in job.get("steps") or [] if isinstance(step, dict))
    return steps


def _conclusion(step: dict[str, Any]) -> str:
    return str(step.get("conclusion") or "").strip().lower()


def _step_executed(steps: Sequence[dict[str, Any]], name: str) -> bool:
    """True when a step with ``name`` actually ran (not skipped/cancelled)."""
    for step in steps:
        if step.get("name") == name:
            return _conclusion(step) in _EXECUTED_CONCLUSIONS
    return False


def _step_succeeded(steps: Sequence[dict[str, Any]], name: str) -> bool:
    """True only when a step with ``name`` ran and succeeded.

    A failed escalation step means the ticket was NOT confirmed, so the next
    cycle must be allowed to try again - a failure is therefore not a marker.
    """
    for step in steps:
        if step.get("name") == name:
            return _conclusion(step) == "success"
    return False


def cycle_state(steps: Sequence[dict[str, Any]]) -> str:
    """Classify one run: ``blind``, ``authenticated`` or ``unknown``.

    BLIND wins: the marker step only runs on the no-credential path. An
    authenticated run has a successful auth step and a skipped blind marker.
    Anything else (an auth failure, a cancelled run, or a run from a workflow
    definition before the auth/marker steps existed) is UNKNOWN - never
    guessed healthy.
    """
    if _step_executed(steps, BLIND_MARKER_STEP_NAME):
        return BLIND
    if _step_succeeded(steps, AUTH_STEP_NAME):
        return AUTHENTICATED
    return UNKNOWN


def describe_run(
    *,
    jobs_payload: dict[str, Any] | None,
    run_id: str,
    created_at: str = "",
    run_url: str = "",
) -> dict[str, Any]:
    """One run's state record, as consumed by :func:`evaluate`."""
    steps = _steps(jobs_payload)
    return {
        "id": str(run_id),
        "created_at": created_at,
        "url": run_url,
        "state": cycle_state(steps),
        "escalated": _step_succeeded(steps, ESCALATION_STEP_NAME),
        "recovered": _step_succeeded(steps, RECOVERY_STEP_NAME),
    }


def _blind_value(state: Any) -> bool | None:
    text = str(state or "").strip().lower()
    if text == BLIND:
        return True
    if text == AUTHENTICATED:
        return False
    return None


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def resolve_email_action(current_blind: bool | None, previous_blind: bool | None) -> str:
    """``blind_transition`` / ``recovery`` / ``none`` for this cycle.

    One email per transition, not one per cycle:

    * going blind (previous authenticated, or unknown - UNKNOWN never
      suppresses the first announcement) -> ``blind_transition``;
    * staying blind -> ``none``;
    * recovering (previous blind, now authenticated) -> ``recovery``;
    * healthy/unknown with no known incident -> ``none``.
    """
    if current_blind is True:
        return EMAIL_NONE if previous_blind is True else EMAIL_BLIND_TRANSITION
    if current_blind is False and previous_blind is True:
        return EMAIL_RECOVERY
    return EMAIL_NONE


def should_escalate(
    *,
    current_blind: bool | None,
    consecutive_blind_cycles: int | None,
    threshold: int,
    already_escalated: bool | None,
    history_read: bool,
) -> bool:
    """Whether this cycle opens (or confirms) the incident ticket.

    Fails toward ALERTING in every ambiguous case: an unreadable state vector
    or an unreadable "already escalated" answer escalates rather than assuming
    the monitor is healthy. ``consecutive_blind_cycles`` is inclusive of the
    current cycle, so the boundary is ``>= threshold`` (exactly-N escalates,
    N-1 does not).
    """
    if current_blind is not True:
        return False
    if not history_read:
        return True
    if already_escalated is True:
        return False
    if already_escalated is None:
        return True
    if consecutive_blind_cycles is None:
        return True
    return consecutive_blind_cycles >= max(1, threshold)


@dataclass(frozen=True)
class _HistoryWalk:
    """What walking the readable history back to its boundary established."""

    prior_blind_streak: int
    already_escalated: bool
    boundary: str | None
    complete: bool


def _record_id(record: dict[str, Any]) -> str:
    return str(record.get("id") or "")


def _previous_blind(records: Sequence[dict[str, Any]], history_read: bool) -> bool | None:
    if history_read and records:
        return _blind_value(records[0].get("state"))
    return None


def _walk_history(records: Sequence[dict[str, Any]], history_exhausted: bool) -> _HistoryWalk:
    """Count the blind streak backwards to the newest non-blind cycle.

    A run of blind records that reaches the end of the readable window may be
    longer than we can see, so it is only COMPLETE when the driver did not
    report the window as exhausted.
    """
    streak = 0
    escalated = False
    for record in records:
        if _blind_value(record.get("state")) is not True:
            return _HistoryWalk(streak, escalated, _record_id(record) or None, True)
        streak += 1
        escalated = escalated or bool(record.get("escalated"))
    if not records:
        return _HistoryWalk(streak, escalated, None, True)
    return _HistoryWalk(streak, escalated, _record_id(records[-1]) or None, not history_exhausted)


def _consecutive_blind(current_blind: bool | None, prior_blind_streak: int) -> int | None:
    if current_blind is True:
        return 1 + prior_blind_streak
    if current_blind is False:
        return 0
    return None


def _incident_fields(
    *,
    history_read: bool,
    already_escalated: bool,
    boundary: str | None,
    records: Sequence[dict[str, Any]],
) -> tuple[bool | None, str]:
    if not history_read:
        return None, "unknown"
    last_id = _record_id(records[-1]) if records else "baseline"
    return already_escalated, boundary or last_id


def evaluate(
    *,
    current_blind: bool | None,
    history: Sequence[dict[str, Any]],
    threshold: int = BLIND_THRESHOLD_DEFAULT,
    history_read: bool = True,
    history_exhausted: bool = False,
) -> MonitorState:
    """Evaluate one cycle from the previous cycles' state records.

    ``history`` is newest-first and excludes the current run. Each record is
    ``{"id", "state", "escalated", ...}`` as produced by :func:`describe_run`.
    """
    due_threshold = max(1, int(threshold))
    records = [record for record in history if isinstance(record, dict)]

    previous_blind = _previous_blind(records, history_read)
    walk = _walk_history(records, history_exhausted)
    consecutive = _consecutive_blind(current_blind, walk.prior_blind_streak)
    already_escalated_value, incident_id = _incident_fields(
        history_read=history_read,
        already_escalated=walk.already_escalated,
        boundary=walk.boundary,
        records=records,
    )

    email_action = resolve_email_action(current_blind, previous_blind)
    escalate = should_escalate(
        current_blind=current_blind,
        consecutive_blind_cycles=consecutive,
        threshold=due_threshold,
        already_escalated=already_escalated_value,
        history_read=history_read,
    )

    return MonitorState(
        current_blind=current_blind,
        previous_blind=previous_blind,
        consecutive_blind_cycles=consecutive,
        threshold=due_threshold,
        already_escalated=walk.already_escalated,
        incident_id=incident_id,
        history_read=history_read,
        history_exhausted=history_exhausted or not walk.complete,
        email_action=email_action,
        escalate=escalate,
        reason=_reason(
            current_blind=current_blind,
            previous_blind=previous_blind,
            consecutive=consecutive,
            threshold=due_threshold,
            already_escalated=already_escalated_value,
            history_read=history_read,
            email_action=email_action,
            escalate=escalate,
        ),
    )


def _previous_label(previous_blind: bool | None) -> str:
    if previous_blind is True:
        return "blind"
    if previous_blind is False:
        return "authenticated (the checks ran)"
    return "unknown (no readable previous cycle)"


def _reason(
    *,
    current_blind: bool | None,
    previous_blind: bool | None,
    consecutive: int | None,
    threshold: int,
    already_escalated: bool | None,
    history_read: bool,
    email_action: str,
    escalate: bool,
) -> str:
    if current_blind is None:
        return (
            "this cycle could not be classified as blind or authenticated (the auth step neither "
            "succeeded nor recorded authenticated=false), so no escalation is inferred from it"
        )
    if current_blind is False:
        if email_action == EMAIL_RECOVERY:
            return (
                "the monitor authenticated and ran its checks again after a blind incident; "
                "the recovery is recorded and the incident ticket is closed"
            )
        return "the monitor authenticated this cycle and ran its checks"
    # current_blind is True
    if not history_read:
        return (
            "the monitor is BLIND (it could not authenticate, so it verified nothing) and the "
            "escalation state could not be read from run history - escalating rather than "
            "assuming the monitor is healthy"
        )
    if already_escalated is True and not escalate:
        return (
            f"the monitor has been BLIND for {consecutive} consecutive cycle(s) "
            f"(threshold {threshold}); this incident was already escalated, so no duplicate "
            "ticket is opened"
        )
    return (
        f"the monitor is BLIND: it could not authenticate, so neither the integrity nor the "
        f"continuity check ran and the audit chain was NOT verified. {consecutive} consecutive "
        f"blind cycle(s) (escalation threshold {threshold})."
    )


# ---------------------------------------------------------------------------
# The messages a human receives
# ---------------------------------------------------------------------------


def build_email(*, state: MonitorState, action: str, run_url: str) -> tuple[str, str]:
    """The (subject, body) an operator receives for a transition."""
    if action == EMAIL_RECOVERY:
        subject = "[Modulo] AUDIT-CHAIN MONITOR RECOVERED: the audit chain is verified again"
        lines = [
            "The audit-chain continuity monitor has RECOVERED.",
            "",
            "An authenticated cycle ran the integrity and 24h-continuity checks again.",
            "The blind incident's ticket has been closed with a recovery note, so a future",
            "blindness opens a fresh incident rather than deduping onto it.",
            "",
            "Figures:",
            "  Monitor       : Audit chain continuity monitor (audit-chain-monitor.yml)",
            f"  Incident id   : {state.incident_id}",
            f"  This cycle    : {run_url}",
            "",
            "No action needed. This email closes the earlier BLIND alert.",
        ]
        return subject, "\n".join(lines)

    subject = f"[Modulo] AUDIT-CHAIN MONITOR BLIND: audit chain NOT verified (incident {state.incident_id})"
    lines = [
        "The audit-chain continuity monitor is BLIND.",
        "",
        "Measured condition(s):",
        f"  {state.reason}",
        "",
        "Figures:",
        "  Monitor                 : Audit chain continuity monitor (audit-chain-monitor.yml)",
        f"  Blind cycles in a row   : {state.consecutive_blind_cycles} (escalation threshold {state.threshold})",
        f"  Previous cycle          : {_previous_label(state.previous_blind)}",
        f"  Incident id             : {state.incident_id}",
        f"  This cycle              : {run_url}",
        "",
        "What this means",
        "  The monitor runs every 6h to verify app.modulo.run's audit chain (chain",
        "  integrity plus 24h continuity). It could not authenticate, so it verified",
        "  NOTHING. A green run in this state is not evidence the chain is intact -",
        "  it is no evidence at all.",
        "",
        "What to do",
        "  1. Confirm E2E_PROD_ADMIN_EMAIL and E2E_PROD_ADMIN_PASSWORD are configured",
        "     on farnalabs/modulo (Settings -> Secrets and variables -> Actions).",
        "  2. If the admin password was rotated, update the secret.",
        "  3. Re-run the monitor and confirm an authenticated cycle:",
        '     gh workflow run "Audit chain continuity monitor" --ref main',
        "",
        "This is a TRANSITION email: it is sent once when the monitor goes blind and",
        "once when it recovers, never every cycle.",
    ]
    return subject, "\n".join(lines)


def build_ticket(*, state: MonitorState, run_url: str) -> tuple[str, str]:
    """The (title, description) of the incident ticket.

    The title embeds the incident id so a human (and the dedupe query) can tell
    one blind incident from another. The body names the monitor, the measured
    facts and the concrete repair - a reader needs no other tab.
    """
    title = f"{TICKET_TITLE_MARKER} [incident {state.incident_id}]: audit chain unverified on app.modulo.run (FAR-2160)"
    description = "\n".join(
        [
            "Automated escalation from `audit-chain-monitor.yml` (FAR-2160).",
            "",
            "## What happened",
            "The audit-chain continuity monitor could not authenticate, so it verified",
            'NOTHING this cycle. This is not "no finding" - it is no evidence, which is',
            "the state an auditor cares about most.",
            "",
            "## Measured facts",
            "- Monitor: Audit chain continuity monitor (`audit-chain-monitor.yml`)",
            f"- Blind cycles in a row: {state.consecutive_blind_cycles} (escalation threshold {state.threshold})",
            f"- Previous cycle: {_previous_label(state.previous_blind)}",
            f"- Incident id: `{state.incident_id}`",
            f"- Cycle run: {run_url}",
            f"- Reason: {state.reason}",
            "",
            "## What to do",
            "1. Confirm `E2E_PROD_ADMIN_EMAIL` and `E2E_PROD_ADMIN_PASSWORD` are configured",
            "   on `farnalabs/modulo` (Settings -> Secrets and variables -> Actions).",
            "2. If the admin password was rotated, update the secret.",
            "3. Re-run the monitor and confirm an authenticated cycle:",
            '   `gh workflow run "Audit chain continuity monitor" --ref main`',
            "",
            "## Dedupe and recovery",
            "One ticket per blind incident. A later cycle in the same incident does not",
            "open another ticket. When an authenticated cycle succeeds, the monitor posts a",
            "recovery comment here and closes this ticket, so a future blindness opens a",
            "fresh incident instead of deduping onto this one.",
        ]
    )
    return title, description


def build_recovery_note(*, state: MonitorState, run_url: str) -> str:
    """The comment posted on the incident ticket when the monitor recovers."""
    return "\n".join(
        [
            "RECOVERED - the audit-chain monitor authenticated again.",
            "",
            "An authenticated cycle ran the integrity and 24h-continuity checks, so the",
            "blind incident this ticket tracked is over. Closing it; a future blindness",
            "opens a fresh incident.",
            "",
            f"- Incident id: `{state.incident_id}`",
            f"- Recovery cycle: {run_url}",
            "",
            "Recorded automatically by `audit-chain-monitor.yml` (FAR-2160).",
        ]
    )


def build_still_blind_note(*, state: MonitorState, run_url: str) -> str:
    """The comment posted when a blind incident continues on an open ticket.

    Used only when the escalation marker has aged out of the run window but the
    incident ticket is still open: a fresh comment keeps the ticket from going
    stale (the FAR-1404 lesson) without opening a duplicate.
    """
    return "\n".join(
        [
            "STILL BLIND - the audit-chain monitor has not authenticated again.",
            "",
            f"- Blind cycles in a row: {state.consecutive_blind_cycles} (escalation threshold {state.threshold})",
            f"- Incident id: `{state.incident_id}`",
            f"- Latest cycle: {run_url}",
            "",
            "The audit chain is still unverified. Confirm `E2E_PROD_ADMIN_EMAIL` /",
            "`E2E_PROD_ADMIN_PASSWORD` on `farnalabs/modulo` and re-run the monitor:",
            '`gh workflow run "Audit chain continuity monitor" --ref main`',
            "",
            "Recorded automatically by `audit-chain-monitor.yml` (FAR-2160).",
        ]
    )


# ---------------------------------------------------------------------------
# CLI (used by audit-chain-notify.sh)
# ---------------------------------------------------------------------------


def _load_json(path: str | None) -> Any:
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    try:
        raw = candidate.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _write_outputs(outputs: dict[str, str], github_output: str | None) -> None:
    rendered = "\n".join(f"{key}={value}" for key, value in outputs.items())
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    else:
        print(rendered)


def _state_from_json(path: str) -> MonitorState | None:
    raw = _load_json(path)
    if not isinstance(raw, dict):
        return None
    fields = MonitorState.__dataclass_fields__
    return MonitorState(**{key: raw[key] for key in raw if key in fields})


def _bool_arg(value: str) -> bool | None:
    text = (value or "").strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _cmd_describe_run(args: argparse.Namespace) -> int:
    record = describe_run(
        jobs_payload=_load_json(args.jobs_json),
        run_id=args.run_id,
        created_at=args.created_at,
        run_url=args.run_url,
    )
    print(json.dumps(record))
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    raw = _load_json(args.history)
    history = raw if isinstance(raw, list) else []
    history_read = args.history_read.lower() != "false"
    state = evaluate(
        current_blind=_bool_arg(args.current_blind),
        history=history,
        threshold=args.threshold,
        history_read=history_read,
        history_exhausted=args.history_exhausted.lower() == "true",
    )
    if args.state_out:
        Path(args.state_out).write_text(json.dumps(state.as_dict()), encoding="utf-8")
    outputs = state.as_outputs
    if args.state_out:
        outputs["state_json"] = args.state_out
    _write_outputs(outputs, args.github_output)

    if not state.history_read:
        print(
            "::error::The audit-chain monitor's escalation state could not be read from its run "
            "history. This cycle is treated as UNKNOWN and still escalates if blind - it is "
            "NEVER assumed healthy (FAR-2160)."
        )
    elif state.current_blind is True:
        print(f"::warning::Audit-chain monitor is BLIND: {state.reason}")
    elif state.email_action == EMAIL_RECOVERY:
        print(f"::notice::Audit-chain monitor recovered: {state.reason}")
    else:
        print(f"::notice::Audit-chain monitor escalation state: {state.reason}")
    return 0


def _cmd_email(args: argparse.Namespace) -> int:
    state = _state_from_json(args.state_json)
    if state is None:
        print("::error::state JSON missing or unreadable - cannot render the audit-monitor email", file=sys.stderr)
        return 2
    if args.action not in (EMAIL_BLIND_TRANSITION, EMAIL_RECOVERY):
        print(f"::error::invalid email action {args.action!r}", file=sys.stderr)
        return 2
    subject, body = build_email(state=state, action=args.action, run_url=args.run_url)
    print(json.dumps({"subject": subject, "body": body}))
    return 0


def _cmd_ticket(args: argparse.Namespace) -> int:
    state = _state_from_json(args.state_json)
    if state is None:
        print("::error::state JSON missing or unreadable - cannot render the audit-monitor ticket", file=sys.stderr)
        return 2
    title, description = build_ticket(state=state, run_url=args.run_url)
    print(json.dumps({"title": title, "description": description, "incident_id": state.incident_id}))
    return 0


def _cmd_recovery_note(args: argparse.Namespace) -> int:
    state = _state_from_json(args.state_json)
    if state is None:
        print("::error::state JSON missing or unreadable - cannot render the recovery note", file=sys.stderr)
        return 2
    print(json.dumps({"body": build_recovery_note(state=state, run_url=args.run_url)}))
    return 0


def _cmd_still_blind_note(args: argparse.Namespace) -> int:
    state = _state_from_json(args.state_json)
    if state is None:
        print("::error::state JSON missing or unreadable - cannot render the still-blind note", file=sys.stderr)
        return 2
    print(json.dumps({"body": build_still_blind_note(state=state, run_url=args.run_url)}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    describe = sub.add_parser("describe-run", help="classify one run from its jobs payload")
    describe.add_argument("--jobs-json", required=True)
    describe.add_argument("--run-id", required=True)
    describe.add_argument("--created-at", default="")
    describe.add_argument("--run-url", default="")
    describe.set_defaults(func=_cmd_describe_run)

    evaluate_parser = sub.add_parser("evaluate", help="decide the blind-cycle state for this cycle")
    evaluate_parser.add_argument("--current-blind", default="unknown", choices=("true", "false", "unknown", ""))
    evaluate_parser.add_argument("--history", help="JSON array of previous state records (newest first)")
    evaluate_parser.add_argument("--threshold", type=int, default=BLIND_THRESHOLD_DEFAULT)
    evaluate_parser.add_argument("--history-read", default="true", choices=("true", "false"))
    evaluate_parser.add_argument("--history-exhausted", default="false", choices=("true", "false"))
    evaluate_parser.add_argument("--github-output", help="append outputs here instead of stdout")
    evaluate_parser.add_argument("--state-out", help="write the state as JSON here")
    evaluate_parser.set_defaults(func=_cmd_evaluate)

    email_parser = sub.add_parser("email", help="render the blind/recovery email as JSON")
    email_parser.add_argument("--state-json", required=True)
    email_parser.add_argument("--action", required=True, choices=(EMAIL_BLIND_TRANSITION, EMAIL_RECOVERY))
    email_parser.add_argument("--run-url", default="")
    email_parser.set_defaults(func=_cmd_email)

    ticket_parser = sub.add_parser("ticket", help="render the incident ticket as JSON")
    ticket_parser.add_argument("--state-json", required=True)
    ticket_parser.add_argument("--run-url", default="")
    ticket_parser.set_defaults(func=_cmd_ticket)

    note_parser = sub.add_parser("recovery-note", help="render the recovery comment as JSON")
    note_parser.add_argument("--state-json", required=True)
    note_parser.add_argument("--run-url", default="")
    note_parser.set_defaults(func=_cmd_recovery_note)

    still_parser = sub.add_parser("still-blind-note", help="render the still-blind comment as JSON")
    still_parser.add_argument("--state-json", required=True)
    still_parser.add_argument("--run-url", default="")
    still_parser.set_defaults(func=_cmd_still_blind_note)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
