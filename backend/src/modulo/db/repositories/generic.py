"""Generic repository for MariaDB / SQLite — explicit tenant filtering."""

import uuid
from typing import Any

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.repositories.base import BaseRepository

_TENANT_COLUMN = "organisation_id"


class GenericRepository(BaseRepository):
    """Repository for backends without RLS support (MariaDB, SQLite).

    Since these databases lack row-level security, tenant filtering is
    applied explicitly by injecting a ``WHERE organisation_id = :org_id``
    clause into every query via ``apply_tenant_filter``.

    For JOIN queries, the WHERE clause is added for every entity that has
    an ``organisation_id`` column, matching the behaviour of the ORM
    ``do_orm_execute`` listener in ``rls._inject_tenant_filter``.
    """

    def apply_tenant_filter(self, stmt: Select[Any], org_id: uuid.UUID) -> Select[Any]:
        if org_id is None:
            raise ValueError("org_id must not be None")
        for desc in stmt.column_descriptions:
            entity = desc.get("entity")
            if entity is None or entity is object:
                continue
            if hasattr(entity, _TENANT_COLUMN):
                stmt = stmt.where(getattr(entity, _TENANT_COLUMN) == org_id)
        return stmt
