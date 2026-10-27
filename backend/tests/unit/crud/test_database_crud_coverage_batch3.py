"""Unit coverage for database CRUD fallback and boundary branches."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.connector_instance import list_connector_instances
from modulo.db.crud.error_tracking import (
    count_error_events_by_group,
    count_error_groups,
    get_error_events_by_group,
    get_error_group,
    get_error_group_by_fingerprint,
    get_error_groups,
    get_scheduler_starvation_pipelines,
    scheduler_starvation_age_anchor,
    update_error_group,
    upsert_error_group,
)
from modulo.db.crud.last_admin_guard import (
    LastAdminLockoutUnavailableError,
    _acquire_org_lock,
    _dialect_name,
)
from modulo.db.crud.node_observation import observe_node
from modulo.db.crud.trigger import apply_trigger_event_cursor
from modulo.db.models.error_group import ErrorGroup

_ORG_ID = uuid.uuid4()


def _programming_error() -> ProgrammingError:
    return ProgrammingError("SELECT 1", {}, RuntimeError("missing relation"))


async def test_connector_list_handles_count_and_item_programming_errors() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = _programming_error()
    page = await list_connector_instances(session)
    assert page.total == 0
    assert not page.items

    count_result = MagicMock()
    count_result.scalar_one.return_value = 1
    session.execute.side_effect = [count_result, _programming_error()]
    page = await list_connector_instances(session)
    assert page.total == 0
    assert not page.items


async def test_error_tracking_programming_error_fallbacks() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = _programming_error()
    assert not await get_error_groups(session, org_id=_ORG_ID)
    assert await count_error_groups(session, org_id=_ORG_ID) == 0


async def test_error_tracking_group_and_event_count_missing_paths() -> None:
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    assert await get_error_group(session, org_id=_ORG_ID, group_id=uuid.uuid4()) is None
    assert await get_error_group_by_fingerprint(session, org_id=_ORG_ID, fingerprint="missing") is None
    assert await count_error_events_by_group(session, org_id=_ORG_ID, group_id=uuid.uuid4()) == 0


async def test_error_tracking_filter_combinations_and_empty_count() -> None:
    session = AsyncMock(spec=AsyncSession)
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [MagicMock(spec=ErrorGroup)]
    session.execute.return_value = rows
    groups = await get_error_groups(
        session,
        org_id=_ORG_ID,
        status="new",
        level="critical",
        source="backend",
        environment="test",
        search="needle",
        limit=3,
        offset=2,
    )
    assert len(groups) == 1
    count_result = MagicMock()
    count_result.scalar_one.return_value = None
    session.execute.return_value = count_result
    assert (
        await count_error_groups(
            session,
            org_id=_ORG_ID,
            status="new",
            level="critical",
            source="backend",
            environment="test",
            search="needle",
        )
        == 0
    )


async def test_error_tracking_group_missing_short_circuits_event_reads() -> None:
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    assert not await get_error_events_by_group(session, org_id=_ORG_ID, group_id=uuid.uuid4())
    assert session.execute.await_count == 1


async def test_error_group_update_missing_raises() -> None:
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    with pytest.raises(ValueError, match="ErrorGroup not found"):
        await update_error_group(session, org_id=_ORG_ID, group_id=uuid.uuid4(), status="resolved")


async def test_error_group_upsert_updates_peak_and_sample() -> None:
    session = AsyncMock(spec=AsyncSession)
    group = MagicMock(spec=ErrorGroup)
    group.count = 1
    group.level_peak = "warning"
    result = MagicMock()
    result.scalar_one_or_none.return_value = group
    session.execute.return_value = result
    sample_id = uuid.uuid4()
    updated = await upsert_error_group(
        session,
        org_id=_ORG_ID,
        fingerprint="fp",
        level="critical",
        sample_event_id=sample_id,
    )
    assert updated is group
    assert group.count == 2
    assert group.level_peak == "critical"
    assert group.sample_event_id == sample_id


async def test_error_group_update_resolved_executes_event_update() -> None:
    session = AsyncMock(spec=AsyncSession)
    group = MagicMock(spec=ErrorGroup)
    group.fingerprint = "fp"
    result = MagicMock()
    result.scalar_one_or_none.return_value = group
    session.execute.return_value = result
    updated = await update_error_group(session, org_id=_ORG_ID, group_id=uuid.uuid4(), status="resolved")
    assert updated.resolved_at is not None
    assert any("error_events" in str(call.args[0]) for call in session.execute.call_args_list)


async def test_scheduler_starvation_query_returns_rows() -> None:
    session = AsyncMock(spec=AsyncSession)
    row = (uuid.uuid4(), "pipeline", 2, object())
    result = MagicMock()
    result.all.return_value = [row]
    session.execute.return_value = result
    rows = await get_scheduler_starvation_pipelines(session, org_id=_ORG_ID, threshold=datetime.now(UTC))
    assert rows == [row]
    assert "coalesce" in str(scheduler_starvation_age_anchor()).lower()


async def test_error_event_count_returns_scalar() -> None:
    session = AsyncMock(spec=AsyncSession)
    group = MagicMock(spec=ErrorGroup)
    group.fingerprint = "fp"
    group_result = MagicMock()
    group_result.scalar_one_or_none.return_value = group
    count_result = MagicMock()
    count_result.scalar_one.return_value = 4
    session.execute.side_effect = [group_result, count_result]
    assert await count_error_events_by_group(session, org_id=_ORG_ID, group_id=uuid.uuid4()) == 4


async def test_last_admin_dialect_name_supports_async_bind() -> None:
    session = AsyncMock(spec=AsyncSession)
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    session.get_bind.return_value = bind
    assert await _dialect_name(session) == "postgresql"


async def test_last_admin_lock_retries_until_success() -> None:
    session = AsyncMock(spec=AsyncSession)
    failed = MagicMock()
    failed.scalar_one.return_value = False
    succeeded = MagicMock()
    succeeded.scalar_one.return_value = True
    session.execute.side_effect = [failed, succeeded]
    with (
        patch("modulo.db.crud.last_admin_guard._POLL_INTERVAL", 0),
        patch("modulo.db.crud.last_admin_guard._LOCK_TIMEOUT_SECONDS", 1),
    ):
        await _acquire_org_lock(session, _ORG_ID)
    assert session.execute.await_count == 2


async def test_last_admin_lock_timeout_raises() -> None:
    session = AsyncMock(spec=AsyncSession)
    failed = MagicMock()
    failed.scalar_one.return_value = False
    session.execute.return_value = failed
    with (
        patch("modulo.db.crud.last_admin_guard._POLL_INTERVAL", 0),
        patch("modulo.db.crud.last_admin_guard._LOCK_TIMEOUT_SECONDS", 0),
        pytest.raises(LastAdminLockoutUnavailableError),
    ):
        await _acquire_org_lock(session, _ORG_ID)


async def test_node_observation_constructor_path_is_exercised() -> None:
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    with pytest.raises(TypeError, match="human_observed_by"):
        await observe_node(
            session,
            organisation_id=_ORG_ID,
            run_id=uuid.uuid4(),
            node_id=str(uuid.uuid4()),
        )


def test_trigger_cursor_boundary_and_malformed_cursor() -> None:
    from modulo.db.models.trigger_event import TriggerEvent

    select_for_test = select(TriggerEvent)
    query = apply_trigger_event_cursor(select_for_test, None)
    assert str(query) == str(select_for_test)
    malformed = apply_trigger_event_cursor(select_for_test, "not-a-valid-cursor")
    assert str(malformed) == str(select_for_test)
    valid = f"2026-01-01T00:00:00+00:00_{uuid.uuid4()}"
    filtered = apply_trigger_event_cursor(select_for_test, valid)
    assert str(filtered) != str(select_for_test)
