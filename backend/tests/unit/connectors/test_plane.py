"""Unit tests for the Plane connector (FAR-1767).

No network, no Docker: every HTTP exchange is respx-mocked against the
Plane v1 API routes the connector was verified against (projects / work items /
by-identifier / search). Integration against a real Plane container is deferred
to CI — this workstation has no Docker daemon.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

import modulo.connectors.plane as plane_mod
from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.plane import PlaneConnector

BASE = "https://plane.example.com/api/v1"
WS = f"{BASE}/workspaces/acme"
PROJECTS = f"{WS}/projects/"
PROJECT_ID = "11111111-1111-1111-1111-111111111111"
ITEM_ID = "22222222-2222-2222-2222-222222222222"
TOKEN = "plane_test_token"  # nosec B105 - test fixture value, not a credential

_ENVELOPE: dict[str, Any] = {
    "next_cursor": "20:1:0",
    "prev_cursor": None,
    "next_page_results": True,
    "prev_page_results": False,
    "count": 1,
    "total_pages": 8,
    "total_results": 150,
    "results": [{"id": ITEM_ID, "identifier": "PROJ-1", "name": "First work item"}],
}


@pytest.fixture
def plane() -> PlaneConnector:
    return PlaneConnector(token=TOKEN, base_url=BASE, workspace_slug="acme")


# ---------------------------------------------------------------------------
# Identity, config validation, health
# ---------------------------------------------------------------------------


def test_connector_type(plane: PlaneConnector) -> None:
    """The hub-native id is "plane"; the object's family type is TICKET_TRACKER."""
    assert plane.connector_type == ConnectorType.TICKET_TRACKER


def test_constructor_rejects_blank_base_url() -> None:
    with pytest.raises(ValueError, match="base_url is required"):
        PlaneConnector(token=TOKEN, base_url="  /", workspace_slug="acme")


@pytest.mark.parametrize("bad_slug", ["", "  ", "acme/other", "..", "../other"])
def test_constructor_rejects_unsafe_workspace_slug(bad_slug: str) -> None:
    with pytest.raises(ValueError, match="workspace_slug must be a single non-empty path segment"):
        PlaneConnector(token=TOKEN, base_url=BASE, workspace_slug=bad_slug)


@respx.mock
async def test_health_check_ok(plane: PlaneConnector) -> None:
    respx.get(PROJECTS).mock(return_value=httpx.Response(200, json=_ENVELOPE))
    result = await plane.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_reports_missing_key(plane: PlaneConnector) -> None:
    respx.get(PROJECTS).mock(return_value=httpx.Response(401, text='{"detail": "Invalid API key"}'))
    result = await plane.health_check()
    assert result.ok is False
    assert "authentication failed" in result.detail


@respx.mock
async def test_health_check_reports_unknown_workspace(plane: PlaneConnector) -> None:
    respx.get(PROJECTS).mock(return_value=httpx.Response(404, text="not found"))
    result = await plane.health_check()
    assert result.ok is False
    assert "404" in result.detail


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_projects_returns_the_cursor_envelope(plane: PlaneConnector) -> None:
    route = respx.get(PROJECTS).mock(return_value=httpx.Response(200, json=_ENVELOPE))

    result = await plane.query(ConnectorQuery(resource="projects", limit=25))

    assert [record["identifier"] for record in result.records] == ["PROJ-1"]
    assert result.next_cursor == "20:1:0"
    assert result.total == 150
    params = route.calls[0].request.url.params
    assert params["per_page"] == "25"
    assert route.calls[0].request.headers["X-API-Key"] == TOKEN


@respx.mock
async def test_query_rejects_an_unsupported_filter(plane: PlaneConnector) -> None:
    """An unknown filter fails loud — never silently dropped.

    The self-hosted edition 400s on ``pql``/``filters`` and ignores nothing
    else worth forwarding, so a filter we do not know about must not pretend
    to have been applied.
    """
    with pytest.raises(ValueError, match="Unsupported Plane filter 'state'"):
        await plane.query(ConnectorQuery(resource="projects", filters={"state": "started"}))


