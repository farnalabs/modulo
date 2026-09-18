"""Postgres-specific repository — relies on RLS for tenant isolation."""

import uuid
from typing import Any

from sqlalchemy import Select

from modulo.db.repositories.base import BaseRepository


class PostgresRepository(BaseRepository):
    """Repository for Postgres backends.

    Tenancy is handled entirely by Postgres RLS policies — ``set_org_context``
    sets the ``app.organisation_id`` config parameter and ``apply_tenant_filter``
    returns the statement unchanged because the RLS policy ``rls_org_isolation``
    already filters every query on ``organisation_id``.
    """

    def apply_tenant_filter(self, stmt: Select[Any], _org_id: uuid.UUID) -> Select[Any]:
        return stmt
