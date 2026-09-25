"""Decision-record purge + FK-classification helpers (FAR-1102 chunk 4).

Unit tests for ``modulo.db.crud.policy_gate_decision`` and the wiring of
``purge_org_decision_records`` into both org-deletion paths:

- ``is_policy_gate_decision_fk_error`` classification (exact constraint names,
  substring fallback, non-IntegrityError types, missing constraint metadata).
- ``purge_org_decision_records`` deletes all decision rows for the org.
- Path 3 (``org_deletion.confirm_org_deletion``) purges decision records
  BEFORE the org hard-delete.
- Path 6 (``organisation.delete_organisation``) purges decision records
  BEFORE the org hard-delete.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

_ORG_ID = uuid.uuid4()


def _decision_fk_error(constraint: str) -> IntegrityError:
    exc = IntegrityError("stmt", {}, Exception("violates foreign key constraint"))
    exc.constraint_name = constraint
    return exc


class TestIsPolicyGateDecisionFkError:
    def test_exact_gate_org_constraint(self) -> None:
        from modulo.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error

        exc = _decision_fk_error("fk_policy_gate_decisions_gate_org")
        assert is_policy_gate_decision_fk_error(exc) is True

    def test_exact_eval_org_constraint(self) -> None:
        from modulo.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error

        exc = _decision_fk_error("fk_policy_gate_decisions_eval_org")
        assert is_policy_gate_decision_fk_error(exc) is True

    def test_unknown_constraint_name_substring_match(self) -> None:
        from modulo.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error

        exc = _decision_fk_error("RS_POLICY_GATE_DECISIONS_EVAL_ORG")
        assert is_policy_gate_decision_fk_error(exc) is True

    def test_unrelated_constraint(self) -> None:
        from modulo.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error

        exc = _decision_fk_error("fk_eval_results_run_org")
        assert is_policy_gate_decision_fk_error(exc) is False

    def test_missing_constraint_name_is_not_classified(self) -> None:
        from modulo.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error

        exc = IntegrityError("stmt", {}, Exception("violates foreign key constraint"))
        assert is_policy_gate_decision_fk_error(exc) is False

    def test_non_integrity_error_is_never_classified(self) -> None:
        from modulo.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error

        err = RuntimeError("connection reset")
        err.constraint_name = "fk_policy_gate_decisions_gate_org"  # type: ignore[attr-defined]
        assert is_policy_gate_decision_fk_error(err) is False


class TestPurgeOrgDecisionRecords:
    @pytest.mark.asyncio
    async def test_deletes_rows_for_org(self) -> None:
        from sqlalchemy import Delete

        from modulo.db.crud.policy_gate_decision import purge_org_decision_records

        result = MagicMock(rowcount=3)
        session = MagicMock(spec=AsyncSession)
        session.execute = AsyncMock(return_value=result)

        count = await purge_org_decision_records(session, _ORG_ID)

        assert count == 3
        stmt = session.execute.await_args.args[0]
        assert isinstance(stmt, Delete)

    @pytest.mark.asyncio
    async def test_zero_rows_returns_zero(self) -> None:
        from modulo.db.crud.policy_gate_decision import purge_org_decision_records

        result = MagicMock(rowcount=0)
        session = MagicMock(spec=AsyncSession)
        session.execute = AsyncMock(return_value=result)

        count = await purge_org_decision_records(session, _ORG_ID)

        assert count == 0


_ORG = MagicMock()
_ORG.id = _ORG_ID


class TestOrgDeletionPath3PurgesFirst:
    """confirm_org_deletion (Path 3): purge precedes the org hard-delete."""

    @pytest.mark.asyncio
    async def test_purge_called_before_org_hard_delete(self) -> None:
        import modulo.db.crud.org_deletion as org_deletion

        order: list[str] = []
        purge = AsyncMock(side_effect=lambda *a, **kw: order.append("purge"))
        org = MagicMock()
        org.id = _ORG_ID
        org.deletion_token = "tok"
        org.deletion_token_expires_at = None

        session = MagicMock(spec=AsyncSession)

        async def _execute(*args: object, **kwargs: object) -> MagicMock:
            return MagicMock(scalar_one_or_none=MagicMock(return_value=org))

        session.execute = AsyncMock(side_effect=_execute)

        async def _delete(obj: object) -> None:
            order.append("hard_delete")

        session.delete = AsyncMock(side_effect=_delete)
        session.flush = AsyncMock()

        with (
            patch(
                "modulo.db.crud.org_deletion._count_non_terminal_runs",
                AsyncMock(return_value=0),
            ),
            patch("modulo.db.crud.org_deletion._abort_org_live_sandboxes", AsyncMock()),
            patch(
                "modulo.db.crud.org_deletion.batch_delete_old_terminal_runs",
                AsyncMock(return_value=0),
            ),
            patch("modulo.db.crud.policy_gate_decision.purge_org_decision_records", purge),
        ):
            result = await org_deletion.confirm_org_deletion(session, _ORG_ID, "never-checked", immediate=True)

        assert purge.await_count == 1
        assert order == ["purge", "hard_delete"]
        assert result["deleted_organisation_id"] == str(_ORG_ID)

    @pytest.mark.asyncio
    async def test_purge_ran_for_force_with_live_runs(self) -> None:
        import modulo.db.crud.org_deletion as org_deletion

        purge = AsyncMock()
        org = MagicMock()
        org.id = _ORG_ID

        session = MagicMock(spec=AsyncSession)
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=org)))
        session.delete = AsyncMock()
        session.flush = AsyncMock()

        with (
            patch("modulo.db.crud.org_deletion._count_non_terminal_runs", AsyncMock(return_value=5)),
            patch("modulo.db.crud.org_deletion._abort_org_live_sandboxes", AsyncMock()),
            patch("modulo.db.crud.org_deletion.batch_delete_old_terminal_runs", AsyncMock(return_value=2)),
            patch("modulo.db.crud.policy_gate_decision.purge_org_decision_records", purge),
        ):
            result = await org_deletion.confirm_org_deletion(session, _ORG_ID, "skipped", immediate=True, force=True)

        purge.assert_awaited_once_with(session, _ORG_ID)
        assert result["hard_deleted_runs"] == 2


class TestOrgDeletionPath6PurgesFirst:
    """delete_organisation (Path 6): purge precedes the org hard-delete."""

    @pytest.mark.asyncio
    async def test_purge_called_before_org_hard_delete(self) -> None:
        import modulo.db.crud.organisation as organisation

        order: list[str] = []
        purge = AsyncMock(side_effect=lambda *a, **kw: order.append("purge"))
        org = MagicMock()
        org.id = _ORG_ID

        session = MagicMock(spec=AsyncSession)
        session.delete = AsyncMock(side_effect=lambda obj: order.append("hard_delete"))
        session.flush = AsyncMock()

        with (
            patch("modulo.db.crud.organisation.get_organisation", AsyncMock(return_value=org)),
            patch("modulo.db.crud.agent_runner_binding.delete_org_binding_rows", AsyncMock()),
            patch("modulo.db.rls.set_rls_org", AsyncMock()),
            patch("modulo.db.rls.set_rls_execution_context", AsyncMock()),
            patch("modulo.db.crud.policy_gate_decision.purge_org_decision_records", purge),
        ):
            deleted = await organisation.delete_organisation(session, _ORG_ID)

        assert deleted is True
        assert purge.await_count == 1
        assert order == ["purge", "hard_delete"]
