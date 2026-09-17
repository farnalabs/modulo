"""BDD step definitions: audit append-only enforcement.

Drives the REAL application-layer append-only guard
(``modulo.core.audit_logger.append_only``) — registration plus the
SQLAlchemy ``before_update`` / ``before_delete`` listeners — against actual
ORM rows in an in-memory engine, so the immutability claim (``feat-audit``,
"Append-only enforcement is defense-in-depth") is pinned by executing BDD
rather than by a mocked assertion.
"""

from __future__ import annotations

import uuid

from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from modulo.core.audit_logger.append_only import (
    AppendOnlyViolationError,
    register_append_only_guard,
)
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.error_event import ErrorEvent

scenarios("../features/audit/append_only.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _persist_audit_event(request) -> AuditEvent:
    """Persist a real AuditEvent row and capture its id before any mutation.

    A rejected mutation deactivates the session, so reading ``row.id`` after
    the failing ``commit()`` would trigger a lazy DB reload and re-raise; the
    id (the same one the guard bakes into its "cannot be updated/deleted"
    message) is therefore captured at persist time.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AuditEvent.__table__])
    session = Session(engine)
    row = AuditEvent(organisation_id=_ORG_ID, event_type="pipeline.run")
    session.add(row)
    session.commit()
    request.node._append_only_session = session
    request.node._append_only_row = row
    request.node._append_only_event_id = str(row.id)
    return row


@given("the append-only guard is registered")
def step_guard_registered() -> None:
    register_append_only_guard()


@given("an audit event row is persisted")
def step_audit_event_row_persisted(request) -> None:
    _persist_audit_event(request)


@given("an error event row is persisted")
def step_error_event_row_persisted(request) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[ErrorEvent.__table__])
    session = Session(engine)
    row = ErrorEvent(
        organisation_id=_ORG_ID,
        fingerprint="fp-1",
        level="error",
        message="boom",
        source="backend",
    )
    session.add(row)
    session.commit()
    request.node._append_only_session = session
    request.node._append_only_row = row
    request.node._append_only_event_id = str(row.id)


@when(parsers.parse("the audit event row is {action}"))
def step_mutate_audit_event(request, action: str) -> None:
    _attempt_mutation(request, action)


@when(parsers.parse("the error event row is {action}"))
def step_mutate_error_event(request, action: str) -> None:
    _attempt_mutation(request, action)


def _mutable_field(row) -> str:
    """A real mapped column to mutate on the persisted model (dirtying it)."""
    if isinstance(row, ErrorEvent):
        return "message"
    return "event_type"


def _attempt_mutation(request, action: str) -> None:
    row = request.node._append_only_row
    session = request.node._append_only_session
    if action == "updated":
        setattr(row, _mutable_field(row), "mutated")
    elif action == "deleted":
        session.delete(row)
    try:
        session.commit()
        request.node._append_only_error = None
    except AppendOnlyViolationError as exc:
        request.node._append_only_error = exc


@then("an AppendOnlyViolationError is raised")
def step_violation_raised(request) -> None:
    error = request.node._append_only_error
    assert error is not None, "the append-only guard did not reject the mutation"
    assert isinstance(error, AppendOnlyViolationError)


@then(parsers.parse('the error names the event and the "{mutation}" mutation'))
def step_error_names_event(request, mutation: str) -> None:
    error = request.node._append_only_error
    assert error is not None, "no AppendOnlyViolationError was captured"
    message = str(error).lower()
    event_id = request.node._append_only_event_id
    assert event_id in message, f"guard message did not name the event id:\n{message}"
    expected = "cannot be updated" if mutation == "update" else "cannot be deleted"
    assert expected in message, f"expected {expected!r} in guard message, got {message!r}"


@when("a new audit event row is appended")
def step_append_new_event(request) -> None:
    _persist_audit_event(request)
    request.node._append_only_error = None


@then("the row is persisted without an AppendOnlyViolationError")
def step_row_persisted(request) -> None:
    error = request.node._append_only_error
    assert error is None, f"the append-only guard blocked a legitimate INSERT: {error!r}"
    assert request.node._append_only_event_id, "the appended row was not persisted"