async def test_query_project_requires_project_id(plane: PlaneConnector) -> None:
    with pytest.raises(ValueError, match="requires a 'project_id' filter"):
        await plane.query(ConnectorQuery(resource="project"))


@respx.mock
async def test_query_project(plane: PlaneConnector) -> None:
    route = respx.get(f"{WS}/projects/{PROJECT_ID}/").mock(
        return_value=httpx.Response(200, json={"id": PROJECT_ID, "identifier": "PROJ"}),
    )

    result = await plane.query(ConnectorQuery(resource="project", filters={"project_id": PROJECT_ID}))

    assert result.records[0]["identifier"] == "PROJ"
    assert "per_page" not in route.calls[0].request.url.params


@respx.mock
async def test_query_work_items(plane: PlaneConnector) -> None:
    route = respx.get(f"{WS}/projects/{PROJECT_ID}/work-items/").mock(
        return_value=httpx.Response(200, json=_ENVELOPE),
    )

    result = await plane.query(
        ConnectorQuery(
            resource="work_items",
            filters={"project_id": PROJECT_ID, "order_by": "-created_at"},
            limit=10,
            cursor="20:0:0",
        ),
    )

    assert result.records[0]["identifier"] == "PROJ-1"
    assert result.next_cursor == "20:1:0"
    params = route.calls[0].request.url.params
    assert params["per_page"] == "10"
    assert params["cursor"] == "20:0:0"
    assert params["order_by"] == "-created_at"
    # the routing selector built the path, it is not also a query parameter
    assert "project_id" not in params


async def test_query_work_items_requires_project_id(plane: PlaneConnector) -> None:
    with pytest.raises(ValueError, match="requires a 'project_id' filter"):
        await plane.query(ConnectorQuery(resource="work_items"))


@respx.mock
async def test_query_work_item_by_identifier(plane: PlaneConnector) -> None:
    route = respx.get(f"{WS}/work-items/PROJ-123/").mock(
        return_value=httpx.Response(200, json={"id": ITEM_ID, "identifier": "PROJ-123"}),
    )

    result = await plane.query(ConnectorQuery(resource="work_item", filters={"identifier": "PROJ-123"}))

    assert result.records[0]["identifier"] == "PROJ-123"
    assert route.called


@respx.mock
async def test_query_work_item_by_ids(plane: PlaneConnector) -> None:
    respx.get(f"{WS}/projects/{PROJECT_ID}/work-items/{ITEM_ID}/").mock(
        return_value=httpx.Response(200, json={"id": ITEM_ID, "identifier": "PROJ-9"}),
    )

    result = await plane.query(
        ConnectorQuery(resource="work_item", filters={"project_id": PROJECT_ID, "work_item_id": ITEM_ID}),
    )

    assert result.records[0]["id"] == ITEM_ID


async def test_query_work_item_without_identifier_requires_ids(plane: PlaneConnector) -> None:
    """A work_item query without ``identifier`` must carry project + item ids."""
    # no respx routes: reaching the client would raise AllMockedAssertionError,
    # so the filters are validated before any HTTP.
    with pytest.raises(ValueError, match="requires a 'project_id' filter"):
        await plane.query(ConnectorQuery(resource="work_item"))


@respx.mock
async def test_query_search(plane: PlaneConnector) -> None:
    route = respx.get(f"{WS}/work-items/search/").mock(return_value=httpx.Response(200, json=_ENVELOPE))

    result = await plane.query(ConnectorQuery(resource="search", filters={"q": "login bug"}))

    assert result.records
    assert route.calls[0].request.url.params["q"] == "login bug"


async def test_query_search_requires_q(plane: PlaneConnector) -> None:
    with pytest.raises(ValueError, match="requires a 'q' filter"):
        await plane.query(ConnectorQuery(resource="search"))


