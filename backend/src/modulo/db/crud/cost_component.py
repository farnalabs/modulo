"""Org-scoped CRUD for CostComponent (PURE DB layer — no modulo.core imports).

All functions assume the caller has set the RLS org context via ``set_rls_org()``
before calling. The session must be within an active transaction. The 409
duplicate pre-check runs with an explicit ``organisation_id`` filter (raw
``text()`` would bypass RLS on Postgres).

Cross-field/formula/reserved-key validation and audit-event emission live in
the API route layer (``modulo.api`` may import ``modulo.core``; ``modulo.db``
may not). This module performs ONLY the DB-level checks that need the session:
the org cap, the duplicate pre-check, and the last-enabled-calculated guards.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.cost_component import CostComponent, CostComponentKind

__all__ = [
    "count_active_components",
    "create_cost_component",
    "get_cost_component",
    "list_cost_components",
    "soft_delete_cost_component",
    "update_cost_component",
]


async def _duplicate_exists(
    session: AsyncSession, org_id: uuid.UUID, *, name: str, report_key: str | None, _kind: str
) -> bool:
    """Org-scoped duplicate pre-check (409). Explicit parens pin the precedence."""
    row = await session.execute(
        select(CostComponent.id)
        .where(
            or_(
                (CostComponent.report_key == report_key) & (CostComponent.kind == "self_reported"),
                CostComponent.name == name,
            )
            if report_key is not None
            else (CostComponent.name == name),
            CostComponent.organisation_id == org_id,
            CostComponent.deleted_at.is_(None),
        )
        .limit(1)
    )
    return row.scalar_one_or_none() is not None


async def count_active_components(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Count active (non-deleted) components for an org (the org-cap check)."""
    result = await session.execute(
        select(func.count())
        .select_from(CostComponent)
        .where(
            CostComponent.organisation_id == org_id,
            CostComponent.deleted_at.is_(None),
        )
    )
    return int(result.scalar_one())


async def create_cost_component(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    display_name: str,
    kind: str,
    rate_usd: Any,
    rate_fallback: str | None,
    formula: str | None,
    report_key: str | None,
    enabled: bool,
    sort_order: int,
    max_components: int = 50,
) -> CostComponent:
    if await count_active_components(session, org_id) >= max_components:
        raise ValueError("org_cap")
    if await _duplicate_exists(session, org_id, name=name, report_key=report_key, _kind=kind):
        raise ValueError("duplicate_component")

    component = CostComponent(
        organisation_id=org_id,
        name=name,
        display_name=display_name,
        kind=kind,
        rate_usd=rate_usd,
        rate_fallback=rate_fallback,
        formula=formula,
        report_key=report_key,
        enabled=enabled,
        sort_order=sort_order,
    )
    session.add(component)
    await session.flush()
    return component


async def list_cost_components(session: AsyncSession) -> list[CostComponent]:
    result = await session.execute(
        select(CostComponent)
        .where(CostComponent.deleted_at.is_(None))
        .order_by(CostComponent.sort_order, CostComponent.name)
    )
    return list(result.scalars().all())


async def get_cost_component(session: AsyncSession, component_id: uuid.UUID) -> CostComponent | None:
    result = await session.execute(
        select(CostComponent).where(CostComponent.id == component_id, CostComponent.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def _is_last_enabled_calculated(session: AsyncSession, component_id: uuid.UUID) -> bool:
    count = await session.execute(
        select(func.count())
        .select_from(CostComponent)
        .where(
            CostComponent.kind == CostComponentKind.CALCULATED.value,
            CostComponent.enabled.is_(True),
            CostComponent.deleted_at.is_(None),
            CostComponent.id != component_id,
        )
    )
    return int(count.scalar_one()) == 0


async def update_cost_component(
    session: AsyncSession,
    *,
    component_id: uuid.UUID,
    updates: dict[str, Any],
) -> CostComponent | None:
    component = await get_cost_component(session, component_id)
    if component is None:
        return None

    new_kind = updates.get("kind", component.kind)
    new_name = updates.get("name", component.name)
    new_report_key = updates.get("report_key", component.report_key)

    # kind-change guard: the last enabled calculated component cannot change kind.
    if (
        new_kind != component.kind
        and component.kind == CostComponentKind.CALCULATED.value
        and component.enabled
        and await _is_last_enabled_calculated(session, component_id)
    ):
        raise ValueError("last_calculated_kind_change")

    # disable-of-last-calculated guard.
    if (
        updates.get("enabled") is False
        and component.kind == CostComponentKind.CALCULATED.value
        and component.enabled
        and await _is_last_enabled_calculated(session, component_id)
    ):
        raise ValueError("last_calculated_disable")

    # duplicate check (excludes self: the name/report_key are compared against
    # OTHER active rows).
    if (new_name != component.name or new_report_key != component.report_key) and await _duplicate_exists(
        session, component.organisation_id, name=new_name, report_key=new_report_key, _kind=new_kind
    ):
        raise ValueError("duplicate_component")

    for key, value in updates.items():
        if key in ("id", "organisation_id", "created_at", "updated_at", "deleted_at"):
            continue
        if hasattr(component, key):
            setattr(component, key, value)
    await session.flush()
    return component


async def soft_delete_cost_component(
    session: AsyncSession,
    *,
    component_id: uuid.UUID,
) -> CostComponent | None:
    component = await get_cost_component(session, component_id)
    if component is None:
        return None
    if (
        component.kind == CostComponentKind.CALCULATED.value
        and component.enabled
        and await _is_last_enabled_calculated(session, component_id)
    ):
        raise ValueError("last_calculated_delete")
    component.deleted_at = func.now()
    await session.flush()
    return component
