"""Unit tests for library_service error paths and edge cases.

Covers:
  - get_primitive: non-transaction path, ProgrammingError, SQLAlchemyError
  - get_primitive_by_slug: full function (DB hit, modulo/community fallback,
    ProgrammingError, SQLAlchemyError)
  - copy_to_adapt: created_by RLS user context, registry download-count
    increment, refreshed-None LookupError, ProgrammingError
  - list_primitives: ProgrammingError and generic exception degradation
  - _fetch_published_community_from_db: search filter
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.core.library_service import (
    _MODULO_PRIMITIVES,
    _fetch_published_community_from_db,
    copy_to_adapt,
    get_primitive,
    get_primitive_by_slug,
    list_primitives,
)
from modulo.db.crud.base import PageResult


def _fake_primitive(*, pid: uuid.UUID | None = None, **overrides: object) -> MagicMock:
    p = MagicMock()
    p.id = pid or uuid.uuid4()
    p.visibility = "org"
    p.primitive_type = "schema"
    p.name = "Test Prim"
    p.slug = "test-prim"
    p.description = "A test primitive"
    p.author = "tester"
    p.version = "1.0"
    p.tags = []
    p.content_json = {}
    p.source = "local"
    p.tier = "native"
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def _mock_session(*, in_transaction: bool = True) -> MagicMock:
    """Return a mock AsyncSession supporting ``async with session.begin():``."""
    session = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=in_transaction)
    session.execute = AsyncMock()
    return session


def _scalar_one_result(value: object) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


# ---------------------------------------------------------------------------
# get_primitive — non-transaction + error paths
# ---------------------------------------------------------------------------


async def test_get_primitive_uses_own_transaction_when_not_in_one():
    """When not inside a caller transaction, get_primitive starts its own."""
    session = _mock_session(in_transaction=False)
    org_id = uuid.uuid4()
    prim = _fake_primitive()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
    ):
        result = await get_primitive(session, org_id, prim.id)

    assert result is prim
    session.begin.assert_called_once()


async def test_get_primitive_returns_none_on_programming_error():
    """Missing table/migration is treated as 'not found'."""
    session = _mock_session()
    org_id = uuid.uuid4()
    pid = uuid.uuid4()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.get_library_primitive",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
        ),
    ):
        result = await get_primitive(session, org_id, pid)

    assert result is None


async def test_get_primitive_raises_on_sqlalchemy_error():
    """Non-migration DB errors must propagate."""
    session = _mock_session()
    org_id = uuid.uuid4()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.get_library_primitive",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("boom"),
        ),
        pytest.raises(SQLAlchemyError),
    ):
        await get_primitive(session, org_id, uuid.uuid4())


# ---------------------------------------------------------------------------
# get_primitive_by_slug
# ---------------------------------------------------------------------------


async def test_get_primitive_by_slug_found_in_db():
    session = _mock_session()
    org_id = uuid.uuid4()
    prim = _fake_primitive()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch.object(session, "execute", new_callable=AsyncMock, return_value=_scalar_one_result(prim)),
    ):
        result = await get_primitive_by_slug(session, org_id, "schema", "test-prim")

    assert result is prim


async def test_get_primitive_by_slug_uses_own_transaction_when_not_in_one():
    session = _mock_session(in_transaction=False)
    org_id = uuid.uuid4()
    prim = _fake_primitive()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch.object(session, "execute", new_callable=AsyncMock, return_value=_scalar_one_result(prim)),
    ):
        result = await get_primitive_by_slug(session, org_id, "schema", "test-prim")

    assert result is prim
    session.begin.assert_called_once()


async def test_get_primitive_by_slug_falls_back_to_modulo():
    session = _mock_session()
    org_id = uuid.uuid4()
    native = _MODULO_PRIMITIVES[0]

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch.object(session, "execute", new_callable=AsyncMock, return_value=_scalar_one_result(None)),
    ):
        result = await get_primitive_by_slug(session, org_id, native.primitive_type, native.slug)

    assert result is native


async def test_get_primitive_by_slug_returns_none_when_unmatched():
    session = _mock_session()
    org_id = uuid.uuid4()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch.object(session, "execute", new_callable=AsyncMock, return_value=_scalar_one_result(None)),
    ):
        result = await get_primitive_by_slug(session, org_id, "schema", "zzz-no-such-slug")

    assert result is None


async def test_get_primitive_by_slug_returns_none_on_programming_error():
    session = _mock_session()
    org_id = uuid.uuid4()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch.object(
            session,
            "execute",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
        ),
    ):
        result = await get_primitive_by_slug(session, org_id, "schema", "test-prim")

    assert result is None


async def test_get_primitive_by_slug_raises_on_sqlalchemy_error():
    session = _mock_session()
    org_id = uuid.uuid4()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch.object(
            session,
            "execute",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("boom"),
        ),
        pytest.raises(SQLAlchemyError),
    ):
        await get_primitive_by_slug(session, org_id, "schema", "test-prim")


# ---------------------------------------------------------------------------
# copy_to_adapt — edge cases
# ---------------------------------------------------------------------------


async def test_copy_to_adapt_sets_user_context_when_created_by_provided():
    session = _mock_session()
    org_id = uuid.uuid4()
    created_by = uuid.uuid4()
    source = _fake_primitive(visibility="org")
    copied = _fake_primitive()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.set_rls_user_context", new_callable=AsyncMock) as set_user,
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=source),
        patch("modulo.core.library_service.create_library_primitive", new_callable=AsyncMock, return_value=copied),
    ):
        result = await copy_to_adapt(session, org_id, source.id, created_by=created_by, org_role="member")

    assert result is copied
    set_user.assert_awaited_once_with(session, created_by, "member")


async def test_copy_to_adapt_does_not_set_user_context_without_created_by():
    session = _mock_session()
    org_id = uuid.uuid4()
    source = _fake_primitive(visibility="org")
    copied = _fake_primitive()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.set_rls_user_context", new_callable=AsyncMock) as set_user,
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=source),
        patch("modulo.core.library_service.create_library_primitive", new_callable=AsyncMock, return_value=copied),
    ):
        await copy_to_adapt(session, org_id, source.id)

    set_user.assert_not_called()


async def test_copy_to_adapt_raises_when_source_disappears_after_refresh():
    """The in-transaction re-read returning None must raise LookupError."""
    session = _mock_session()
    org_id = uuid.uuid4()
    pid = uuid.uuid4()

    # First call (pre-transaction) finds it; in-transaction re-read finds nothing.
    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.get_library_primitive",
            new_callable=AsyncMock,
            return_value=None,
        ),
        pytest.raises(LookupError, match="not found for org"),
    ):
        await copy_to_adapt(session, org_id, pid)


async def test_copy_to_adapt_raises_when_in_transaction_reread_is_missing():
    """copy_to_adapt line 1112: get_primitive succeeds but the in-transaction
    re-read + cache fallback finds nothing."""
    session = _mock_session()
    org_id = uuid.uuid4()
    pid = uuid.uuid4()
    source = _fake_primitive(pid=pid, visibility="org")

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_primitive", new_callable=AsyncMock, return_value=source),
        patch(
            "modulo.core.library_service.get_library_primitive",
            new_callable=AsyncMock,
            return_value=None,
        ),
        pytest.raises(LookupError, match="during copy"),
    ):
        await copy_to_adapt(session, org_id, pid)


async def test_copy_to_adapt_increments_download_count_for_registry_source():
    session = _mock_session()
    org_id = uuid.uuid4()
    registry_prim = _fake_primitive(visibility="org")
    registry_prim.source = "registry"
    registry_prim.content_json = {"a": 1}
    copied = _fake_primitive()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=registry_prim),
        patch("modulo.core.library_service.create_library_primitive", new_callable=AsyncMock, return_value=copied),
    ):
        await copy_to_adapt(session, org_id, registry_prim.id)

    # The atomic download_count increment must be issued via session.execute.
    session.execute.assert_called_once()
    stmt = session.execute.call_args[0][0]
    assert "download_count" in str(stmt)


async def test_copy_to_adapt_programming_error_propagates():
    session = _mock_session()
    org_id = uuid.uuid4()
    prim = _fake_primitive(visibility="org")

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
        patch(
            "modulo.core.library_service.create_library_primitive",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
        ),
        pytest.raises(ProgrammingError),
    ):
        await copy_to_adapt(session, org_id, prim.id)


# ---------------------------------------------------------------------------
# list_primitives — degradation paths
# ---------------------------------------------------------------------------


async def test_list_primitives_degrades_on_programming_error():
    """Missing DB table yields a page containing only in-memory items."""
    session = _mock_session()
    org_id = uuid.uuid4()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.list_library_primitives",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
        ),
    ):
        result = await list_primitives(session, org_id)

    assert result.total > 0
    assert all(p.source in {"modulo", "community"} for p in result.items)


async def test_list_primitives_degrades_on_generic_error():
    """Unexpected DB errors degrade to in-memory results, not a crash."""
    session = _mock_session()
    org_id = uuid.uuid4()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.list_library_primitives",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        result = await list_primitives(session, org_id)

    assert result.total > 0


async def test_list_primitives_propagates_cancellation():
    """Cancellation must never be swallowed by the degradation handler."""
    session = _mock_session()
    org_id = uuid.uuid4()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.list_library_primitives",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError(),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await list_primitives(session, org_id)


async def test_list_primitives_dedupes_db_community_against_in_memory():
    """DB community rows that match in-memory ids must not be double-listed."""
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    from modulo.core.library_service import _COMMUNITY_PRIMITIVES

    first = _COMMUNITY_PRIMITIVES[0]
    duplicate = _fake_primitive(pid=first.id, visibility="community")
    extra = _fake_primitive(visibility="community")

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page),
        patch(
            "modulo.core.library_service._fetch_published_community_from_db",
            new_callable=AsyncMock,
            return_value=[duplicate, extra],
        ),
    ):
        result = await list_primitives(session, org_id)

    ids = [p.id for p in result.items]
    assert ids.count(first.id) == 1
    assert extra.id in ids


# ---------------------------------------------------------------------------
# _fetch_published_community_from_db — search + cancellation
# ---------------------------------------------------------------------------


async def test_fetch_published_community_applies_search_filter():
    session = _mock_session()
    org_id = uuid.uuid4()
    session.info = {}

    captured_stmt = None

    async def _execute(stmt, *args: object, **kwargs: object) -> MagicMock:
        from modulo.core.library_service import _COMMUNITY_PRIMITIVES

        nonlocal captured_stmt
        captured_stmt = stmt
        return MagicMock(scalars=MagicMock(return_value=_COMMUNITY_PRIMITIVES))

    session.execute = AsyncMock(side_effect=_execute)

    result = await _fetch_published_community_from_db(session, org_id, search="  French  ")

    from modulo.core.library_service import _COMMUNITY_PRIMITIVES as EXPECTED

    assert captured_stmt is not None
    compiled = str(captured_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "%French%" in compiled, "search term must be applied to the SQL statement"
    assert "%  French  %" not in compiled, "search term must be stripped before use"
    assert result == EXPECTED


async def test_fetch_published_community_propagates_cancellation():
    session = _mock_session()
    org_id = uuid.uuid4()
    session.info = {}
    session.execute = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _fetch_published_community_from_db(session, org_id)