async def test_query_unknown_resource_raises(plane: PlaneConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Plane query resource"):
        await plane.query(ConnectorQuery(resource="definitely-not-a-resource"))


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_creates_a_work_item(plane: PlaneConnector) -> None:
    route = respx.post(f"{WS}/projects/{PROJECT_ID}/work-items/").mock(
        return_value=httpx.Response(201, json={"id": ITEM_ID, "identifier": "PROJ-12"}),
    )

    result = await plane.write(
        ConnectorPayload(
            resource="work_item",
            data={"project_id": PROJECT_ID, "name": "Fix login bug", "priority": "high"},
        ),
    )

    assert result["identifier"] == "PROJ-12"
    assert route.calls[0].request.headers["X-API-Key"] == TOKEN
    body = route.calls[0].request.content
    assert b"project_id" not in body
    assert b"Fix login bug" in body


async def test_write_create_requires_project_id(plane: PlaneConnector) -> None:
    with pytest.raises(ValueError, match="requires 'project_id' in data"):
        await plane.write(ConnectorPayload(resource="work_item", data={"name": "orphan"}))


@respx.mock
async def test_write_updates_a_work_item(plane: PlaneConnector) -> None:
    route = respx.patch(f"{WS}/projects/{PROJECT_ID}/work-items/{ITEM_ID}/").mock(
        return_value=httpx.Response(200, json={"id": ITEM_ID, "name": "Renamed"}),
    )

    result = await plane.write(
        ConnectorPayload(
            resource="work_item_update",
            data={"project_id": PROJECT_ID, "work_item_id": ITEM_ID, "name": "Renamed"},
        ),
    )

    assert result["name"] == "Renamed"
    body = route.calls[0].request.content
    assert b"project_id" not in body
    assert b"work_item_id" not in body
    assert b"Renamed" in body


async def test_write_update_requires_work_item_id(plane: PlaneConnector) -> None:
    with pytest.raises(ValueError, match="requires 'work_item_id' in data"):
        await plane.write(
            ConnectorPayload(resource="work_item_update", data={"project_id": PROJECT_ID, "name": "x"}),
        )


async def test_write_unknown_resource_raises(plane: PlaneConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Plane write resource"):
        await plane.write(ConnectorPayload(resource="comment", data={}))


# ---------------------------------------------------------------------------
# Errors: status hints + Retry-After
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_401_carries_the_auth_hint(plane: PlaneConnector) -> None:
    respx.post(f"{WS}/projects/{PROJECT_ID}/work-items/").mock(
        return_value=httpx.Response(401, text='{"detail": "Invalid API key"}'),
    )

    with pytest.raises(ValueError, match="authentication failed"):
        await plane.write(ConnectorPayload(resource="work_item", data={"project_id": PROJECT_ID, "name": "x"}))


@respx.mock
async def test_429_is_retried_after_retry_after(plane: PlaneConnector, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rate limit retries with the upstream's Retry-After honoured, then succeeds."""
    monkeypatch.setattr(plane_mod, "_retry_delay", lambda attempt, response: 0.0)
    route = respx.get(PROJECTS).mock(
        side_effect=[
            httpx.Response(429, text="rate_limited", headers={"Retry-After": "1"}),
            httpx.Response(200, json=_ENVELOPE),
        ],
    )

    result = await plane.query(ConnectorQuery(resource="projects"))

    assert result.records
    assert route.call_count == 2


@respx.mock
async def test_429_fails_loud_when_retries_are_exhausted(
    plane: PlaneConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plane_mod, "_retry_delay", lambda attempt, response: 0.0)
    respx.get(PROJECTS).mock(
        return_value=httpx.Response(429, text="rate_limited", headers={"Retry-After": "1"}),
    )

    with pytest.raises(ValueError, match="rate limited"):
        await plane.query(ConnectorQuery(resource="projects"))


@respx.mock
async def test_success_with_a_non_json_body_fails_loud(plane: PlaneConnector) -> None:
    respx.get(PROJECTS).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))

    with pytest.raises(ValueError, match="non-JSON"):
        await plane.query(ConnectorQuery(resource="projects"))


def test_retry_delay_prefers_retry_after_then_backoff() -> None:
    """The shared helper honours a positive Retry-After and clamps it."""
    with_retry = httpx.Response(429, headers={"Retry-After": "5"})
    assert plane_mod._retry_delay(0, with_retry) == 5.0
    assert plane_mod._retry_delay(0, httpx.Response(503)) == plane_mod._backoff_delay(0)
    assert plane_mod._retry_delay(0, None) == plane_mod._backoff_delay(0)
