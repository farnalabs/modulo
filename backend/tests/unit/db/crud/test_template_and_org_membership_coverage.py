"""Real async CRUD coverage for templates and organisation memberships."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.db.crud.org_membership import (
    create_membership,
    get_membership_by_account_and_org,
    list_memberships_for_account,
    list_memberships_for_org,
    reactivate_membership,
    resolve_live_role_and_kind,
    resolve_role_from_membership,
    update_membership_role,
)
from farna.db.crud.template import (
    _agent_count_from_content,
    _preview_data_from_content,
    get_template,
    list_templates,
)
from farna.db.models.account import Account
from farna.db.models.base import Base
from farna.db.models.library_primitive import LibraryPrimitive
from farna.db.models.org_membership import OrgMembership

_ORG_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_OTHER_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    Account.__table__,
                    OrgMembership.__table__,
                    LibraryPrimitive.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


async def _seed_accounts(session: AsyncSession) -> None:
    session.add_all(
        [
            Account(id=_ACCOUNT, email="one@example.com", display_name="One", kind="human", active=True),
            Account(id=_OTHER_ACCOUNT, email="two@example.com", display_name="Two", kind="human", active=True),
        ]
    )
    await session.flush()


@pytest.mark.anyio
async def test_template_listing_filters_search_category_and_paginates(session: AsyncSession) -> None:
    session.add_all(
        [
            LibraryPrimitive(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                primitive_type="pipeline_template",
                category="review",
                name="Alpha review",
                source="local",
                slug="alpha-review",
                author="tester",
                version="1.0.0",
                content_json={},
            ),
            LibraryPrimitive(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                primitive_type="pipeline_template",
                category="build",
                name="Beta build",
                source="local",
                slug="beta-build",
                author="tester",
                version="1.0.0",
                content_json={},
            ),
            LibraryPrimitive(
                id=uuid.uuid4(),
                organisation_id=_ORG_A,
                primitive_type="agent",
                category="review",
                name="Not a template",
                source="local",
                slug="not-template",
                author="tester",
                version="1.0.0",
                content_json={},
            ),
        ]
    )
    await session.flush()

    result = await list_templates(session, page=1, page_size=1, category="review", search=" alpha ")

    assert result.total == 1
    assert len(result.items) == 1
    assert result.items[0].name == "Alpha review"
    assert result.has_more is False

    all_templates = await list_templates(session, page=1, page_size=20, search="   ")
    assert all_templates.total == 2

    with pytest.MonkeyPatch.context() as monkeypatch:
        from sqlalchemy.exc import ProgrammingError

        async def fail_execute(*_args: object, **_kwargs: object) -> object:
            raise ProgrammingError("missing", {}, RuntimeError("missing"))

        monkeypatch.setattr(session, "execute", fail_execute)
        fallback = await list_templates(session)
    assert fallback.total == 0
    assert not fallback.items


@pytest.mark.anyio
async def test_template_lookup_and_content_preview_helpers(session: AsyncSession) -> None:
    template_id = uuid.uuid4()
    session.add(
        LibraryPrimitive(
            id=template_id,
            organisation_id=_ORG_A,
            primitive_type="pipeline_template",
            name="Template",
            source="local",
            slug="template",
            author="tester",
            version="1.0.0",
            content_json={},
        )
    )
    await session.flush()

    found = await get_template(session, template_id)
    missing = await get_template(session, uuid.uuid4())

    assert found is not None
    assert missing is None
    assert _agent_count_from_content({"agents": [{"id": 1}, {"id": 2}]}) == 2
    assert _agent_count_from_content({}) == 0
    preview = _preview_data_from_content(
        {
            "graph_nodes": [{"id": "n1", "label": "Start", "node_type": "condition"}, {}],
            "edges": [{"source_node_id": "n1", "target_node_id": "n2", "edge_type": "conditional"}, {}],
        }
    )
    assert preview == {
        "nodes": [
            {"id": "n1", "label": "Start", "node_type": "condition"},
            {"id": "", "label": "", "node_type": "agent"},
        ],
        "edges": [
            {"source": "n1", "target": "n2", "edge_type": "conditional"},
            {"source": "", "target": "", "edge_type": "normal"},
        ],
    }


@pytest.mark.anyio
async def test_membership_crud_and_active_filters(session: AsyncSession) -> None:
    await _seed_accounts(session)
    active = await create_membership(session, account_id=_ACCOUNT, org_id=_ORG_A, role="runner")
    deactivated = await create_membership(session, account_id=_ACCOUNT, org_id=_ORG_B, role="viewer")
    deactivated.deactivated_at = datetime.now(UTC) - timedelta(seconds=1)
    other = await create_membership(session, account_id=_OTHER_ACCOUNT, org_id=_ORG_A, role="operator")
    await session.flush()

    assert await get_membership_by_account_and_org(session, _ACCOUNT, _ORG_A) is active
    assert await get_membership_by_account_and_org(session, _ACCOUNT, uuid.uuid4()) is None
    account_memberships = await list_memberships_for_account(session, _ACCOUNT)
    active_memberships = await list_memberships_for_account(session, _ACCOUNT, active_only=True)
    org_memberships = await list_memberships_for_org(session, _ORG_A)
    assert len(account_memberships) == 2
    assert active_memberships == [active]
    assert org_memberships == [active, other]

    assert await update_membership_role(session, active.id, "admin", org_id=_ORG_A) is active
    assert active.role == "admin"
    assert await update_membership_role(session, active.id, "viewer", org_id=_ORG_B) is None
    await reactivate_membership(session, deactivated, "runner")
    assert deactivated.deactivated_at is None
    assert deactivated.role == "runner"


@pytest.mark.anyio
async def test_membership_role_resolution_requires_live_account_and_membership(session: AsyncSession) -> None:
    await _seed_accounts(session)
    await create_membership(session, account_id=_ACCOUNT, org_id=_ORG_A, role="admin")
    await create_membership(session, account_id=_OTHER_ACCOUNT, org_id=_ORG_A, role="runner")
    await session.flush()

    assert await resolve_role_from_membership(session, _ACCOUNT, _ORG_A) == "admin"
    assert await resolve_live_role_and_kind(session, _ACCOUNT, _ORG_A) == ("admin", "human")

    (await get_membership_by_account_and_org(session, _OTHER_ACCOUNT, _ORG_A)).deactivated_at = datetime.now(UTC)
    (await session.get(Account, _OTHER_ACCOUNT)).active = False
    await session.flush()
    assert await resolve_role_from_membership(session, _OTHER_ACCOUNT, _ORG_A) is None
    assert await resolve_live_role_and_kind(session, _OTHER_ACCOUNT, _ORG_A) is None
    assert await resolve_live_role_and_kind(session, _ACCOUNT, _ORG_B) is None
