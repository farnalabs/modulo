"""Step definitions for admin tier-catalog BDD scenarios."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from tests.bdd.conftest import ORG_ID, _active_client, make_settings

scenarios("tier_catalog.feature")

_TIERS_CACHE_PREFIX = "tiers:"

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
async def _clear_tiers_cache() -> None:
    """Cold-start the org-scoped tier catalog cache before every scenario.

    The admin tiers endpoint reads/writes a per-organisation Redis cache with a
    300s TTL. An earlier scenario in this feature (e.g. "lists all plan tiers")
    warms the ``tiers:<org>`` key, which would otherwise make the empty /
    programming-error / database-error scenarios short-circuit inside the route
    and serve the cached populated catalog instead of exercising the freshly
    mocked ``list_tiers``. Deleting the key up front keeps each scenario
    hermetic.
    """
    redis: AsyncRedis | None = None
    try:
        redis = AsyncRedis.from_url(
            make_settings().redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        await redis.delete(_TIERS_CACHE_PREFIX + str(ORG_ID))
    except Exception:
        logger.warning("tiers.cache_clear_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()


_STANDARD_TIERS = [
    {
        "tier_id": "community",
        "label": "Community",
        "rank": 0,
        "requires_license": False,
        "description": "Free tier",
    },
    {
        "tier_id": "team",
        "label": "Team",
        "rank": 1,
        "requires_license": True,
        "description": "Team tier",
    },
]

_TIERS_PATCH_TARGET = "modulo.api.routes.admin_tiers.list_tiers"


_TIERS_MOCK_ATTR = "_tiers_catalog_mock"


@given(parsers.parse("the tier catalog contains the standard Community and Team tiers"))
def _given_tiers_present(request) -> None:
    _configure_tiers(request, _STANDARD_TIERS)


@given("the tier catalog is empty")
def _given_tiers_empty(request) -> None:
    _configure_tiers(request, [])


@given("the tier query raises a programming error")
def _given_tiers_programming_error(request) -> None:
    _configure_tiers_failure(request, ProgrammingError("stmt", {}, Exception("undef_table")))


@given("the tier query raises a database error")
def _given_tiers_db_error(request) -> None:
    _configure_tiers_failure(request, SQLAlchemyError("txn failed"))


def _configure_tiers(request, tiers) -> None:
    setattr(request.node, _TIERS_MOCK_ATTR, AsyncMock(return_value=tiers))


def _configure_tiers_failure(request, exc) -> None:
    setattr(request.node, _TIERS_MOCK_ATTR, AsyncMock(side_effect=exc))


@when("I request GET /api/v1/admin/tiers")
def _bdd_get_tiers(request) -> None:
    tiers_mock = getattr(request.node, _TIERS_MOCK_ATTR, AsyncMock(return_value=_STANDARD_TIERS))
    with (
        patch(_TIERS_PATCH_TARGET, tiers_mock),
        patch(
            "modulo.api.routes.admin_tiers.Redis.from_url",
            side_effect=RuntimeError("no redis in hermetic BDD scenarios"),
        ),
    ):
        request.node._resp = _active_client(request).get("/api/v1/admin/tiers")


@when("I request GET /api/v1/admin/tiers without authentication")
def _bdd_get_tiers_unauth(request, unauth_client: TestClient) -> None:
    request.node._resp = unauth_client.get("/api/v1/admin/tiers")


@then("the tiers are ordered by rank")
def _then_tiers_ordered(request) -> None:
    tiers = request.node._resp.json()["tiers"]
    ranks = [t["rank"] for t in tiers]
    assert ranks == sorted(ranks), f"Tiers not ordered by rank: {ranks}"


@then("each tier has tier_id, label, rank, requires_license, and description fields")
def _then_tier_shape(request) -> None:
    for tier in request.node._resp.json()["tiers"]:
        assert "tier_id" in tier
        assert "label" in tier
        assert "rank" in tier
        assert "requires_license" in tier
        assert "description" in tier


@then("the tiers array is empty")
def _then_tiers_empty(request) -> None:
    assert not request.node._resp.json()["tiers"]
