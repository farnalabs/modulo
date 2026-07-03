"""Abstract base repository with tenant-aware operations.

Two implementations dispatch via RepositoryHub:
  - PostgresRepository  — RLS handles tenant filtering via set_config()
  - GenericRepository   — explicit .where() for MariaDB / SQLite
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Callable
from typing import Any

from sqlalchemy import Select, func
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.rls import set_rls_org


def extract_orm_entity(stmt: Select[Any]) -> type | None:
    """Walk the statement's column descriptions to find the ORM entity.

    Shared utility used by GenericRepository.apply_tenant_filter and
    rls._inject_tenant_filter to avoid duplicating entity-detection logic.
    """
    for desc in stmt.column_descriptions:
        entity = desc.get("entity")
        if entity is not None and isinstance(entity, type):
            return entity
    return None


class BaseRepository(ABC):
    """Thin abstraction over session operations with tenant awareness.

    Subclasses override *apply_tenant_filter* and *set_org_context* to
    match the backend's multi-tenancy strategy (RLS vs. explicit filter).
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncGenerator[AsyncSession, None]],
    ) -> None:
        self._session_factory = session_factory

    async def set_org_context(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        """Establish tenant identity for the current transaction.

        Called once per request inside the active transaction.  Delegates
        to ``set_rls_org`` to configure the session-level ``organisation_id``
        parameter, which Postgres RLS policies enforce.
        """
        await set_rls_org(session, org_id)

    @abstractmethod
    def apply_tenant_filter(self, stmt: Select[Any], org_id: uuid.UUID) -> Select[Any]:
        """Augment *stmt* with an ``organisation_id = :org_id`` clause.

        Postgres returns the statement unchanged (RLS policies handle
        filtering); generic backends inject an explicit WHERE clause.
        """

    async def paginate(
        self,
        session: AsyncSession,
        stmt: Select[Any],
        page: int,
        page_size: int,
    ) -> PageResult[Any]:
        """Apply LIMIT/OFFSET pagination and return a PageResult.

        Both Postgres and generic backends share the same standard-SQL
        pagination strategy.  Override in a subclass for cursor-based or
        keyset pagination.
        """
        if page < 1:
            raise ValueError("page must be >= 1")
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        offset = (page - 1) * page_size

        count_stmt = sa_select(func.count()).select_from(stmt.order_by(None).subquery())
        total: int = (await session.execute(count_stmt)).scalar_one()

        result = await session.execute(stmt.offset(offset).limit(page_size))
        items = result.scalars().all()

        return PageResult(items=items, total=total, page=page, page_size=page_size)

    async def execute(self, session: AsyncSession, stmt: Any) -> Any:
        """Thin wrapper around ``session.execute()``."""
        return await session.execute(stmt)
