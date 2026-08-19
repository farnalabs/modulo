"""Step definitions for admin tier-catalog BDD scenarios."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from tests.bdd.conftest import _active_client

scenarios("tier_catalog.feature")

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


class _NoCacheRedis:
    """Redis double that always misses so the endpoint hits the CRUD mock.

    The tier endpoint reads through a Redis cache keyed by org before
    querying the DB. In CI the live Redis is reachable, so an earlier
    scenario's cached standard tiers short-circuit the patched CRUD call
    (returning real cached tiers / 200 instead of the empty list or the
    501/503 fault). A cache that always misses keeps the scenarios
    deterministic.
    """

    async def get(self, key: str) -> None:
        return None

    async def setex(self, key: str, ttl: int, value: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


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
        patch("modulo.api.routes.admin_tiers.Redis.from_url", return_value=_NoCacheRedis()),
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
