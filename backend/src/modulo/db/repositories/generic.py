"""Generic repository for MariaDB / SQLite — explicit tenant filtering."""

import uuid
from collections.abc import AsyncGenerator, Callable

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.repositories.base import BaseRepository, extract_orm_entity
from modulo.db.rls import set_rls_org


class GenericRepository(BaseRepository):
    """Repository for backends without RLS support (MariaDB, SQLite).

    Since these databases lack row-level security, tenant filtering is
    applied explicitly by injecting a ``WHERE organisation_id = :org_id``
    clause into every query via ``apply_tenant_filter``.
    """

    async def set_org_context(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        await set_rls_org(session, org_id)

    def apply_tenant_filter(self, stmt: Select, org_id: uuid.UUID) -> Select:
        entity = extract_orm_entity(stmt)
        if entity is not None and hasattr(entity, "organisation_id"):
            return stmt.where(entity.organisation_id == org_id)
        return stmt
