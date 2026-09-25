"""Unit tests for FAR-1161 accountability-owner wiring in ``db.crud.pipeline``.

Covers ``audit_accountability_owner_change`` (no-op vs write, all three
transitions) and the owner/scope-change eligibility + audit branches threaded
through ``create_pipeline`` and ``update_pipeline``. The session and the
eligibility helper are mocked — the invariant itself is exercised in
``test_pipeline_owner.py`` and the RLS integration suite.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.pipeline import (
    audit_accountability_owner_change,
    create_pipeline,
    update_pipeline,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
_OTHER_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000dd")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ab")


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


def _pipeline_stub(
    *,
    business_owner_id: object = None,
    reliability_owner_id: object = None,
    visibility: object = "org",
    owner_team_id: object = None,
) -> MagicMock:
    pipeline = MagicMock()
    pipeline.business_owner_id = business_owner_id
    pipeline.reliability_owner_id = reliability_owner_id
    pipeline.visibility = visibility
    pipeline.owner_team_id = owner_team_id
    pipeline.organisation_id = _ORG_ID
    pipeline.circuit_breaker_threshold = None
    pipeline.circuit_breaker_tripped = False
    return pipeline


# ---------------------------------------------------------------------------
# audit_accountability_owner_change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAuditAccountabilityOwnerChange:
    async def test_noop_when_value_unchanged(self) -> None:
        session = _mock_session()
        with patch("modulo.db.crud.pipeline.append_audit_event", new=AsyncMock()) as audit:
            result = await audit_accountability_owner_change(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                actor_user_id=_ACTOR_ID,
                field="business_owner_id",
                previous=_OWNER_ID,
                new=_OWNER_ID,
            )
        assert result is False
        audit.assert_not_awaited()

    async def test_noop_when_both_none(self) -> None:
        session = _mock_session()
        with patch("modulo.db.crud.pipeline.append_audit_event", new=AsyncMock()) as audit:
            result = await audit_accountability_owner_change(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                actor_user_id=None,
                field="reliability_owner_id",
                previous=None,
                new=None,
            )
        assert result is False
        audit.assert_not_awaited()

    async def test_assignment_writes_event(self) -> None:
        session = _mock_session()
        with patch("modulo.db.crud.pipeline.append_audit_event", new=AsyncMock()) as audit:
            result = await audit_accountability_owner_change(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                actor_user_id=_ACTOR_ID,
                field="business_owner_id",
                previous=None,
                new=_OWNER_ID,
                request_id="req-1",
            )
        assert result is True
        payload = audit.await_args.kwargs
        assert payload["event_type"] == "pipeline.business_owner_changed"
        assert payload["payload_json"]["previous_owner_id"] is None
        assert payload["payload_json"]["new_owner_id"] == str(_OWNER_ID)
        assert payload["payload_json"]["changed_by"] == str(_ACTOR_ID)
        assert payload["request_id"] == "req-1"

    async def test_clear_writes_event_with_null_new_and_actor(self) -> None:
        session = _mock_session()
        with patch("modulo.db.crud.pipeline.append_audit_event", new=AsyncMock()) as audit:
            result = await audit_accountability_owner_change(
                session,
                org_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
                actor_user_id=None,
                field="reliability_owner_id",
                previous=_OWNER_ID,
                new=None,
            )
        assert result is True
        payload = audit.await_args.kwargs
        assert payload["event_type"] == "pipeline.reliability_owner_changed"
        assert payload["payload_json"]["previous_owner_id"] == str(_OWNER_ID)
        assert payload["payload_json"]["new_owner_id"] is None
        assert payload["payload_json"]["changed_by"] is None


# ---------------------------------------------------------------------------
# create_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCreatePipelineOwners:
    async def test_validates_each_owner_and_audits_assignments(self) -> None:
        session = _mock_session()
        with (
            patch("modulo.db.crud.pipeline.validate_accountability_owner", new=AsyncMock()) as validate,
            patch("modulo.db.crud.pipeline.audit_accountability_owner_change", new=AsyncMock()) as audit,
        ):
            pipeline = await create_pipeline(
                session,
                org_id=_ORG_ID,
                name="owned",
                account_id=_ACTOR_ID,
                business_owner_id=_OWNER_ID,
                reliability_owner_id=_OTHER_OWNER_ID,
            )
        assert pipeline is not None
        assert validate.await_count == 2
        validated_fields = {call.kwargs["field"] for call in validate.await_args_list}
        assert validated_fields == {"business_owner_id", "reliability_owner_id"}
        assert audit.await_count == 2
        audited = {(c.kwargs["field"], c.kwargs["new"]) for c in audit.await_args_list}
        assert audited == {("business_owner_id", _OWNER_ID), ("reliability_owner_id", _OTHER_OWNER_ID)}
        assert all(c.kwargs["previous"] is None for c in audit.await_args_list)

    async def test_no_owners_validates_but_writes_no_audit(self) -> None:
        session = _mock_session()
        with (
            patch("modulo.db.crud.pipeline.validate_accountability_owner", new=AsyncMock()) as validate,
            patch("modulo.db.crud.pipeline.audit_accountability_owner_change", new=AsyncMock()) as audit,
        ):
            await create_pipeline(session, org_id=_ORG_ID, name="unowned", account_id=_ACTOR_ID)
        assert validate.await_count == 2
        audit.assert_not_awaited()


# ---------------------------------------------------------------------------
# update_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestUpdatePipelineOwners:
    async def test_payload_owner_is_validated_and_audited(self) -> None:
        session = _mock_session()
        pipeline = _pipeline_stub(business_owner_id=_OTHER_OWNER_ID)
        with (
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline.validate_accountability_owner", new=AsyncMock()) as validate,
            patch("modulo.db.crud.pipeline.audit_accountability_owner_change", new=AsyncMock()) as audit,
        ):
            result = await update_pipeline(
                session,
                _PIPELINE_ID,
                {"business_owner_id": _OWNER_ID},
                org_id=_ORG_ID,
                account_id=_ACTOR_ID,
            )
        assert result is pipeline
        validate.assert_awaited_once()
        assert validate.await_args.kwargs["owner_account_id"] == _OWNER_ID
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["previous"] == _OTHER_OWNER_ID
        assert audit.await_args.kwargs["new"] == _OWNER_ID

    async def test_owner_clear_is_audited(self) -> None:
        session = _mock_session()
        pipeline = _pipeline_stub(business_owner_id=_OWNER_ID)
        with (
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline.validate_accountability_owner", new=AsyncMock()),
            patch("modulo.db.crud.pipeline.audit_accountability_owner_change", new=AsyncMock()) as audit,
        ):
            await update_pipeline(
                session,
                _PIPELINE_ID,
                {"business_owner_id": None},
                org_id=_ORG_ID,
                account_id=_ACTOR_ID,
            )
        assert audit.await_args.kwargs["previous"] == _OWNER_ID
        assert audit.await_args.kwargs["new"] is None

    async def test_scope_change_revalidates_stored_uuid_owners(self) -> None:
        session = _mock_session()
        pipeline = _pipeline_stub(
            business_owner_id=_OWNER_ID,
            reliability_owner_id=_OTHER_OWNER_ID,
            visibility="org",
        )
        with (
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline.validate_accountability_owner", new=AsyncMock()) as validate,
            patch("modulo.db.crud.pipeline.audit_accountability_owner_change", new=AsyncMock()) as audit,
            patch("modulo.db.crud.pipeline.count_active_runs_for_pipeline", new=AsyncMock(return_value=0)),
            patch("modulo.db.crud.pipeline.append_audit_event", new=AsyncMock()),
        ):
            await update_pipeline(
                session,
                _PIPELINE_ID,
                {"visibility": "team", "owner_team_id": _TEAM_ID},
                org_id=_ORG_ID,
                account_id=_ACTOR_ID,
            )
        assert validate.await_count == 2
        candidates = {c.kwargs["field"]: c.kwargs["owner_account_id"] for c in validate.await_args_list}
        assert candidates["business_owner_id"] == _OWNER_ID
        assert candidates["reliability_owner_id"] == _OTHER_OWNER_ID
        assert all(c.kwargs["visibility"] == "team" for c in validate.await_args_list)
        # Scope-only change carries no owner update -> no owner audit.
        audit.assert_not_awaited()

    async def test_scope_change_treats_non_uuid_stored_owner_as_unassigned(self) -> None:
        session = _mock_session()
        pipeline = _pipeline_stub(business_owner_id=MagicMock(), reliability_owner_id=None)
        with (
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline.validate_accountability_owner", new=AsyncMock()) as validate,
            patch("modulo.db.crud.pipeline.audit_accountability_owner_change", new=AsyncMock()),
        ):
            await update_pipeline(
                session,
                _PIPELINE_ID,
                {"owner_team_id": _TEAM_ID},
                org_id=None,
                account_id=_ACTOR_ID,
            )
        candidates = {c.kwargs["field"]: c.kwargs["owner_account_id"] for c in validate.await_args_list}
        assert candidates["business_owner_id"] is None
        assert candidates["reliability_owner_id"] is None
        assert all(c.kwargs["org_id"] == _ORG_ID for c in validate.await_args_list)

    async def test_owner_update_without_org_id_uses_pipeline_org(self) -> None:
        session = _mock_session()
        pipeline = _pipeline_stub(business_owner_id=None)
        with (
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline.validate_accountability_owner", new=AsyncMock()) as validate,
            patch("modulo.db.crud.pipeline.audit_accountability_owner_change", new=AsyncMock()) as audit,
        ):
            await update_pipeline(session, _PIPELINE_ID, {"business_owner_id": _OWNER_ID}, account_id=None)
        assert validate.await_args.kwargs["org_id"] == _ORG_ID
        assert audit.await_args.kwargs["org_id"] == _ORG_ID
        assert audit.await_args.kwargs["actor_user_id"] is None

    async def test_owner_update_with_non_str_visibility_defaults_to_org(self) -> None:
        session = _mock_session()
        pipeline = _pipeline_stub(business_owner_id=None, visibility=MagicMock())
        with (
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline.validate_accountability_owner", new=AsyncMock()) as validate,
            patch("modulo.db.crud.pipeline.audit_accountability_owner_change", new=AsyncMock()),
        ):
            await update_pipeline(session, _PIPELINE_ID, {"business_owner_id": _OWNER_ID}, org_id=_ORG_ID)
        assert validate.await_args.kwargs["visibility"] == "org"

    async def test_unrelated_update_reads_no_scope_attributes(self) -> None:
        session = _mock_session()
        pipeline = _pipeline_stub()
        with (
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline.validate_accountability_owner", new=AsyncMock()) as validate,
            patch("modulo.db.crud.pipeline.audit_accountability_owner_change", new=AsyncMock()) as audit,
        ):
            await update_pipeline(session, _PIPELINE_ID, {"name": "renamed"})
        validate.assert_not_awaited()
        audit.assert_not_awaited()
