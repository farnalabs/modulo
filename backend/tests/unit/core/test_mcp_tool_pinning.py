"""MCP tool-definition pinning — canonical hash + drift decision (FAR-1692 S2).

Covers, without Docker (pure functions + in-memory SQLite):

* the canonical hash is order-independent and ignores volatile server metadata,
  but changes when a tool's name or input schema changes;
* the drift decision matrix (unapproved / approved / drift -> approved-stale /
  blocked-never-downgraded / drift-cleared);
* the end-to-end ``apply_tool_observation`` path: observing a changed tool set on
  an approved server flips the pin to ``approved-stale`` and raises exactly one
  notification + one audit event, re-observation while already stale does NOT
  re-alert, and a return to the approved hash clears the stale flag.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.mcp.tool_pinning import (
    ToolPinningError,
    apply_tool_observation,
    compute_tools_hash,
    decide_observation,
)
from modulo.db.models import Base
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.mcp_server import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_APPROVED_STALE,
    APPROVAL_STATUS_BLOCKED,
    APPROVAL_STATUS_UNAPPROVED,
    McpServer,
)
from modulo.db.models.notification import Notification
from modulo.db.models.organisation import Organisation

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

_TABLES = [
    Organisation.__table__,
    McpServer.__table__,
    Notification.__table__,
    AuditEvent.__table__,
    AuditChainHead.__table__,
]


def _tools(*names: str) -> list[dict[str, Any]]:
    return [{"name": name, "description": name + " desc", "inputSchema": {"type": "object"}} for name in names]


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        s.add(Organisation(id=_ORG, name="org", slug="org"))
        await s.flush()
        yield s
    await engine.dispose()


async def _seed_server(
    session: AsyncSession,
    *,
    approved_hash: str | None,
    status: str,
    tools: list[dict[str, Any]] | None = None,
) -> McpServer:
    server = McpServer(
        organisation_id=_ORG,
        name="server",
        transport="http",
        endpoint="https://mcp.example/sse",
        declared_scopes=[],
        tools_json=tools or [],
        approval_status=status,
        approved_hash=approved_hash,
    )
    session.add(server)
    await session.flush()
    return server


# ---------------------------------------------------------------------------
# compute_tools_hash — canonicalisation
# ---------------------------------------------------------------------------


def test_hash_is_order_independent() -> None:
    assert compute_tools_hash(_tools("read", "write")) == compute_tools_hash(_tools("write", "read"))


def test_hash_ignores_volatile_metadata() -> None:
    base = _tools("read")
    noisy = [
        {
            "name": "read",
            "description": "read desc",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
            "_meta": {"fastmcp": {"version": "1"}},
        }
    ]
    assert compute_tools_hash(base) == compute_tools_hash(noisy)


def test_hash_changes_on_input_schema_change() -> None:
    before = [{"name": "read", "inputSchema": {"type": "object", "properties": {}}}]
    after = [{"name": "read", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}}]

    assert compute_tools_hash(before) != compute_tools_hash(after)


def test_hash_accepts_snake_case_input_schema_alias() -> None:
    camel = [{"name": "read", "inputSchema": {"type": "object"}}]
    snake = [{"name": "read", "input_schema": {"type": "object"}}]

    assert compute_tools_hash(camel) == compute_tools_hash(snake)


def test_hash_rejects_tool_without_name() -> None:
    with pytest.raises(ToolPinningError):
        compute_tools_hash([{"description": "nameless"}])


def test_hash_rejects_non_mapping_tool() -> None:
    with pytest.raises(ToolPinningError):
        compute_tools_hash([["not", "a", "mapping"]])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# decide_observation — drift matrix
# ---------------------------------------------------------------------------


def test_unapproved_observation_stays_unapproved() -> None:
    outcome = decide_observation(observed_hash="a" * 64, approved_hash=None, current_status=APPROVAL_STATUS_UNAPPROVED)

    assert outcome.approval_status == APPROVAL_STATUS_UNAPPROVED
    assert outcome.drifted is False


def test_approval_without_hash_is_reset_to_unapproved() -> None:
    # Defensive: an `approved` status with no recorded hash cannot be trusted to
    # be current, so an observation downgrades it rather than silently keeping it.
    outcome = decide_observation(observed_hash="a" * 64, approved_hash=None, current_status=APPROVAL_STATUS_APPROVED)

    assert outcome.approval_status == APPROVAL_STATUS_UNAPPROVED


def test_matching_approved_hash_is_approved() -> None:
    approved = "a" * 64
    outcome = decide_observation(
        observed_hash=approved, approved_hash=approved, current_status=APPROVAL_STATUS_APPROVED
    )

    assert outcome.approval_status == APPROVAL_STATUS_APPROVED
    assert outcome.drifted is False


def test_changed_hash_is_drift() -> None:
    outcome = decide_observation(
        observed_hash="b" * 64, approved_hash="a" * 64, current_status=APPROVAL_STATUS_APPROVED
    )

    assert outcome.approval_status == APPROVAL_STATUS_APPROVED_STALE
    assert outcome.drifted is True


def test_return_to_approved_hash_clears_stale() -> None:
    approved = "a" * 64
    outcome = decide_observation(
        observed_hash=approved, approved_hash=approved, current_status=APPROVAL_STATUS_APPROVED_STALE
    )

    assert outcome.approval_status == APPROVAL_STATUS_APPROVED
    assert outcome.drifted is False


def test_blocked_server_is_never_downgraded() -> None:
    outcome = decide_observation(observed_hash="b" * 64, approved_hash="a" * 64, current_status=APPROVAL_STATUS_BLOCKED)

    assert outcome.approval_status == APPROVAL_STATUS_BLOCKED
    assert outcome.drifted is False


# ---------------------------------------------------------------------------
# apply_tool_observation — persistence + alerting
# ---------------------------------------------------------------------------


async def _count(session: AsyncSession, model: Any) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _drift_events(session: AsyncSession) -> list[AuditEvent]:
    result = await session.execute(select(AuditEvent).where(AuditEvent.event_type == "mcp_server_tools_drifted"))
    return list(result.scalars())


async def test_observation_of_changed_tools_marks_stale_and_alerts(session: AsyncSession) -> None:
    tools_a = _tools("read", "write")
    hash_a = compute_tools_hash(tools_a)
    server = await _seed_server(session, approved_hash=hash_a, status=APPROVAL_STATUS_APPROVED, tools=tools_a)

    result = await apply_tool_observation(
        session, server=server, org_id=_ORG, tools=_tools("read", "write", "delete"), actor_user_id=None
    )

    assert result.approval_status == APPROVAL_STATUS_APPROVED_STALE
    assert result.drifted is True
    assert result.newly_drifted is True
    assert await _count(session, Notification) == 1
    assert len(await _drift_events(session)) == 1
    assert server.tools_hash == result.tools_hash


async def test_second_observation_while_stale_does_not_re_alert(session: AsyncSession) -> None:
    tools_a = _tools("read")
    hash_a = compute_tools_hash(tools_a)
    server = await _seed_server(session, approved_hash=hash_a, status=APPROVAL_STATUS_APPROVED, tools=tools_a)

    await apply_tool_observation(
        session, server=server, org_id=_ORG, tools=_tools("read", "delete"), actor_user_id=None
    )
    second = await apply_tool_observation(
        session, server=server, org_id=_ORG, tools=_tools("read", "delete"), actor_user_id=None
    )

    assert second.drifted is True
    assert second.newly_drifted is False
    assert await _count(session, Notification) == 1
    assert len(await _drift_events(session)) == 1


async def test_return_to_approved_tools_clears_stale(session: AsyncSession) -> None:
    tools_a = _tools("read")
    hash_a = compute_tools_hash(tools_a)
    server = await _seed_server(session, approved_hash=hash_a, status=APPROVAL_STATUS_APPROVED, tools=tools_a)

    await apply_tool_observation(
        session, server=server, org_id=_ORG, tools=_tools("read", "delete"), actor_user_id=None
    )
    reverted = await apply_tool_observation(session, server=server, org_id=_ORG, tools=tools_a, actor_user_id=None)

    assert reverted.approval_status == APPROVAL_STATUS_APPROVED
    assert reverted.drifted is False


async def test_blocked_server_observation_does_not_alert(session: AsyncSession) -> None:
    tools_a = _tools("read")
    hash_a = compute_tools_hash(tools_a)
    server = await _seed_server(session, approved_hash=hash_a, status=APPROVAL_STATUS_BLOCKED, tools=tools_a)

    result = await apply_tool_observation(
        session, server=server, org_id=_ORG, tools=_tools("read", "delete"), actor_user_id=None
    )

    assert result.approval_status == APPROVAL_STATUS_BLOCKED
    assert result.drifted is False
    assert await _count(session, Notification) == 0
    assert len(await _drift_events(session)) == 0


async def test_unapproved_server_observation_does_not_alert(session: AsyncSession) -> None:
    server = await _seed_server(session, approved_hash=None, status=APPROVAL_STATUS_UNAPPROVED)

    result = await apply_tool_observation(session, server=server, org_id=_ORG, tools=_tools("read"), actor_user_id=None)

    assert result.approval_status == APPROVAL_STATUS_UNAPPROVED
    assert result.drifted is False
    assert await _count(session, Notification) == 0


async def test_drift_audit_failure_is_swallowed(session: AsyncSession) -> None:
    # The audit append is best-effort: a DB failure there must not roll back the
    # observation itself nor stop the notification attempt (fail-open savepoint).
    tools_a = _tools("read")
    hash_a = compute_tools_hash(tools_a)
    server = await _seed_server(session, approved_hash=hash_a, status=APPROVAL_STATUS_APPROVED, tools=tools_a)

    with patch(
        "modulo.core.mcp.tool_pinning.append_audit_event",
        AsyncMock(side_effect=SQLAlchemyError("audit down")),
    ):
        result = await apply_tool_observation(
            session, server=server, org_id=_ORG, tools=_tools("read", "delete"), actor_user_id=None
        )

    assert result.approval_status == APPROVAL_STATUS_APPROVED_STALE
    assert result.newly_drifted is True
    # The notification leg still ran even though the audit leg raised.
    assert await _count(session, Notification) == 1


async def test_drift_notification_failure_is_swallowed(session: AsyncSession) -> None:
    tools_a = _tools("read")
    hash_a = compute_tools_hash(tools_a)
    server = await _seed_server(session, approved_hash=hash_a, status=APPROVAL_STATUS_APPROVED, tools=tools_a)

    with patch(
        "modulo.core.mcp.tool_pinning.create_notification",
        AsyncMock(side_effect=SQLAlchemyError("notify down")),
    ):
        result = await apply_tool_observation(
            session, server=server, org_id=_ORG, tools=_tools("read", "delete"), actor_user_id=None
        )

    assert result.approval_status == APPROVAL_STATUS_APPROVED_STALE
    assert result.newly_drifted is True
