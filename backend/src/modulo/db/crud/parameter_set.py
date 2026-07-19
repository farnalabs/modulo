"""Org-scoped CRUD for ParameterSet.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import apply_updates
from modulo.db.models.parameter_set import ParameterSet
from modulo.db.models.pipeline_snapshot import PipelineSnapshot


async def create_set(
    session: AsyncSession,
    *,
    parameter_schema_id: uuid.UUID,
    org_id: uuid.UUID,
    name: str,
    description: str | None,
    values: dict[str, Any],
    account_id: uuid.UUID,
    schema_version: int = 1,
) -> ParameterSet:
    ps = ParameterSet(
        parameter_schema_id=parameter_schema_id,
        organisation_id=org_id,
        name=name,
        description=description,
        values=values,
        account_id=account_id,
        schema_version=schema_version,
    )
    session.add(ps)
    await session.flush()
    return ps


async def get_set(
    session: AsyncSession,
    set_id: uuid.UUID,
) -> ParameterSet | None:
    result = await session.execute(select(ParameterSet).where(ParameterSet.id == set_id))
    return result.scalar_one_or_none()


async def list_sets(
    session: AsyncSession,
    *,
    parameter_schema_id: uuid.UUID,
    org_id: uuid.UUID,
) -> list[ParameterSet]:
    result = await session.execute(
        select(ParameterSet)
        .where(
            ParameterSet.parameter_schema_id == parameter_schema_id,
            ParameterSet.organisation_id == org_id,
        )
        .order_by(ParameterSet.name)
    )
    return list(result.scalars().all())


async def update_set(
    session: AsyncSession,
    set_id: uuid.UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    values: dict[str, Any] | None = None,
    version: int,
) -> ParameterSet | None:
    result = await session.execute(
        select(ParameterSet)
        .where(
            ParameterSet.id == set_id,
            ParameterSet.version == version,
        )
        .with_for_update()
    )
    ps = result.scalar_one_or_none()
    if ps is None:
        return None
    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    if values is not None:
        updates["values"] = values
    apply_updates(ps, updates)
    ps.version += 1
    await session.flush()
    return ps


async def delete_set(
    session: AsyncSession,
    set_id: uuid.UUID,
) -> bool:
    ps = await get_set(session, set_id)
    if ps is None:
        return False
    await session.delete(ps)
    await session.flush()
    return True


async def get_set_references(
    session: AsyncSession,
    set_id: uuid.UUID,
) -> dict[str, list[uuid.UUID]]:
    set_id_str = str(set_id)
    pipeline_nodes: list[uuid.UUID] = []
    snapshots: list[uuid.UUID] = []

    rows = (
        (await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.parameter_bindings_json.isnot(None))))
        .scalars()
        .all()
    )

    for snap in rows:
        bindings = snap.parameter_bindings_json or {}
        for node_id_str, binding in bindings.items():
            if isinstance(binding, dict) and str(binding.get("parameter_set_id")) == set_id_str:
                snapshots.append(snap.id)
                pipeline_nodes.append(uuid.UUID(node_id_str))

        if snap.id in snapshots:
            continue
        nodes = snap.graph_json.get("nodes", []) if isinstance(snap.graph_json, dict) else []
        for node in nodes:
            if isinstance(node, dict) and str(node.get("parameter_set_id")) == set_id_str:
                snapshots.append(snap.id)
                pipeline_nodes.append(uuid.UUID(str(node["id"])))
                break

    return {"pipeline_nodes": pipeline_nodes, "snapshots": snapshots}
