"""FAR-1854: an expired HITL review is RECORDED as unanswered, never dropped.

``_terminalize_expired_hitl_reviews`` cancels the run and leaves the gate
``decision IS NULL``. Both pending surfaces fence undecided gates to
actionable run statuses, so the expired review vanished with no gate-level
evidence. These tests pin the recorder's contract without a database:

* an undecided gate on an expiry-cancelled run gets exactly ONE
  ``hitl.review_expired`` event whose payload says ``expired_undecided`` and
  carries ``decision: null`` — never a fabricated verdict;
* the selection predicate excludes gates that were already decided (the
  ``decision IS NULL`` guard) and runs that were not cancelled for expiry
  (the ``cancel_reason`` / ``error_code`` guard);
* an audit failure is logged under the dedicated key and never raised —
  the cancellation has already committed and must not be undone by its own
  record.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.hitl_manager import review_expiry
from modulo.core.hitl_manager.review_expiry import (
    EXPIRY_AUDIT_LOG_KEY,
    EXPIRY_EVENT_TYPE,
    EXPIRY_OUTCOME,
    record_review_expiry_outcomes,
)
from modulo.db.models.run import CANCEL_REASON_HITL_REVIEW_EXPIRED

_ORG = uuid.uuid4()
_RUN = uuid.uuid4()
_CLAIM = uuid.uuid4()
_GATE = "review-step"


def _factory(rows: list[Any]) -> MagicMock:
    """A sessionmaker mock usable as ``async with factory() as s, s.begin():``."""
    session = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    session.execute = AsyncMock(return_value=result)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = begin_cm
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = session_cm
    return factory


def _undecided_gate() -> tuple[uuid.UUID, uuid.UUID, str, str]:
    """The (claim, run, review_id, cancel_reason) row the selection returns."""
    return (_CLAIM, _RUN, _GATE, CANCEL_REASON_HITL_REVIEW_EXPIRED)


async def test_undecided_gate_on_expiring_run_gets_one_expiry_event() -> None:
    """Exactly one event, and it records a MISSING decision rather than a verdict."""
    factory = _factory([_undecided_gate()])
    append = AsyncMock(return_value=True)

    with (
        patch.object(review_expiry, "set_rls_org", new=AsyncMock()),
        patch.object(review_expiry, "append_background_audit_event", append),
    ):
        recorded = await record_review_expiry_outcomes(factory, [(_RUN, _ORG)])

    assert recorded == 1
    append.assert_awaited_once()
    kwargs = append.await_args.kwargs
    assert kwargs["org_id"] == _ORG
    assert kwargs["event_type"] == EXPIRY_EVENT_TYPE
    assert kwargs["resource_type"] == "hitl_claim"
    assert kwargs["resource_id"] == _CLAIM
    payload = kwargs["payload_json"]
    assert payload["pipeline_run_id"] == str(_RUN)
    assert payload["node_id"] == _GATE
    assert payload["outcome"] == EXPIRY_OUTCOME
    assert payload["decision"] is None
    assert payload["cancel_reason"] == CANCEL_REASON_HITL_REVIEW_EXPIRED
    assert payload["summary"] == "HITL review expired unanswered; no verdict was recorded"


def test_decided_gate_is_excluded_by_the_selection_predicate() -> None:
    """A gate decided before the sweep ran keeps its decision event.

    The exclusion is in the SQL selection (not a post-read filter), so a
    decided gate is never returned and therefore never re-labelled as expired.
    """
    stmt = review_expiry._expiry_gate_select(_ORG, [_RUN])
    sql = str(stmt)

    assert "hitl_claims.decision IS NULL" in sql
    # The run-marker re-verification: the sweep hands EVERY terminalised run id
    # here, so a non-expiry cancellation must be filtered out by predicate.
    assert "runs.cancel_reason" in sql
    assert "runs.error_code" in sql
    assert "hitl_claims.organisation_id" in sql
    assert "hitl_claims.run_id IN" in sql


async def test_decided_gate_records_no_expiry_event() -> None:
    """When the selection returns nothing (a decided gate), no append happens."""
    factory = _factory([])
    append = AsyncMock(return_value=True)

    with (
        patch.object(review_expiry, "set_rls_org", new=AsyncMock()),
        patch.object(review_expiry, "append_background_audit_event", append),
    ):
        recorded = await record_review_expiry_outcomes(factory, [(_RUN, _ORG)])

    assert recorded == 0
    append.assert_not_awaited()


async def test_audit_failure_logs_under_the_dedicated_key_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail open WITH a log: the cancellation is already committed."""
    factory = _factory([_undecided_gate()])

    with (
        patch.object(review_expiry, "set_rls_org", new=AsyncMock()),
        patch.object(
            review_expiry,
            "append_background_audit_event",
            AsyncMock(side_effect=RuntimeError("db down")),
        ),
        caplog.at_level(logging.WARNING),
    ):
        recorded = await record_review_expiry_outcomes(factory, [(_RUN, _ORG)])

    assert recorded == 0
    assert any(EXPIRY_AUDIT_LOG_KEY in record.message for record in caplog.records)


async def test_selection_failure_logs_and_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    """A failed read is loud and swallowed — the cancellation stands."""
    factory = _factory([])
    factory.return_value.__aenter__.return_value.execute = AsyncMock(side_effect=RuntimeError("read failed"))

    with (
        patch.object(review_expiry, "set_rls_org", new=AsyncMock()),
        caplog.at_level(logging.WARNING),
    ):
        recorded = await record_review_expiry_outcomes(factory, [(_RUN, _ORG)])

    assert recorded == 0
    assert any(EXPIRY_AUDIT_LOG_KEY in record.message for record in caplog.records)


async def test_empty_input_opens_no_session() -> None:
    factory = MagicMock()

    recorded = await record_review_expiry_outcomes(factory, [])

    assert recorded == 0
    factory.assert_not_called()


async def test_cancelled_error_during_selection_propagates() -> None:
    """Cancellation is never swallowed — the tick is being torn down."""
    factory = _factory([])
    factory.return_value.__aenter__.return_value.execute = AsyncMock(side_effect=asyncio.CancelledError)

    with (
        patch.object(review_expiry, "set_rls_org", new=AsyncMock()),
        pytest.raises(asyncio.CancelledError),
    ):
        await record_review_expiry_outcomes(factory, [(_RUN, _ORG)])


async def test_cancelled_error_during_append_propagates() -> None:
    """Cancellation on the append path propagates for the same reason."""
    factory = _factory([_undecided_gate()])

    with (
        patch.object(review_expiry, "set_rls_org", new=AsyncMock()),
        patch.object(
            review_expiry,
            "append_background_audit_event",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await record_review_expiry_outcomes(factory, [(_RUN, _ORG)])


async def test_append_returning_false_is_not_counted() -> None:
    """A fail-open append that records nothing is not reported as recorded."""
    factory = _factory([_undecided_gate()])
    append = AsyncMock(return_value=False)

    with (
        patch.object(review_expiry, "set_rls_org", new=AsyncMock()),
        patch.object(review_expiry, "append_background_audit_event", append),
    ):
        recorded = await record_review_expiry_outcomes(factory, [(_RUN, _ORG)])

    assert recorded == 0
    append.assert_awaited_once()
