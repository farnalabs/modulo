"""Postgres-specific repository — relies on RLS for tenant isolation."""

import uuid

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.repositories.base import BaseRepository
from modulo.db.rls import set_rls_org


class PostgresRepository(BaseRepository):
    """Repository for Postgres backends.

    Tenancy is handled entirely by Postgres RLS policies — ``set_org_context``
    sets the ``app.organisation_id`` config parameter and ``apply_tenant_filter``
    returns the statement unchanged because the RLS policy ``rls_org_isolation``
    already filters every query on ``organisation_id``.
    """

    async def set_org_context(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        await set_rls_org(session, org_id)

    def apply_tenant_filter(self, stmt: Select, org_id: uuid.UUID) -> Select:
        return stmt
