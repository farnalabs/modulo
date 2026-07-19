"""Cross-tenant scoping tests for CRUD functions that filter by primary key.

These verify the explicit app-layer org/account filters added as a backstop
for tables without an RLS policy. Uses a real in-memory SQLite database so
the WHERE clauses are actually executed (no ORM tenant-filter listener is
active — session.info carries no org context here).
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.node_category import (
    delete_node_category,
    get_node_category,
    list_node_categories,
    update_node_category,
)
from modulo.db.crud.org_membership import update_membership_role
from modulo.db.crud.publisher import delete_publisher, get_publisher, update_publisher
from modulo.db.crud.token_family import (
    advance_sequence,
    blacklist_family,
    get_or_create_family,
    is_family_blacklisted,
)
from modulo.db.models.base import Base
from modulo.db.models.node_category import NodeCategory
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.publisher import Publisher
from modulo.db.models.token_family import TokenFamily

_TABLE_NAMES = {"node_categories", "publishers", "org_memberships", "token_families"}

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_ACCOUNT_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_ACCOUNT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b1")


@pytest.fixture()
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture()
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed_category(session: AsyncSession) -> NodeCategory:
    category = NodeCategory(
        organisation_id=_ORG_A,
        name="Org A Category",
        account_id=_ACCOUNT_A,
    )
    session.add(category)
    await session.flush()
    return category


async def _seed_publisher(session: AsyncSession) -> Publisher:
    publisher = Publisher(
        organisation_id=_ORG_A,
        name="Org A Publisher",
        contact_email=None,
        public_key_hex="ab" * 32,
        trust_tier="amber",
    )
    session.add(publisher)
    await session.flush()
    return publisher


async def _seed_membership(session: AsyncSession) -> OrgMembership:
    membership = OrgMembership(
        account_id=_ACCOUNT_A,
        organisation_id=_ORG_A,
        role="runner",
    )
    session.add(membership)
    await session.flush()
    return membership


async def _seed_family(session: AsyncSession) -> TokenFamily:
    family = TokenFamily(
        family_id=uuid.uuid4(),
        account_id=_ACCOUNT_A,
        organisation_id=_ORG_A,
        max_sequence=0,
    )
    session.add(family)
    await session.flush()
    return family


class TestNodeCategoryOrgScoping:
    async def test_get_same_org_returns_category(self, session: AsyncSession) -> None:
        category = await _seed_category(session)
        result = await get_node_category(session, category.id, org_id=_ORG_A)
        assert result is not None
        assert result.id == category.id

    async def test_get_other_org_returns_none(self, session: AsyncSession) -> None:
        category = await _seed_category(session)
        assert await get_node_category(session, category.id, org_id=_ORG_B) is None

    async def test_list_same_org_returns_items(self, session: AsyncSession) -> None:
        await _seed_category(session)
        result = await list_node_categories(session, org_id=_ORG_A)
        assert result.total == 1
        assert len(result.items) == 1

    async def test_list_other_org_returns_empty(self, session: AsyncSession) -> None:
        await _seed_category(session)
        result = await list_node_categories(session, org_id=_ORG_B)
        assert result.total == 0
        assert result.items == []

    async def test_update_other_org_returns_none(self, session: AsyncSession) -> None:
        category = await _seed_category(session)
        result = await update_node_category(session, category.id, {"name": "Hijacked"}, org_id=_ORG_B)
        assert result is None
        assert category.name == "Org A Category"

    async def test_delete_other_org_returns_false(self, session: AsyncSession) -> None:
        category = await _seed_category(session)
        assert await delete_node_category(session, category.id, org_id=_ORG_B) is False
        assert await get_node_category(session, category.id, org_id=_ORG_A) is not None

    async def test_delete_same_org_returns_true(self, session: AsyncSession) -> None:
        category = await _seed_category(session)
        assert await delete_node_category(session, category.id, org_id=_ORG_A) is True
        assert await get_node_category(session, category.id, org_id=_ORG_A) is None


class TestPublisherOrgScoping:
    async def test_get_same_org_returns_publisher(self, session: AsyncSession) -> None:
        publisher = await _seed_publisher(session)
        result = await get_publisher(session, publisher.id, org_id=_ORG_A)
        assert result is not None
        assert result.id == publisher.id

    async def test_get_other_org_returns_none(self, session: AsyncSession) -> None:
        publisher = await _seed_publisher(session)
        assert await get_publisher(session, publisher.id, org_id=_ORG_B) is None

    async def test_update_other_org_returns_none(self, session: AsyncSession) -> None:
        publisher = await _seed_publisher(session)
        result = await update_publisher(session, publisher.id, {"name": "Hijacked"}, org_id=_ORG_B)
        assert result is None
        assert publisher.name == "Org A Publisher"

    async def test_delete_other_org_returns_false(self, session: AsyncSession) -> None:
        publisher = await _seed_publisher(session)
        assert await delete_publisher(session, publisher.id, org_id=_ORG_B) is False
        assert await get_publisher(session, publisher.id, org_id=_ORG_A) is not None

    async def test_delete_same_org_returns_true(self, session: AsyncSession) -> None:
        publisher = await _seed_publisher(session)
        assert await delete_publisher(session, publisher.id, org_id=_ORG_A) is True
        assert await get_publisher(session, publisher.id, org_id=_ORG_A) is None


class TestOrgMembershipOrgScoping:
    async def test_update_role_same_org_updates(self, session: AsyncSession) -> None:
        membership = await _seed_membership(session)
        result = await update_membership_role(session, membership.id, "admin", org_id=_ORG_A)
        assert result is not None
        assert result.role == "admin"

    async def test_update_role_other_org_returns_none(self, session: AsyncSession) -> None:
        membership = await _seed_membership(session)
        result = await update_membership_role(session, membership.id, "admin", org_id=_ORG_B)
        assert result is None
        assert membership.role == "runner"


class TestTokenFamilyAccountScoping:
    async def test_get_or_create_same_account_returns_existing(self, session: AsyncSession) -> None:
        family = await _seed_family(session)
        result = await get_or_create_family(session, family.family_id, _ACCOUNT_A, _ORG_A)
        assert result.family_id == family.family_id
        assert result.max_sequence == family.max_sequence

    async def test_advance_sequence_other_account_finds_nothing(self, session: AsyncSession) -> None:
        family = await _seed_family(session)
        new_sequence, theft_detected = await advance_sequence(session, family.family_id, 0, _ACCOUNT_B)
        assert (new_sequence, theft_detected) == (0, False)
        assert family.max_sequence == 0

    async def test_advance_sequence_same_account_advances(self, session: AsyncSession) -> None:
        family = await _seed_family(session)
        new_sequence, theft_detected = await advance_sequence(session, family.family_id, 0, _ACCOUNT_A)
        assert (new_sequence, theft_detected) == (1, False)

    async def test_blacklist_other_account_returns_false(self, session: AsyncSession) -> None:
        family = await _seed_family(session)
        assert await blacklist_family(session, family.family_id, _ACCOUNT_B) is False
        assert family.is_blacklisted is False

    async def test_blacklist_same_account_returns_true(self, session: AsyncSession) -> None:
        family = await _seed_family(session)
        assert await blacklist_family(session, family.family_id, _ACCOUNT_A) is True
        assert family.is_blacklisted is True

    async def test_is_blacklisted_other_account_returns_false(self, session: AsyncSession) -> None:
        family = await _seed_family(session)
        await blacklist_family(session, family.family_id, _ACCOUNT_A)
        assert await is_family_blacklisted(session, family.family_id, _ACCOUNT_B) is False
        assert await is_family_blacklisted(session, family.family_id, _ACCOUNT_A) is True
