"""Org-scoping + persistence for the FAR-1692 registries (no Docker).

Drives the REAL CRUD helpers against in-memory SQLite (no RLS there), so a
cross-org call can only be refused by the query-layer ``organisation_id``
predicate itself — the SQLite counterpart to the Postgres ``rls_team_isolation``
policy. Also pins the team-visibility CHECK constraint and the tool-pinning
columns' persistence.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.external_agent import (
    create_external_agent,
    get_external_agent,
    list_external_agents,
    soft_delete_external_agent,
    update_external_agent,
)
from modulo.db.crud.mcp_server import (
    create_mcp_server,
    get_mcp_server,
    list_mcp_servers,
    soft_delete_mcp_server,
    update_mcp_server,
)
from modulo.db.models import Base
from modulo.db.models.external_agent import ExternalAgent
from modulo.db.models.mcp_server import APPROVAL_STATUS_APPROVED, McpServer
from modulo.db.models.organisation import Organisation
from modulo.db.soft_delete import register_soft_delete_filter

# Soft-delete semantics depend on the global ORM filter, which is registered as
# an import side effect of ``modulo.db.session``. A test module asserting a
# soft-deleted row is hidden MUST ensure the filter is registered in its own
# process (see the 2026-10-14 lesson in Repos/modulo/AGENTS.md).
register_soft_delete_filter()

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_AGENT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_SERVER_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

_TABLES = [Organisation.__table__, ExternalAgent.__table__, McpServer.__table__]


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        for org in (_ORG_A, _ORG_B):
            s.add(Organisation(id=org, name="org " + str(org), slug="org-" + str(org)))
        await s.flush()
        s.add(ExternalAgent(id=_AGENT_B, organisation_id=_ORG_B, name="agent-b", runtime_kind="cursor"))
        s.add(McpServer(id=_SERVER_B, organisation_id=_ORG_B, name="server-b", transport="http", declared_scopes=[]))
        await s.flush()
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# external_agents
# ---------------------------------------------------------------------------


async def test_get_external_agent_refuses_other_org(session: AsyncSession) -> None:
    assert await get_external_agent(session, _AGENT_B, organisation_id=_ORG_A) is None


async def test_get_external_agent_returns_own_org(session: AsyncSession) -> None:
    found = await get_external_agent(session, _AGENT_B, organisation_id=_ORG_B)

    assert found is not None
    assert found.name == "agent-b"


async def test_list_external_agents_excludes_other_org(session: AsyncSession) -> None:
    other = await list_external_agents(session, organisation_id=_ORG_A)
    own = await list_external_agents(session, organisation_id=_ORG_B)

    assert other.total == 0
    assert own.total == 1


async def test_update_external_agent_refuses_other_org(session: AsyncSession) -> None:
    result = await update_external_agent(session, _AGENT_B, {"name": "hijacked"}, organisation_id=_ORG_A)

    assert result is None
    stored = await get_external_agent(session, _AGENT_B, organisation_id=_ORG_B)
    assert stored is not None
    assert stored.name == "agent-b"


async def test_soft_delete_external_agent_refuses_other_org(session: AsyncSession) -> None:
    assert await soft_delete_external_agent(session, _AGENT_B, organisation_id=_ORG_A) is False

    # The row was not retired: it is still visible to its own org.
    surviving = await get_external_agent(session, _AGENT_B, organisation_id=_ORG_B)
    assert surviving is not None


async def test_soft_delete_external_agent_hides_row(session: AsyncSession) -> None:
    assert await soft_delete_external_agent(session, _AGENT_B, organisation_id=_ORG_B) is True

    hidden = await get_external_agent(session, _AGENT_B, organisation_id=_ORG_B)
    assert hidden is None
    listed = await list_external_agents(session, organisation_id=_ORG_B)
    assert listed.total == 0


async def test_team_visibility_requires_owner_team(session: AsyncSession) -> None:
    # Team-private without an owning team violates ck_external_agents_team_owner.
    with pytest.raises(IntegrityError):
        await create_external_agent(
            session, org_id=_ORG_A, name="bad", runtime_kind="other", visibility="team", owner_team_id=None
        )


async def test_create_external_agent_persists_provenance(session: AsyncSession) -> None:
    created = await create_external_agent(
        session,
        org_id=_ORG_A,
        name="claude",
        runtime_kind="claude-code",
        host_descriptor="workstation-1",
        discovery_provenance="manual",
    )

    assert created.discovery_provenance == "manual"
    assert created.visibility == "org"
    assert created.organisation_id == _ORG_A


# ---------------------------------------------------------------------------
# mcp_servers
# ---------------------------------------------------------------------------


async def test_get_mcp_server_refuses_other_org(session: AsyncSession) -> None:
    assert await get_mcp_server(session, _SERVER_B, organisation_id=_ORG_A) is None


async def test_list_mcp_servers_excludes_other_org(session: AsyncSession) -> None:
    other = await list_mcp_servers(session, organisation_id=_ORG_A)
    own = await list_mcp_servers(session, organisation_id=_ORG_B)

    assert other.total == 0
    assert own.total == 1


async def test_update_mcp_server_refuses_other_org(session: AsyncSession) -> None:
    result = await update_mcp_server(session, _SERVER_B, {"name": "hijacked"}, organisation_id=_ORG_A)

    assert result is None
    stored = await get_mcp_server(session, _SERVER_B, organisation_id=_ORG_B)
    assert stored is not None
    assert stored.name == "server-b"


async def test_soft_delete_mcp_server_refuses_other_org(session: AsyncSession) -> None:
    assert await soft_delete_mcp_server(session, _SERVER_B, organisation_id=_ORG_A) is False

    still_there = await get_mcp_server(session, _SERVER_B, organisation_id=_ORG_B)
    assert still_there is not None


async def test_mcp_server_pinning_fields_persist(session: AsyncSession) -> None:
    created = await create_mcp_server(
        session,
        org_id=_ORG_A,
        name="pinned",
        transport="sse",
        endpoint="https://mcp.example/sse",
        declared_scopes=["tools.read"],
        credentials_ciphertext=b"ciphertext",
    )
    assert created.approval_status == "unapproved"
    assert created.tools_hash is None

    updated = await update_mcp_server(
        session,
        created.id,
        {
            "tools_json": [{"name": "read"}],
            "tools_hash": "a" * 64,
            "approval_status": APPROVAL_STATUS_APPROVED,
            "approved_hash": "a" * 64,
        },
        organisation_id=_ORG_A,
    )

    assert updated is not None
    assert updated.approval_status == APPROVAL_STATUS_APPROVED
    assert updated.approved_hash == "a" * 64
    assert updated.tools_json == [{"name": "read"}]


async def test_mcp_server_transport_constraint(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await create_mcp_server(session, org_id=_ORG_A, name="bad", transport="carrier-pigeon")
