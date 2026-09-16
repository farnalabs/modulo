"""Step definitions for the SharePoint connector BDD feature.

Wires ``features/connectors/sharepoint.feature`` into the executing suite (the
improve-architecture product-map walk) by driving the REAL
``SharePointConnector`` against a respx-mocked Microsoft Graph API v1.0 —
mirroring ``backend/tests/unit/connectors/test_sharepoint.py`` so the executing
BDD surface locks the same contract the unit suite does: token validation via
``/sites/root`` (200 => healthy reporting the site root name, 401 => unhealthy),
listing sites / list items, reading a file, creating a list item, and failing
closed on an unsupported resource.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/sharepoint.feature")

_BASE = "https://graph.microsoft.com/v1.0"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the SharePoint connector scenarios."""
    return {}


@given("a SharePoint connector configured with valid token")
def sharepoint_connector_valid(ctx: dict) -> None:
    from modulo.connectors.sharepoint import SharePointConnector

    ctx["connector"] = SharePointConnector(token="test_sharepoint_token")
    ctx["valid"] = True


@given("a SharePoint connector configured with invalid credentials")
def sharepoint_connector_invalid(ctx: dict) -> None:
    from modulo.connectors.sharepoint import SharePointConnector

    ctx["connector"] = SharePointConnector(token="bad_token")
    ctx["valid"] = False


@when("the connector checks health")
def sharepoint_health_check(ctx: dict) -> None:
    response = (
        httpx.Response(200, json={"id": "root", "displayName": "Contoso Portal"})
        if ctx["valid"]
        else httpx.Response(401, text="Unauthorized")
    )
    with respx.mock:
        respx.get(f"{_BASE}/sites/root").mock(return_value=response)
        ctx["health_result"] = asyncio.run(ctx["connector"].health_check())


@when("the connector lists sites")
def sharepoint_query_sites(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    sites = {
        "value": [
            {"id": "site1", "displayName": "Site A"},
            {"id": "site2", "displayName": "Site B"},
        ]
    }
    with respx.mock:
        respx.get(f"{_BASE}/sites").mock(return_value=httpx.Response(200, json=sites))
        ctx["query_result"] = asyncio.run(ctx["connector"].query(ConnectorQuery(resource="sites")))


@when(parsers.parse('the connector lists items in list "{list_id}" of site "{site_id}"'))
def sharepoint_query_list_items(list_id: str, site_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    items = {
        "value": [
            {"id": "item1", "fields": {"Title": "Task 1"}},
            {"id": "item2", "fields": {"Title": "Task 2"}},
        ]
    }
    with respx.mock:
        respx.get(f"{_BASE}/sites/{site_id}/lists/{list_id}/items").mock(
            return_value=httpx.Response(200, json=items)
        )
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(
                ConnectorQuery(resource="list_items", filters={"site_id": site_id, "list_id": list_id})
            )
        )


@when(
    parsers.parse(
        'the connector creates a list item in list "{list_id}" of site "{site_id}" with fields Title "{title}"'
    )
)
def sharepoint_write_list_item(list_id: str, site_id: str, title: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    created = {"id": "new-item-1", "fields": {"Title": title}}
    with respx.mock:
        respx.post(f"{_BASE}/sites/{site_id}/lists/{list_id}/items").mock(
            return_value=httpx.Response(201, json=created)
        )
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(
                ConnectorPayload(
                    resource="list_item",
                    data={"site_id": site_id, "list_id": list_id, "fields": {"Title": title}},
                )
            )
        )


@when(parsers.parse('the connector reads file "{path}" from site "{site_id}" drive "{drive_id}"'))
def sharepoint_query_file(path: str, site_id: str, drive_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    content = "Hello, SharePoint!"
    with respx.mock:
        respx.get(f"{_BASE}/sites/{site_id}/drives/{drive_id}/root:/{path.strip('/')}:/content").mock(
            return_value=httpx.Response(200, text=content)
        )
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(
                ConnectorQuery(
                    resource="file",
                    filters={"site_id": site_id, "drive_id": drive_id, "path": path},
                )
            )
        )


@then("the health check reports the site root name")
def sharepoint_health_reports_root(ctx: dict) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    assert result.ok is True, f"Expected healthy, got: {result.detail}"
    assert "Contoso Portal" in result.detail, f"Expected site root name, got: {result.detail}"


@then(parsers.parse('the health check returns "{status}"'))
def sharepoint_health_result(status: str, ctx: dict) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    if status == "healthy":
        assert result.ok is True, f"Expected healthy, got: {result.detail}"
    else:
        assert result.ok is False, f"Expected unhealthy, got: {result.detail}"


@then("the result contains SharePoint sites")
def sharepoint_result_sites(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected site records"
    assert result.records[0]["displayName"] == "Site A", result.records
    assert result.records[1]["displayName"] == "Site B", result.records


@then("the result contains list items")
def sharepoint_result_list_items(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected list item records"
    assert result.records[0]["fields"]["Title"] == "Task 1", result.records
    assert result.records[1]["fields"]["Title"] == "Task 2", result.records


@then("the list item is created successfully")
def sharepoint_list_item_created(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["id"] == "new-item-1", result
    assert result["fields"]["Title"] == "New Task", result


@then("the connector returns the file content")
def sharepoint_result_file(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected file record"
    assert result.records[0]["content"] == "Hello, SharePoint!", result.records
    assert result.records[0]["path"] == "/documents/report.docx", result.records
