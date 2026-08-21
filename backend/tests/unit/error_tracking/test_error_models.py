"""Schema-level and CRUD tests for error tracking models."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from modulo.db.crud.error_tracking import (
    create_error_event,
    get_error_events_by_group,
    get_error_group,
    get_error_group_by_fingerprint,
    get_error_groups,
    update_error_group,
    upsert_error_group,
)
from modulo.db.models import Base, ErrorEvent, ErrorGroup, OrgScoped

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestErrorEvent:
    def test_table_exists(self) -> None:
        assert "error_events" in Base.metadata.tables

    def test_columns(self) -> None:
        cols = Base.metadata.tables["error_events"].c
        assert "id" in cols
        assert "organisation_id" in cols
        assert "fingerprint" in cols
        assert "level" in cols
        assert "message" in cols
        assert "stacktrace" in cols
        assert "context_json" in cols
        assert "source" in cols
        assert "environment" in cols
        assert "version" in cols
        assert "status" in cols
        assert "created_at" in cols
        assert "updated_at" in cols
        assert "resolved_at" in cols

    def test_is_org_scoped(self) -> None:
        assert issubclass(ErrorEvent, OrgScoped)

    def test_fingerprint_not_null(self) -> None:
        col = Base.metadata.tables["error_events"].c["fingerprint"]
        assert not col.nullable

    def test_level_not_null(self) -> None:
        col = Base.metadata.tables["error_events"].c["level"]
        assert not col.nullable

    def test_message_not_null(self) -> None:
        col = Base.metadata.tables["error_events"].c["message"]
        assert not col.nullable

    def test_source_not_null(self) -> None:
        col = Base.metadata.tables["error_events"].c["source"]
        assert not col.nullable

    def test_status_has_server_default(self) -> None:
        col = Base.metadata.tables["error_events"].c["status"]
        assert col.server_default is not None
        assert "new" in str(col.server_default.arg).lower()

    def test_level_check_constraint_exists(self) -> None:
        table = Base.metadata.tables["error_events"]
        check = next(
            (c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_error_events_level"),
            None,
        )
        assert check is not None

    def test_level_check_allows_valid_levels(self) -> None:
        table = Base.metadata.tables["error_events"]
        check = next(
            c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_error_events_level"
        )
        sql = str(check.sqltext)
        for level in ("error", "warning", "critical"):
            assert f"'{level}'" in sql

    def test_source_check_constraint_exists(self) -> None:
        table = Base.metadata.tables["error_events"]
        check = next(
            (c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_error_events_source"),
            None,
        )
        assert check is not None

    def test_source_check_allows_valid_sources(self) -> None:
        table = Base.metadata.tables["error_events"]
        check = next(
            c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_error_events_source"
        )
        sql = str(check.sqltext)
        for source in ("backend", "frontend", "celery", "saq"):
            assert f"'{source}'" in sql

    def test_status_check_constraint_exists(self) -> None:
        table = Base.metadata.tables["error_events"]
        check = next(
            (c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_error_events_status"),
            None,
        )
        assert check is not None

    def test_stacktrace_nullable(self) -> None:
        col = Base.metadata.tables["error_events"].c["stacktrace"]
        assert col.nullable

    def test_context_json_nullable(self) -> None:
        col = Base.metadata.tables["error_events"].c["context_json"]
        assert col.nullable

    def test_resolved_at_nullable(self) -> None:
        col = Base.metadata.tables["error_events"].c["resolved_at"]
        assert col.nullable


class TestErrorGroup:
    def test_table_exists(self) -> None:
        assert "error_groups" in Base.metadata.tables

    def test_columns(self) -> None:
        cols = Base.metadata.tables["error_groups"].c
        assert "id" in cols
        assert "organisation_id" in cols
        assert "fingerprint" in cols
        assert "status" in cols
        assert "first_seen" in cols
        assert "last_seen" in cols
        assert "count" in cols
        assert "level_peak" in cols
        assert "sample_event_id" in cols
        assert "assigned_to" in cols
        assert "created_at" in cols
        assert "updated_at" in cols

    def test_is_org_scoped(self) -> None:
        assert issubclass(ErrorGroup, OrgScoped)

    def test_fingerprint_not_null(self) -> None:
        col = Base.metadata.tables["error_groups"].c["fingerprint"]
        assert not col.nullable

    def test_count_has_server_default(self) -> None:
        col = Base.metadata.tables["error_groups"].c["count"]
        assert col.server_default is not None
        assert "1" in str(col.server_default.arg)

    def test_level_peak_has_server_default(self) -> None:
        col = Base.metadata.tables["error_groups"].c["level_peak"]
        assert col.server_default is not None
        assert "error" in str(col.server_default.arg).lower()

    def test_status_check_constraint_exists(self) -> None:
        table = Base.metadata.tables["error_groups"]
        check = next(
            (c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_error_groups_status"),
            None,
        )
        assert check is not None

    def test_level_peak_check_constraint_exists(self) -> None:
        table = Base.metadata.tables["error_groups"]
        check = next(
            (c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_error_groups_level_peak"),
            None,
        )
        assert check is not None

    def test_unique_fingerprint_per_org(self) -> None:
        table = Base.metadata.tables["error_groups"]
        unique = next(
            (c for c in table.constraints if c.name == "uq_error_groups_org_fingerprint"),
            None,
        )
        assert unique is not None
        cols = [col.name for col in unique.columns]
        assert "organisation_id" in cols
        assert "fingerprint" in cols

    def test_sample_event_id_nullable(self) -> None:
        col = Base.metadata.tables["error_groups"].c["sample_event_id"]
        assert col.nullable

    def test_assigned_to_nullable(self) -> None:
        col = Base.metadata.tables["error_groups"].c["assigned_to"]
        assert col.nullable


class TestCreateErrorEvent:
    async def test_creates_event_with_required_fields(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        event = await create_error_event(
            session=session,
            org_id=_ORG_ID,
            fingerprint="abc123",
            level="error",
            message="Something went wrong",
            source="backend",
        )
        assert event.organisation_id == _ORG_ID
        assert event.fingerprint == "abc123"
        assert event.level == "error"
        assert event.message == "Something went wrong"
        assert event.source == "backend"
        session.add.assert_called_once_with(event)
        session.flush.assert_awaited_once()

    async def test_creates_event_with_optional_fields(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        event = await create_error_event(
            session=session,
            org_id=_ORG_ID,
            fingerprint="def456",
            level="critical",
            message="Critical failure",
            source="frontend",
            stacktrace="Traceback...",
            environment="production",
            version="1.2.3",
            context_json={"url": "/api/test", "user_id": "user_1"},
        )
        assert event.stacktrace == "Traceback..."
        assert event.environment == "production"
        assert event.version == "1.2.3"
        assert event.context_json == {"url": "/api/test", "user_id": "user_1"}


class TestGetErrorGroupByFingerprint:
    async def test_returns_none_when_not_found(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock
        result = await get_error_group_by_fingerprint(session=session, org_id=_ORG_ID, fingerprint="nonexistent")
        assert result is None

    async def test_returns_group_when_found(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        expected = MagicMock(spec=ErrorGroup)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = expected
        session.execute.return_value = result_mock
        result = await get_error_group_by_fingerprint(session=session, org_id=_ORG_ID, fingerprint="exists")
        assert result is expected


class TestUpsertErrorGroup:
    async def test_creates_new_group(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        not_found_mock = MagicMock()
        not_found_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = not_found_mock
        group = await upsert_error_group(session=session, org_id=_ORG_ID, fingerprint="abc123", level="error")
        assert group.organisation_id == _ORG_ID
        assert group.fingerprint == "abc123"
        assert group.level_peak == "error"
        session.add.assert_called_once()
        added = session.add.call_args.args[0]
        assert added.organisation_id == _ORG_ID
        assert added.fingerprint == "abc123"
        assert added.level_peak == "error"
        assert added.sample_event_id is None
        session.flush.assert_awaited()

    async def test_increments_count_for_existing_group(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        existing = MagicMock(spec=ErrorGroup)
        existing.count = 1
        existing.level_peak = "error"
        found_mock = MagicMock()
        found_mock.scalar_one_or_none.return_value = existing
        session.execute.return_value = found_mock
        await upsert_error_group(session=session, org_id=_ORG_ID, fingerprint="abc123", level="warning")
        assert existing.count == 2
        session.flush.assert_awaited()

    async def test_promotes_level_peak(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        existing = MagicMock(spec=ErrorGroup)
        existing.count = 1
        existing.level_peak = "warning"
        found_mock = MagicMock()
        found_mock.scalar_one_or_none.return_value = existing
        session.execute.return_value = found_mock
        await upsert_error_group(session=session, org_id=_ORG_ID, fingerprint="abc123", level="critical")
        assert existing.level_peak == "critical"

    async def test_does_not_demote_level_peak(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        existing = MagicMock(spec=ErrorGroup)
        existing.count = 1
        existing.level_peak = "critical"
        found_mock = MagicMock()
        found_mock.scalar_one_or_none.return_value = existing
        session.execute.return_value = found_mock
        await upsert_error_group(session=session, org_id=_ORG_ID, fingerprint="abc123", level="warning")
        assert existing.level_peak == "critical"


class TestGetErrorGroups:
    async def test_returns_empty_list_when_no_groups(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        session.execute.return_value = result_mock
        result = await get_error_groups(session=session, org_id=_ORG_ID)
        assert result == []

    async def test_returns_groups_filtered_by_org(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        expected = [MagicMock(spec=ErrorGroup), MagicMock(spec=ErrorGroup)]
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = expected
        session.execute.return_value = result_mock
        result = await get_error_groups(session=session, org_id=_ORG_ID)
        assert len(result) == 2


class TestGetErrorGroup:
    async def test_returns_none_when_not_found(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock
        result = await get_error_group(session=session, org_id=_ORG_ID, group_id=uuid.uuid4())
        assert result is None

    async def test_returns_group_when_found(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        expected = MagicMock(spec=ErrorGroup)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = expected
        session.execute.return_value = result_mock
        result = await get_error_group(session=session, org_id=_ORG_ID, group_id=uuid.uuid4())
        assert result is expected


class TestGetErrorEventsByGroup:
    async def test_returns_empty_when_group_not_found(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock
        result = await get_error_events_by_group(session=session, org_id=_ORG_ID, group_id=uuid.uuid4())
        assert result == []

    async def test_returns_events_for_group(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        group_mock = MagicMock(spec=ErrorGroup)
        group_mock.fingerprint = "test-fingerprint"
        group_result = MagicMock()
        group_result.scalar_one_or_none.return_value = group_mock
        events_result = MagicMock()
        events_result.scalars.return_value.all.return_value = []
        session.execute.side_effect = [group_result, events_result]
        result = await get_error_events_by_group(session=session, org_id=_ORG_ID, group_id=uuid.uuid4())
        assert result == []


class TestUpdateErrorGroup:
    async def test_updates_status(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        group = MagicMock(spec=ErrorGroup)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = group
        session.execute.return_value = result_mock
        await update_error_group(session=session, org_id=_ORG_ID, group_id=uuid.uuid4(), status="resolved")
        assert group.status == "resolved"

    async def test_updates_assigned_to(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        group = MagicMock(spec=ErrorGroup)
        assignee = uuid.uuid4()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = group
        session.execute.return_value = result_mock
        await update_error_group(session=session, org_id=_ORG_ID, group_id=uuid.uuid4(), assigned_to=assignee)
        assert group.assigned_to == assignee

    async def test_raises_when_group_not_found(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock
        with pytest.raises(ValueError, match="ErrorGroup not found"):
            await update_error_group(session=session, org_id=_ORG_ID, group_id=uuid.uuid4(), status="resolved")

    async def test_resolve_propagates_to_events(self) -> None:
        """Resolving a group transitions its non-terminal events to resolved."""
        session = AsyncMock(spec=AsyncSession)
        group = MagicMock(spec=ErrorGroup)
        group.fingerprint = "fp-resolve"
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = group
        session.execute.return_value = result_mock
        await update_error_group(session=session, org_id=_ORG_ID, group_id=uuid.uuid4(), status="resolved")
        assert group.status == "resolved"
        assert group.resolved_at is not None

        update_call = None
        for call in session.execute.call_args_list:
            stmt = call.args[0]
            if isinstance(stmt, Update):
                update_call = stmt
                break
        assert update_call is not None, "expected a bulk UPDATE on ErrorEvent"
        compiled = str(update_call)
        assert "error_events" in compiled
        assert "resolved_at" in compiled
        assert "status" in compiled

    async def test_non_resolve_status_does_not_touch_events(self) -> None:
        """Acknowledging a group must not issue a bulk event update."""
        session = AsyncMock(spec=AsyncSession)
        group = MagicMock(spec=ErrorGroup)
        group.fingerprint = "fp-ack"
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = group
        session.execute.return_value = result_mock
        await update_error_group(session=session, org_id=_ORG_ID, group_id=uuid.uuid4(), status="acknowledged")
        for call in session.execute.call_args_list:
            assert not isinstance(call.args[0], Update)

    @pytest.mark.asyncio
    async def test_resolve_end_to_end_sqlite(self) -> None:
        """Real ORM end-to-end: resolving a group flips new/acknowledged events
        to resolved with a timestamp while leaving terminal events untouched."""
        from datetime import UTC, datetime

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(
                        sync_conn, tables=[ErrorEvent.__table__, ErrorGroup.__table__]
                    )
                )

            org_id = uuid.uuid4()
            fingerprint = "fp-sqlite"
            already_resolved = datetime.now(UTC)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                session.add(ErrorGroup(organisation_id=org_id, fingerprint=fingerprint, level_peak="error"))
                for status in ("new", "acknowledged", "resolved", "archived"):
                    resolved_at = already_resolved if status == "resolved" else None
                    session.add(
                        ErrorEvent(
                            organisation_id=org_id,
                            fingerprint=fingerprint,
                            level="error",
                            message=f"event-{status}",
                            source="backend",
                            status=status,
                            resolved_at=resolved_at,
                        )
                    )

            async with factory() as session:
                await session.begin()
                from sqlalchemy import select as _select

                result = await session.execute(_select(ErrorGroup).where(ErrorGroup.organisation_id == org_id))
                group = result.scalars().first()
                assert group is not None
                await update_error_group(session=session, org_id=org_id, group_id=group.id, status="resolved")
                await session.commit()

            async with factory() as session:
                result = await session.execute(
                    _select(ErrorEvent).where(
                        ErrorEvent.organisation_id == org_id,
                        ErrorEvent.fingerprint == fingerprint,
                    )
                )
                by_message = {e.message: e for e in result.scalars().all()}
                for status in ("new", "acknowledged"):
                    event = by_message[f"event-{status}"]
                    assert event.status == "resolved"
                    assert event.resolved_at is not None
                assert by_message["event-resolved"].status == "resolved"
                assert by_message["event-resolved"].resolved_at == already_resolved.replace(tzinfo=None)
                assert by_message["event-archived"].status == "archived"
                assert by_message["event-archived"].resolved_at is None
        finally:
            await engine.dispose()


class TestAppendOnly:
    def test_insert_not_blocked(self) -> None:
        event = ErrorEvent(
            organisation_id=_ORG_ID,
            fingerprint="abc",
            level="error",
            message="test",
            source="backend",
        )
        assert event.fingerprint == "abc"

    def test_update_listener_blocks(self) -> None:
        """The registered before_update guard actually raises AppendOnlyViolationError."""
        from modulo.core.audit_logger.append_only import AppendOnlyViolationError, _make_blocker

        blocker = _make_blocker(ErrorEvent, "error_events", "update")
        event = ErrorEvent(
            organisation_id=_ORG_ID,
            fingerprint="abc",
            level="error",
            message="test",
            source="backend",
        )
        event.id = uuid.uuid4()
        with pytest.raises(AppendOnlyViolationError) as exc_info:
            blocker(None, None, event)
        assert "append-only" in str(exc_info.value).lower()
        assert str(event.id) in str(exc_info.value)

    def test_delete_listener_blocks(self) -> None:
        """The registered before_delete guard actually raises AppendOnlyViolationError."""
        from modulo.core.audit_logger.append_only import AppendOnlyViolationError, _make_blocker

        blocker = _make_blocker(ErrorEvent, "error_events", "delete")
        event = ErrorEvent(
            organisation_id=_ORG_ID,
            fingerprint="abc",
            level="error",
            message="test",
            source="backend",
        )
        event.id = uuid.uuid4()
        with pytest.raises(AppendOnlyViolationError) as exc_info:
            blocker(None, None, event)
        assert "append-only" in str(exc_info.value).lower()
        assert str(event.id) in str(exc_info.value)


class TestOrgScoping:
    async def test_get_error_group_filters_by_org(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock
        await get_error_group_by_fingerprint(session=session, org_id=_ORG_ID, fingerprint="test")
        call_args = session.execute.call_args[0][0]
        sql = str(call_args)
        assert "organisation_id" in sql
        assert ":organisation_id_1" in sql or ":organisation_id" in sql
