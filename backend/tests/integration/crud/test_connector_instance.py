"""Integration tests for ConnectorInstance CRUD.

RLS is set to test_org; all ORM changes are rolled back after each test.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.connector_instance import (
    create_connector_instance,
    delete_connector_instance,
    get_connector_instance,
    list_connector_instances,
    update_connector_instance,
)

pytestmark = pytest.mark.integration


def _ci_kwargs(test_org: uuid.UUID, test_user: uuid.UUID, *, suffix: str = "") -> dict:
    return {
        "org_id": test_org,
        "name": f"TestConnector{suffix}",
        "connector_type_id": "filesystem",
        "owner_id": test_user,
        "credentials_ciphertext": b"fake-cipher",
    }


async def test_create_connector_instance(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    ci = await create_connector_instance(rls_session, **_ci_kwargs(test_org, test_user))
    assert ci.id is not None
    assert ci.connector_type_id == "filesystem"
    assert ci.organisation_id == test_org


async def test_get_connector_instance_returns_existing(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    ci = await create_connector_instance(rls_session, **_ci_kwargs(test_org, test_user, suffix="-fetch"))
    fetched = await get_connector_instance(rls_session, ci.id)
    assert fetched is not None
    assert fetched.id == ci.id


async def test_get_connector_instance_returns_none_for_unknown(
    rls_session: AsyncSession,
) -> None:
    assert await get_connector_instance(rls_session, uuid.uuid4()) is None


async def test_list_connector_instances_pagination(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    for i in range(3):
        await create_connector_instance(
            rls_session,
            **_ci_kwargs(test_org, test_user, suffix=f"-list-{i}-{uuid.uuid4().hex[:4]}"),
        )
    page1 = await list_connector_instances(rls_session, page=1, page_size=2)
    assert page1.total >= 3
    assert len(page1.items) == 2
    assert page1.page == 1


async def test_update_connector_instance(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    ci = await create_connector_instance(rls_session, **_ci_kwargs(test_org, test_user, suffix="-upd"))
    updated = await update_connector_instance(rls_session, ci.id, {"name": "Renamed Connector"})
    assert updated is not None
    assert updated.name == "Renamed Connector"


async def test_update_connector_instance_unknown_returns_none(
    rls_session: AsyncSession,
) -> None:
    assert await update_connector_instance(rls_session, uuid.uuid4(), {"name": "x"}) is None


async def test_delete_connector_instance(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    ci = await create_connector_instance(rls_session, **_ci_kwargs(test_org, test_user, suffix="-del"))
    assert await delete_connector_instance(rls_session, ci.id) is True
    assert await get_connector_instance(rls_session, ci.id) is None


async def test_delete_connector_instance_unknown_returns_false(
    rls_session: AsyncSession,
) -> None:
    assert await delete_connector_instance(rls_session, uuid.uuid4()) is False


class TestListConnectorInstancesTierFiltering:
    """Server-side tier filtering for list_connector_instances."""

    async def _create_with_tier(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        tier: str,
        suffix: str,
    ) -> None:
        await create_connector_instance(
            rls_session,
            tier=tier,
            **_ci_kwargs(test_org, test_user, suffix=suffix),
        )

    async def test_default_excludes_in_dev(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        await self._create_with_tier(rls_session, test_org, test_user, "in_dev", "-tier-dev")
        await self._create_with_tier(rls_session, test_org, test_user, "preview", "-tier-prev")
        await self._create_with_tier(rls_session, test_org, test_user, "native", "-tier-nat")
        result = await list_connector_instances(rls_session)
        assert result.total == 2
        assert all(i.tier != "in_dev" for i in result.items)

    async def test_explicit_excluded_tiers_in_dev(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        await self._create_with_tier(rls_session, test_org, test_user, "in_dev", "-tier2-dev")
        await self._create_with_tier(rls_session, test_org, test_user, "native", "-tier2-nat")
        result = await list_connector_instances(rls_session, excluded_tiers=["in_dev"])
        assert result.total == 1
        assert result.items[0].tier == "native"

    async def test_excluded_tiers_none_defaults_to_in_dev(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        await self._create_with_tier(rls_session, test_org, test_user, "in_dev", "-tier3-dev")
        await self._create_with_tier(rls_session, test_org, test_user, "native", "-tier3-nat")
        result = await list_connector_instances(rls_session, excluded_tiers=None)
        assert result.total == 1
        assert result.items[0].tier == "native"

    async def test_excluded_tiers_empty_skips_filter(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        await self._create_with_tier(rls_session, test_org, test_user, "in_dev", "-tier4-dev")
        await self._create_with_tier(rls_session, test_org, test_user, "native", "-tier4-nat")
        result = await list_connector_instances(rls_session, excluded_tiers=[])
        assert result.total == 2

    async def test_excluded_tiers_preview(
        self,
        rls_session: AsyncSession,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        await self._create_with_tier(rls_session, test_org, test_user, "in_dev", "-tier5-dev")
        await self._create_with_tier(rls_session, test_org, test_user, "preview", "-tier5-prev")
        await self._create_with_tier(rls_session, test_org, test_user, "native", "-tier5-nat")
        result = await list_connector_instances(rls_session, excluded_tiers=["preview"])
        assert result.total == 2
        assert all(i.tier != "preview" for i in result.items)
        tiers = {i.tier for i in result.items}
        assert tiers == {"in_dev", "native"}
