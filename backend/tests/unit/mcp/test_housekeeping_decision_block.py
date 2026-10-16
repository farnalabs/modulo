"""MCP housekeeping tool surfaces decision-record blocks (FAR-1102 chunk 4).

Covers acceptance criterion 12 (unit-level half): the MCP housekeeping delete
path classifies a policy_gate_decisions RESTRICT violation as a
blocked-with-reason error entry carrying ``blocked_by:
policy_gate_decisions`` and an operator-actionable message.

The ``perform_housekeeping`` MCP tool deletes via
``_delete_housekeeping_groups`` -> ``_delete_housekeeping_group``; these tests
exercise the grouping delete helper directly with a session whose savepoint
RELEASE raises an IntegrityError — the real point at which the flushed DELETE
is refused (FAR-1857), so the mock reproduces the count-only-after-release path
rather than failing at the SELECT.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from modulo.api.mcp_server import _delete_housekeeping_group
from modulo.db.models.pipeline import Pipeline


def _mock_session(savepoint_error: Exception | None = None) -> MagicMock:
    """Session whose savepoint RELEASE raises *savepoint_error*.

    The real failure mode is at ``begin_nested().__aexit__``: SQLAlchemy
    flushes the pending DELETE when the nested transaction releases, so an
    ``ON DELETE RESTRICT`` refusal surfaces there — not at the ``session.execute``
    SELECT. A mock that raised from ``execute`` never exercised the
    count-only-after-release path and would mask a regression (FAR-1857), so
    this raises from ``__aexit__`` instead.
    """
    session = MagicMock()
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    if savepoint_error is None:
        nested.__aexit__ = AsyncMock(return_value=False)
    else:
        nested.__aexit__ = AsyncMock(side_effect=savepoint_error)
    session.begin_nested = MagicMock(return_value=nested)
    found = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=found)))
    session.delete = AsyncMock()
    return session


def _decision_fk_error() -> IntegrityError:
    exc = IntegrityError("stmt", {}, Exception("violates foreign key constraint"))
    exc.constraint_name = "fk_policy_gate_decisions_eval_org"
    return exc


def _unrelated_fk_error() -> IntegrityError:
    exc = IntegrityError("stmt", {}, Exception("violates foreign key constraint"))
    exc.constraint_name = "fk_eval_results_run_org"
    return exc


class TestDeleteHousekeepingGroupDecisionBlock:
    @pytest.mark.asyncio
    async def test_decision_fk_produces_blocked_with_reason_entry(self) -> None:
        errors: list[dict[str, str]] = []
        session = _mock_session(_decision_fk_error())

        deleted = await _delete_housekeeping_group(
            session,
            "pipelines",
            [str(uuid.uuid4())],
            Pipeline,
            uuid.uuid4(),
            errors,
        )

        # The delete was attempted (so the error came from the savepoint
        # release, not the SELECT) but the refusal means it is NOT counted.
        session.delete.assert_awaited_once()
        assert deleted == 0
        assert len(errors) == 1
        entry = errors[0]
        assert entry["blocked_by"] == "policy_gate_decisions"
        assert "archive or purge" in entry["error"]
        assert "policy_gate_decisions" in entry["error"]

    @pytest.mark.asyncio
    async def test_unrelated_fk_gets_generic_error(self) -> None:
        errors: list[dict[str, str]] = []
        session = _mock_session(_unrelated_fk_error())

        deleted = await _delete_housekeeping_group(
            session,
            "pipelines",
            [str(uuid.uuid4())],
            Pipeline,
            uuid.uuid4(),
            errors,
        )

        assert deleted == 0
        assert errors[0]["error"] == "Foreign key constraint violation"
        assert "blocked_by" not in errors[0]

    @pytest.mark.asyncio
    async def test_success_path_deletes_and_reports_no_errors(self) -> None:
        errors: list[dict[str, str]] = []
        session = _mock_session()
        found = session.execute.return_value.scalar_one_or_none.return_value

        eid = str(uuid.uuid4())
        deleted = await _delete_housekeeping_group(session, "pipelines", [eid], Pipeline, uuid.uuid4(), errors)

        assert deleted == 1
        assert not errors
        session.delete.assert_awaited_once_with(found)

    @pytest.mark.asyncio
    async def test_substring_guard_does_not_overclassify_unrelated_constraints(self) -> None:
        """Only constraint names matching the decision-record FK vocabulary (exact or
        by substring) get the blocked_with_reason treatment; anything else is generic."""
        errors: list[dict[str, str]] = []
        exc = IntegrityError("stmt", {}, Exception("duplicate key"))
        exc.constraint_name = "ix_tmp_runs_node_id_created_at"
        session = _mock_session(exc)

        deleted = await _delete_housekeeping_group(
            session,
            "pipelines",
            [str(uuid.uuid4())],
            Pipeline,
            uuid.uuid4(),
            errors,
        )

        assert deleted == 0
        assert errors[0]["error"] == "Foreign key constraint violation"
