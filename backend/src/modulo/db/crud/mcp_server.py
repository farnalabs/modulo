"""Org-scoped CRUD for the ``mcp_servers`` registry (FAR-1692 S1+S2).

Same org-scoped query-layer discipline as the other CRUD modules: every read
and write carries an explicit ``organisation_id`` predicate so a cross-org call
is refused without relying on Postgres RLS (absent on generic backends).

The tool-definition pinning columns (``tools_json``/``tools_hash`` and the
approval record) are persisted here; the *decision* logic lives in
``core.mcp.tool_pinning`` and the notification/audit side effects in the route
layer (the db layer must not import core).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.mcp_server import McpServer


async def create_mcp_server(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    transport: str,
    endpoint: str | None = None,
    command: str | None = None,
    declared_scopes: list[str] | None = None,
    owner_account_id: uuid.UUID | None = None,
    credentials_ciphertext: bytes | None = None,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
) -> McpServer:
    server = McpServer(
        organisation_id=org_id,
        name=name,
        transport=transport,
        endpoint=endpoint,
        command=command,
        declared_scopes=declared_scopes or [],
        owner_account_id=owner_account_id,
        credentials_ciphertext=credentials_ciphertext,
        visibility=visibility,
        owner_team_id=owner_team_id,
    )
    session.add(server)
    await session.flush()
    return server


async def get_mcp_server(
    session: AsyncSession,
    server_id: uuid.UUID,
    *,
    organisation_id: uuid.UUID | None = None,
) -> McpServer | None:
    stmt = select(McpServer).where(McpServer.id == server_id)
    if organisation_id is not None:
        stmt = stmt.where(McpServer.organisation_id == organisation_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_mcp_servers(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[McpServer]:
    org_filter = McpServer.organisation_id == organisation_id if organisation_id else None

    total_query = select(func.count()).select_from(McpServer)
    if org_filter is not None:
        total_query = total_query.where(org_filter)
    total = (await session.execute(total_query)).scalar_one()

    offset = (page - 1) * page_size
    items_stmt = (
        select(McpServer).order_by(McpServer.created_at.desc(), McpServer.id.desc()).offset(offset).limit(page_size)
    )
    if org_filter is not None:
        items_stmt = items_stmt.where(org_filter)
    items = list((await session.execute(items_stmt)).scalars())
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_mcp_server(
    session: AsyncSession,
    server_id: uuid.UUID,
    updates: dict[str, Any],
    *,
    organisation_id: uuid.UUID | None = None,
) -> McpServer | None:
    server = await get_mcp_server(session, server_id, organisation_id=organisation_id)
    if server is None:
        return None
    apply_updates(server, updates)
    await session.flush()
    return server


async def soft_delete_mcp_server(
    session: AsyncSession,
    server_id: uuid.UUID,
    *,
    organisation_id: uuid.UUID | None = None,
) -> bool:
    server = await get_mcp_server(session, server_id, organisation_id=organisation_id)
    if server is None:
        return False
    server.deleted_at = datetime.now(UTC)
    await session.flush()
    return True
