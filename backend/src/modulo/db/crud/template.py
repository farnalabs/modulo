"""CRUD for pipeline templates (library_primitives with type='pipeline_template')."""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.models.library_primitive import LibraryPrimitive

TEMPLATE_TYPE = "pipeline_template"


async def list_templates(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    category: str | None = None,
    search: str | None = None,
) -> PageResult[LibraryPrimitive]:
    conditions = [LibraryPrimitive.primitive_type == TEMPLATE_TYPE]
    if category is not None:
        conditions.append(LibraryPrimitive.category == category)
    if search is not None and search.strip():
        term = f"%{search.strip()}%"
        conditions.append(LibraryPrimitive.name.ilike(term))

    count_stmt = select(func.count()).select_from(LibraryPrimitive)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = (await session.execute(count_stmt)).scalar_one()

    items_stmt = (
        select(LibraryPrimitive)
        .where(*conditions)
        .order_by(LibraryPrimitive.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = list((await session.execute(items_stmt)).scalars())

    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def get_template(session: AsyncSession, template_id: uuid.UUID) -> LibraryPrimitive | None:
    result = await session.execute(
        select(LibraryPrimitive).where(
            LibraryPrimitive.id == template_id,
            LibraryPrimitive.primitive_type == TEMPLATE_TYPE,
        )
    )
    return result.scalar_one_or_none()


def _agent_count_from_content(content: dict[str, Any]) -> int:
    agents = content.get("agents", [])
    return len(agents)


def _preview_data_from_content(content: dict[str, Any]) -> dict[str, Any]:
    nodes = content.get("graph_nodes", [])
    edges = content.get("edges", [])
    return {
        "nodes": [
            {
                "id": n.get("id", ""),
                "label": n.get("label", ""),
                "node_type": n.get("node_type", "agent"),
            }
            for n in nodes
        ],
        "edges": [
            {
                "source": e.get("source_node_id", ""),
                "target": e.get("target_node_id", ""),
                "edge_type": e.get("edge_type", "normal"),
            }
            for e in edges
        ],
    }
