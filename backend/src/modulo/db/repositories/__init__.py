"""Repository hub — dispatches to backend-specific repo implementation.

Usage:
    repo = RepositoryHub()
    await repo.set_org_context(session, org_id)
    stmt = repo.apply_tenant_filter(select(Pipeline), org_id)
    result = await repo.paginate(session, stmt, page=1, page_size=20)
"""

import uuid
from collections.abc import AsyncGenerator, Callable
from typing import Any

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.repositories.base import BaseRepository
from modulo.db.repositories.generic import GenericRepository
from modulo.db.repositories.locks import BaseLockService, _build_lock_service
from modulo.db.repositories.postgres import PostgresRepository


def _build_repository(
    db_type: str,
    session_factory: Callable[[], AsyncGenerator[AsyncSession, None]],
) -> BaseRepository:
    match db_type:
        case "postgres":
            return PostgresRepository(session_factory)
        case _:
            return GenericRepository(session_factory)


class RepositoryHub:
    """Dispatches to the correct backend repository based on MODULO_DB.

    Follows the same dispatcher pattern as ConnectorHub — the factory
    selects a concrete implementation at construction time.
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncGenerator[AsyncSession, None]],
        db_type: str | None = None,
    ) -> None:
        if db_type is None:
            from modulo.settings import get_settings
            db_type = get_settings().modulo_db.lower()
        self._repo: BaseRepository = _build_repository(db_type, session_factory)
        self._lock_service: BaseLockService = _build_lock_service(db_type)

    @property
    def repo(self) -> BaseRepository:
        return self._repo

    @property
    def locks(self) -> BaseLockService:
        return self._lock_service

    async def set_org_context(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        await self._repo.set_org_context(session, org_id)

    def apply_tenant_filter(self, stmt: Select, org_id: uuid.UUID) -> Select:
        return self._repo.apply_tenant_filter(stmt, org_id)

    async def paginate(
        self,
        session: AsyncSession,
        stmt: Select,
        page: int,
        page_size: int,
    ) -> PageResult[Any]:
        return await self._repo.paginate(session, stmt, page, page_size)

    async def execute(self, session: AsyncSession, stmt: Any) -> Any:
        return await self._repo.execute(session, stmt)
