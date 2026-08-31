"""Integration tests for ConnectorInstance CRUD.

RLS is set to test_org; all ORM changes are rolled back after each test.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from modulo.db.crud.connector_instance import (
    clear_degraded_markers,
    create_connector_instance,
    delete_connector_instance,
    get_connector_instance,
    list_connector_instances,
    mark_instances_degraded,
    update_connector_instance,
)
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


def _ci_kwargs(test_org: uuid.UUID, test_user: uuid.UUID, *, suffix: str = "") -> dict:
    return {
        "org_id": test_org,
        "name": f"TestConnector{suffix}",
        "connector_type_id": "filesystem",
        "account_id": test_user,
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
    migrated_db_url: str,
    test_org: uuid.UUID,
) -> None:
    """Update on an unknown connector instance returns None.

    This test owns its engine with ``NullPool`` (matching the ``db_engine`` /
    ``app_engine`` fixtures) instead of the shared ``rls_session`` fixture,
    whose pooled engine raced asyncpg connection closure at teardown — the
    flake this test was skipped for. NullPool opens a fresh connection per
    checkout and disposes it on the same event loop, so teardown never races a
    pooled connection close. The fix belongs in the ``rls_session`` fixture
    (add ``poolclass=NullPool``); this self-contained form keeps the test
    robust without a conftest change.
    """
    engine = create_async_engine(migrated_db_url, echo=False, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(text("SELECT 1"))
            await set_rls_org(session, test_org)
            assert await update_connector_instance(session, uuid.uuid4(), {"name": "x"}) is None
            await session.rollback()
    finally:
        await engine.dispose()


async def test_delete_connector_instance(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    ci = await create_connector_instance(rls_session, **_ci_kwargs(test_org, test_user, suffix="-del"))
    assert await delete_connector_instance(rls_session, ci.id) is True
    assert await get_connector_instance(rls_session, ci.id) is None


async def test_delete_connector_instance_unknown_returns_false(
    rls_session: AsyncSession,
) -> None:
    assert await delete_connector_instance(rls_session, uuid.uuid4()) is False


async def test_mark_instances_degraded_persists_marker(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-495: mark_instances_degraded writes degraded_at/last_skip_error."""
    ci = await create_connector_instance(rls_session, **_ci_kwargs(test_org, test_user, suffix="-degraded"))
    await mark_instances_degraded(rls_session, {ci.id: "ValueError: Missing credential key 'token'"})
    fetched = await get_connector_instance(rls_session, ci.id)
    assert fetched is not None
    assert fetched.degraded_at is not None
    assert fetched.last_skip_error == "ValueError: Missing credential key 'token'"


async def test_mark_instances_degraded_sanitizes_overlong_and_nul_summary(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-498: the writer sanitizes summaries itself (NUL-strip + truncate to 2000).

    Defense-in-depth: the hub sanitizes what it records, but a future caller
    could bypass it. Postgres rejects NUL bytes in SQL text (a NUL in any
    batched summary fails the WHOLE UPDATE so no instance gets marked) and the
    column is String(2000).
    """
    ci = await create_connector_instance(rls_session, **_ci_kwargs(test_org, test_user, suffix="-sanitized"))
    summary = f"RuntimeError: bad\x00summary{'x' * 3000}"
    await mark_instances_degraded(rls_session, {ci.id: summary})
    fetched = await get_connector_instance(rls_session, ci.id)
    assert fetched is not None
    assert fetched.degraded_at is not None
    assert fetched.last_skip_error is not None
    assert "\x00" not in fetched.last_skip_error
    assert len(fetched.last_skip_error) == 2000
    assert fetched.last_skip_error.startswith("RuntimeError: badsummary")


async def test_mark_instances_degraded_empty_dict_is_noop(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-495: an empty skipped dict leaves rows untouched."""
    ci = await create_connector_instance(rls_session, **_ci_kwargs(test_org, test_user, suffix="-noop"))
    await mark_instances_degraded(rls_session, {})
    fetched = await get_connector_instance(rls_session, ci.id)
    assert fetched is not None
    assert fetched.degraded_at is None
    assert fetched.last_skip_error is None


async def test_clear_degraded_markers_persists_nulls(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-495: clear_degraded_markers resets degraded_at/last_skip_error to NULL."""
    ci = await create_connector_instance(rls_session, **_ci_kwargs(test_org, test_user, suffix="-cleared"))
    await mark_instances_degraded(rls_session, {ci.id: "ValueError: Missing credential key 'token'"})
    await clear_degraded_markers(rls_session, {ci.id})
    fetched = await get_connector_instance(rls_session, ci.id)
    assert fetched is not None
    assert fetched.degraded_at is None
    assert fetched.last_skip_error is None


async def test_clear_degraded_markers_empty_collection_is_noop(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """FAR-495: an empty instance-id collection leaves existing markers untouched."""
    ci = await create_connector_instance(rls_session, **_ci_kwargs(test_org, test_user, suffix="-clear-noop"))
    await mark_instances_degraded(rls_session, {ci.id: "ValueError: boom"})
    await clear_degraded_markers(rls_session, set())
    fetched = await get_connector_instance(rls_session, ci.id)
    assert fetched is not None
    assert fetched.degraded_at is not None
    assert fetched.last_skip_error == "ValueError: boom"


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
