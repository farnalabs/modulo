"""Criterion 14: library uninstall partial success with decision-record blocks.

A collection where one pipeline has recorded ``PolicyGateDecision`` rows and
another does not: the unblocked pipeline is deleted, the blocked one is
reported blocked-with-reason (``blocked_by: "policy_gate_decisions"``),
and the response reports BOTH outcomes.  A swallowed warning must FAIL.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from modulo.core.library_service.uninstall import uninstall_collection


def _decision_fk_error() -> IntegrityError:
    exc = IntegrityError(
        "DELETE FROM pipelines WHERE id = :id",
        {},
        Exception('violates foreign key constraint "fk_policy_gate_decisions_gate_org"'),
    )
    exc.constraint_name = "fk_policy_gate_decisions_gate_org"
    return exc


class _FakeSavepoint:
    """Context manager that either succeeds or raises a decision-FK IntegrityError."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self._should_fail = should_fail

    async def __aenter__(self):
        if self._should_fail:
            raise _decision_fk_error()
        return self

    async def __aexit__(self, *args):
        return False


def _make_install_row(install_id: uuid.UUID, org_id: uuid.UUID, collection_id: uuid.UUID):
    install = MagicMock()
    install.id = install_id
    install.organisation_id = org_id
    install.collection_id = collection_id
    return install


def _make_entity_row(entity_type: str, entity_id: uuid.UUID, install_id: uuid.UUID):
    row = MagicMock()
    row.entity_type = entity_type
    row.entity_id = entity_id
    row.install_id = install_id
    return row


def _build_session(
    install,
    entity_rows: list,
    savepoint_fail_indices: frozenset[int] | None = None,
):
    """Build a mock session wired to exercise uninstall_collection.

    ``savepoint_fail_indices`` is a set of 0-based savepoint call indices
    at which ``begin_nested()`` should raise a decision-FK IntegrityError.
    By default (None) all savepoints succeed.
    """
    fail_at = savepoint_fail_indices or frozenset()
    session = MagicMock()
    session.get = AsyncMock(return_value=install)

    # _load_entity_rows: SELECT returns the entity rows.
    # The code does: list(result.scalars())  — scalars() must be iterable.
    select_result = MagicMock()
    select_result.scalars.return_value = entity_rows  # list is directly iterable
    session.execute = AsyncMock(return_value=select_result)

    # _entity_exists + _check_unmodified + _delete_entity all use session.scalar.
    # For pipelines, they do select(Pipeline).where(Pipeline.id == entity_id).
    # Return a mock entity whose collection_install_id matches the install
    # (i.e. "unmodified") for any entity that exists.
    async def _scalar(stmt, *args, **kwargs):
        return MagicMock(collection_install_id=install.id)

    session.scalar = AsyncMock(side_effect=_scalar)

    # begin_nested: per-item savepoint, failing at specified indices
    call_count = 0

    def _begin_nested():
        nonlocal call_count
        idx = call_count
        call_count += 1
        return _FakeSavepoint(should_fail=(idx in fail_at))

    session.begin_nested = _begin_nested

    session.delete = AsyncMock()
    session.flush = AsyncMock()

    return session


class TestUninstallCollectionPartialDecisionBlock:
    """Criterion 14: unblocked pipeline deleted, blocked pipeline reported."""

    @pytest.mark.asyncio
    async def test_one_blocked_one_deleted(self) -> None:
        org_id = uuid.uuid4()
        install_id = uuid.uuid4()
        collection_id = uuid.uuid4()

        pipeline_unblocked = uuid.uuid4()
        pipeline_blocked = uuid.uuid4()

        install = _make_install_row(install_id, org_id, collection_id)
        entity_unblocked = _make_entity_row("pipeline", pipeline_unblocked, install_id)
        entity_blocked = _make_entity_row("pipeline", pipeline_blocked, install_id)

        # Unblocked (index 0) succeeds; blocked (index 1) raises FK error
        session = _build_session(
            install,
            [entity_unblocked, entity_blocked],
            savepoint_fail_indices=frozenset({1}),
        )

        result = await uninstall_collection(session, org_id, install_id, collection_id)

        # Unblocked pipeline: deleted
        assert len(result["deleted"]) == 1
        assert result["deleted"][0]["entity_type"] == "pipeline"
        assert result["deleted"][0]["entity_id"] == str(pipeline_unblocked)

        # Blocked pipeline: blocked-with-reason
        assert len(result["blocked"]) == 1
        assert result["blocked"][0]["entity_type"] == "pipeline"
        assert result["blocked"][0]["entity_id"] == str(pipeline_blocked)
        assert result["blocked"][0]["blocked_by"] == "policy_gate_decisions"
        assert "archive or purge" in result["blocked"][0]["error"]
        assert "policy_gate_decisions" in result["blocked"][0]["error"]

        # No detachments
        assert not result["detached"]

        # The install record itself is deleted (called for unblocked pipeline + install)
        session.delete.assert_any_await(install)

    @pytest.mark.asyncio
    async def test_blocked_message_names_remediation(self) -> None:
        """The blocked entry's error message names the remediation."""
        org_id = uuid.uuid4()
        install_id = uuid.uuid4()
        collection_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()

        install = _make_install_row(install_id, org_id, collection_id)
        entity = _make_entity_row("pipeline", pipeline_id, install_id)

        session = _build_session(
            install,
            [entity],
            savepoint_fail_indices=frozenset({0}),
        )

        result = await uninstall_collection(session, org_id, install_id, collection_id)

        assert len(result["blocked"]) == 1
        entry = result["blocked"][0]
        assert entry["blocked_by"] == "policy_gate_decisions"
        assert "governance decision records present" in entry["error"]
        assert "archive or purge policy_gate_decisions first" in entry["error"]

    @pytest.mark.asyncio
    async def test_non_decision_integrity_error_is_reraised(self) -> None:
        """An IntegrityError from an unrelated constraint must NOT be swallowed."""
        org_id = uuid.uuid4()
        install_id = uuid.uuid4()
        collection_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()

        install = _make_install_row(install_id, org_id, collection_id)
        entity = _make_entity_row("pipeline", pipeline_id, install_id)

        unrelated_error = IntegrityError("stmt", {}, Exception("duplicate key"))
        unrelated_error.constraint_name = "ix_unrelated_constraint"

        session = _build_session(install, [entity])

        # Override begin_nested to raise an unrelated IntegrityError
        def _raising_begin_nested():
            class _Raising:
                async def __aenter__(self):
                    raise unrelated_error

                async def __aexit__(self, *args):
                    return False

            return _Raising()

        session.begin_nested = _raising_begin_nested

        with pytest.raises(IntegrityError):
            await uninstall_collection(session, org_id, install_id, collection_id)

    @pytest.mark.asyncio
    async def test_swallowed_warning_would_fail_criterion(self) -> None:
        """Verify that when a decision-FK error IS classified as blocked,
        the response contains the blocked entry (not silently swallowed)."""
        org_id = uuid.uuid4()
        install_id = uuid.uuid4()
        collection_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()

        install = _make_install_row(install_id, org_id, collection_id)
        entity = _make_entity_row("pipeline", pipeline_id, install_id)

        session = _build_session(
            install,
            [entity],
            savepoint_fail_indices=frozenset({0}),
        )

        result = await uninstall_collection(session, org_id, install_id, collection_id)

        # If the warning was swallowed (no blocked entry), this assertion fails
        assert len(result["blocked"]) == 1, (
            "A swallowed decision-FK IntegrityError would produce an empty blocked list — "
            "the criterion requires the blocked entry to be present with blocked_by and remediation message"
        )
