"""Generic repository for MariaDB / SQLite — explicit tenant filtering."""

import uuid
from collections.abc import AsyncGenerator, Callable

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.repositories.base import BaseRepository


def _extract_entity(stmt: Select) -> type | None:
    """Walk the statement's column descriptions to find the ORM entity."""
    for desc in stmt.column_descriptions:
        entity = desc.get("entity")
        if entity is not None:
            return entity
    return None


class GenericRepository(BaseRepository):
    """Repository for backends without RLS support (MariaDB, SQLite).

    Since these databases lack row-level security, tenant filtering is
    applied explicitly by injecting a ``WHERE organisation_id = :org_id``
    clause into every query via ``apply_tenant_filter``.
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncGenerator[AsyncSession, None]],
    ) -> None:
        super().__init__(session_factory)

    async def set_org_context(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        pass

    def apply_tenant_filter(self, stmt: Select, org_id: uuid.UUID) -> Select:
        entity = _extract_entity(stmt)
        if entity is not None and hasattr(entity, "organisation_id"):
            return stmt.where(entity.organisation_id == org_id)
        return stmt
