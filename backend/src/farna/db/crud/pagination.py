"""Cursor-based pagination utility using keyset (seek) pagination.

Avoids the offset drift problem of LIMIT/OFFSET by using composite
``WHERE (sort_field, id) < (cursor_value, cursor_id)`` conditions.

Usage::

    paginator = CursorPaginator(sort_field="created_at", sort_dir="desc")
    page = await paginator.paginate(session, stmt, cursor=cursor, limit=20, model=Pipeline)
"""

import base64
import logging
import uuid
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import Select, func, literal, select
from sqlalchemy import tuple_ as sa_tuple
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

T = TypeVar("T")
_ModelT = TypeVar("_ModelT")  # bound=DeclarativeBase — omitted for pydantic compatibility


class CursorPage(BaseModel, Generic[T]):  # noqa: UP046 — needs Python 3.12+ `[T]` type param syntax
    model_config = {"arbitrary_types_allowed": True}
    items: list[T]
    next_cursor: str | None = None
    total: int | None = None
    has_more: bool = False


class CursorPaginator:
    """Keyset-based cursor paginator.

    Encodes the cursor as url-safe-base64 of ``{sort_value}:{record_id}``.
    Decodes it to reconstruct the ``WHERE`` clause for the next page.
    """

    def __init__(
        self,
        sort_field: str = "created_at",
        sort_dir: str = "desc",
    ) -> None:
        self.sort_field = sort_field
        self.sort_dir = sort_dir

    @staticmethod
    def encode_cursor(sort_field_value: Any, record_id: uuid.UUID) -> str:
        if isinstance(sort_field_value, datetime):
            sort_field_value = sort_field_value.isoformat()
        raw = f"{sort_field_value}:{record_id}"
        return base64.urlsafe_b64encode(raw.encode()).decode()

    @staticmethod
    def decode_cursor(cursor: str) -> tuple[Any, uuid.UUID]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode()).decode()
            sort_val_str, id_str = decoded.rsplit(":", 1)
            return sort_val_str, uuid.UUID(id_str)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid cursor value") from exc

    def _parse_cursor_value(self, raw: str) -> Any:
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            logger.warning("Failed to parse cursor value as datetime, falling back to raw string: %s", raw)
            return raw

    @staticmethod
    def _validate_cursor_value_type(value: Any, sort_col: Any) -> None:
        """Reject cursor values that cannot be compared against the sort column.

        A well-formed cursor whose value is not of the sort column's Python
        type (e.g. a non-date string for a ``created_at`` timestamptz cursor)
        would fail the keyset tuple comparison at the database and surface as
        a 503 instead of the 422 a client error deserves.
        """
        try:
            expected = sort_col.type.python_type
        except NotImplementedError:
            return
        if expected is datetime and not isinstance(value, datetime):
            raise ValueError("Invalid cursor value") from None

    async def paginate(
        self,
        session: AsyncSession,
        stmt: Select[Any],
        *,
        cursor: str | None = None,
        limit: int = 20,
        sort_field: str | None = None,
        sort_dir: str | None = None,
        model: type[_ModelT],
        compute_total: bool = False,
    ) -> CursorPage[_ModelT]:
        """Apply keyset pagination to *stmt* and return a CursorPage.

        When *cursor* is ``None``, returns the first page sorted by
        *sort_field* in *sort_dir* order.

        Parameters
        ----------
        session:
            Active async DB session.
        stmt:
            SQLAlchemy ``Select`` statement (without ORDER BY / LIMIT).
        cursor:
            Opaque cursor string from a previous page, or ``None``.
        limit:
            Number of items per page.
        sort_field:
            Override the default sort field.
        sort_dir:
            Override the default sort direction.
        model:
            The ORM model class (required to resolve column attributes).
        compute_total:
            If True, run an extra ``COUNT`` query to populate ``total``.

        """
        sf = sort_field or self.sort_field
        sd = sort_dir or self.sort_dir

        sort_col = getattr(model, sf)
        id_col = model.id  # type: ignore[attr-defined]

        if cursor:
            cursor_value_str, cursor_id = self.decode_cursor(cursor)
            cursor_value = self._parse_cursor_value(cursor_value_str)
            self._validate_cursor_value_type(cursor_value, sort_col)

            bound_cursor = literal(cursor_value)
            bound_id = literal(cursor_id)
            if sd == "desc":
                stmt = stmt.where(sa_tuple(sort_col, id_col) < sa_tuple(bound_cursor, bound_id))
            else:
                stmt = stmt.where(sa_tuple(sort_col, id_col) > sa_tuple(bound_cursor, bound_id))

        order_col = sort_col.desc() if sd == "desc" else sort_col.asc()
        id_order = id_col.desc() if sd == "desc" else id_col.asc()
        stmt = stmt.order_by(order_col, id_order)
        paginated = stmt.limit(limit + 1)

        rows = list((await session.execute(paginated)).scalars().all())

        has_more = len(rows) > limit
        items = rows[:limit]

        next_cursor: str | None = None
        if has_more:
            last = items[-1]
            next_cursor = self.encode_cursor(getattr(last, sf), last.id)

        total_count: int | None = None
        if compute_total:
            count_q = select(func.count()).select_from(stmt.order_by(None).subquery())
            total_count = (await session.execute(count_q)).scalar_one_or_none() or 0

        return CursorPage(
            items=items,
            next_cursor=next_cursor,
            total=total_count,
            has_more=has_more,
        )
