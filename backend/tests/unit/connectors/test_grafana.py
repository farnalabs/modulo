"""Unit tests for GrafanaConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.grafana import GrafanaConnector

TOKEN = "glc_test_token"
_BASE = "http://localhost:3000"


@pytest.fixture()
def connector() -> GrafanaConnector:
    return GrafanaConnector(token=TOKEN)


def test_connector_type(connector: GrafanaConnector) -> None:
    assert connector.connector_type == ConnectorType.GRAFANA


def test_constructor_default_base_url() -> None:
    c = GrafanaConnector(token=TOKEN)
    assert c._base_url == "http://localhost:3000"


def test_constructor_custom_base_url() -> None:
    c = GrafanaConnector(token=TOKEN, base_url="https://grafana.example.com/")
    assert c._base_url == "https://grafana.example.com"


@respx.mock
async def test_health_check_ok(connector: GrafanaConnector) -> None:
    respx.get(f"{_BASE}/api/health").mock(
        return_value=httpx.Response(200, json={"commit": "abc123"})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Grafana API healthy"


@respx.mock
async def test_health_check_invalid_token(connector: GrafanaConnector) -> None:
    respx.get(f"{_BASE}/api/health").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid" in result.detail
    assert "token" in result.detail


@respx.mock
async def test_health_check_forbidden(connector: GrafanaConnector) -> None:
    respx.get(f"{_BASE}/api/health").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid" in result.detail


@respx.mock
async def test_health_check_network_error(connector: GrafanaConnector) -> None:
    respx.get(f"{_BASE}/api/health").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


@respx.mock
async def test_health_check_other_status(connector: GrafanaConnector) -> None:
    respx.get(f"{_BASE}/api/health").mock(
        return_value=httpx.Response(503, text="Service Unavailable")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "503" in result.detail


@respx.mock
async def test_query_dashboards(connector: GrafanaConnector) -> None:
    dashboards = [
        {"uid": "d1", "title": "System Dashboard", "type": "dash-db"},
        {"uid": "d2", "title": "API Monitoring", "type": "dash-db"},
    ]
    respx.get(f"{_BASE}/api/search", params={"type": "dash-db"}).mock(
        return_value=httpx.Response(200, json=dashboards)
    )
    result = await connector.query(ConnectorQuery(resource="dashboards"))
    assert len(result.records) == 2
    assert result.records[0]["title"] == "System Dashboard"


@respx.mock
async def test_query_dashboards_with_filters(connector: GrafanaConnector) -> None:
    respx.get(
        f"{_BASE}/api/search",
        params={"type": "dash-db", "query": "system", "folderIds": "1", "tag": "prod"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"uid": "d3", "title": "System Overview"}],
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="dashboards",
            filters={"query": "system", "folderIds": "1", "tag": "prod"},
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["uid"] == "d3"


@respx.mock
async def test_query_dashboards_with_limit(connector: GrafanaConnector) -> None:
    respx.get(
        f"{_BASE}/api/search",
        params={"type": "dash-db", "limit": 1},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"uid": "d1", "title": "Only One"}],
        )
    )
    result = await connector.query(
        ConnectorQuery(resource="dashboards", limit=1)
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_dashboard_by_uid(connector: GrafanaConnector) -> None:
    dashboard = {
        "dashboard": {"uid": "abc123", "title": "My Dashboard"},
        "meta": {"slug": "my-dashboard"},
    }
    respx.get(f"{_BASE}/api/dashboards/uid/abc123").mock(
        return_value=httpx.Response(200, json=dashboard)
    )
    result = await connector.query(
        ConnectorQuery(resource="dashboard", filters={"uid": "abc123"})
    )
    assert len(result.records) == 1
    assert result.records[0]["dashboard"]["uid"] == "abc123"
    assert result.records[0]["meta"]["slug"] == "my-dashboard"


@respx.mock
async def test_query_dashboard_missing_uid(connector: GrafanaConnector) -> None:
    with pytest.raises(ValueError, match="Grafana dashboard query requires 'uid' in filters"):
        await connector.query(ConnectorQuery(resource="dashboard"))


@respx.mock
async def test_query_alerts(connector: GrafanaConnector) -> None:
    alerts = [
        {"id": 1, "name": "CPU High", "state": "alerting"},
        {"id": 2, "name": "Disk Full", "state": "ok"},
    ]
    respx.get(f"{_BASE}/api/alerts").mock(
        return_value=httpx.Response(200, json=alerts)
    )
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "CPU High"


@respx.mock
async def test_query_alerts_with_filters(connector: GrafanaConnector) -> None:
    respx.get(
        f"{_BASE}/api/alerts",
        params={"state": "alerting", "folderIds": "1", "limit": 10, "query": "cpu"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 3, "name": "CPU Alert", "state": "alerting"}],
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="alerts",
            filters={"state": "alerting", "folderIds": "1", "query": "cpu"},
            limit=10,
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_alert_rules(connector: GrafanaConnector) -> None:
    rules = [
        {"uid": "r1", "name": "High CPU Rule"},
        {"uid": "r2", "name": "Memory Rule"},
    ]
    respx.get(f"{_BASE}/api/v1/provisioning/alert-rules").mock(
        return_value=httpx.Response(200, json=rules)
    )
    result = await connector.query(ConnectorQuery(resource="alert_rules"))
    assert len(result.records) == 2
    assert result.records[0]["uid"] == "r1"


@respx.mock
async def test_query_datasources(connector: GrafanaConnector) -> None:
    datasources = [
        {"id": 1, "name": "Prometheus", "type": "prometheus"},
        {"id": 2, "name": "Loki", "type": "loki"},
    ]
    respx.get(f"{_BASE}/api/datasources").mock(
        return_value=httpx.Response(200, json=datasources)
    )
    result = await connector.query(ConnectorQuery(resource="datasources"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Prometheus"


@respx.mock
async def test_query_folders(connector: GrafanaConnector) -> None:
    folders = [
        {"uid": "f1", "title": "Infrastructure"},
        {"uid": "f2", "title": "Applications"},
    ]
    respx.get(f"{_BASE}/api/folders").mock(
        return_value=httpx.Response(200, json=folders)
    )
    result = await connector.query(ConnectorQuery(resource="folders"))
    assert len(result.records) == 2
    assert result.records[0]["title"] == "Infrastructure"


@respx.mock
async def test_query_folders_with_limit(connector: GrafanaConnector) -> None:
    respx.get(
        f"{_BASE}/api/folders",
        params={"limit": 1},
    ).mock(
        return_value=httpx.Response(200, json=[{"uid": "f1", "title": "Infrastructure"}])
    )
    result = await connector.query(
        ConnectorQuery(resource="folders", limit=1)
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_organizations(connector: GrafanaConnector) -> None:
    orgs = [
        {"id": 1, "name": "Main Org"},
        {"id": 2, "name": "Dev Org"},
    ]
    respx.get(f"{_BASE}/api/orgs").mock(
        return_value=httpx.Response(200, json=orgs)
    )
    result = await connector.query(ConnectorQuery(resource="organizations"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Main Org"


@respx.mock
async def test_query_users(connector: GrafanaConnector) -> None:
    users = [
        {"id": 1, "login": "admin", "email": "admin@example.com"},
        {"id": 2, "login": "dev", "email": "dev@example.com"},
    ]
    respx.get(f"{_BASE}/api/users").mock(
        return_value=httpx.Response(200, json=users)
    )
    result = await connector.query(ConnectorQuery(resource="users"))
    assert len(result.records) == 2
    assert result.records[0]["login"] == "admin"


@respx.mock
async def test_query_users_with_permission_filter(connector: GrafanaConnector) -> None:
    respx.get(
        f"{_BASE}/api/users",
        params={"permission": "Admin"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 1, "login": "admin", "permission": "Admin"}],
        )
    )
    result = await connector.query(
        ConnectorQuery(resource="users", filters={"permission": "Admin"})
    )
    assert len(result.records) == 1
    assert result.records[0]["permission"] == "Admin"


@respx.mock
async def test_query_annotations(connector: GrafanaConnector) -> None:
    annotations = [
        {"id": 1, "text": "Deploy v2.0", "type": "alert"},
        {"id": 2, "text": "Scaling event", "type": "alert"},
    ]
    respx.get(f"{_BASE}/api/annotations", params={"type": "alert"}).mock(
        return_value=httpx.Response(200, json=annotations)
    )
    result = await connector.query(ConnectorQuery(resource="annotations"))
    assert len(result.records) == 2
    assert result.records[0]["text"] == "Deploy v2.0"


@respx.mock
async def test_query_annotations_with_filters(connector: GrafanaConnector) -> None:
    respx.get(
        f"{_BASE}/api/annotations",
        params={"type": "alert", "from": "1700000000000", "to": "1700001000000", "limit": 5},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 3, "text": "Filtered annotation"}],
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="annotations",
            filters={"from": "1700000000000", "to": "1700001000000", "limit": 5},
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_write_annotation(connector: GrafanaConnector) -> None:
    respx.post(f"{_BASE}/api/annotations").mock(
        return_value=httpx.Response(200, json={"id": 42, "text": "Deploy complete"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="annotation",
            data={"text": "Deploy complete"},
        )
    )
    assert result["id"] == 42
    assert result["text"] == "Deploy complete"


@respx.mock
async def test_write_annotation_with_tags_and_time(connector: GrafanaConnector) -> None:
    respx.post(f"{_BASE}/api/annotations").mock(
        return_value=httpx.Response(
            200,
            json={"id": 43, "text": "Deploy v2.1", "tags": ["deploy", "prod"], "time": 1700000000000},
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="annotation",
            data={
                "text": "Deploy v2.1",
                "tags": ["deploy", "prod"],
                "time": 1700000000000,
                "dashboardId": 1,
                "panelId": 2,
                "timeEnd": 1700001000000,
            },
        )
    )
    assert result["id"] == 43
    assert "deploy" in result["tags"]


@respx.mock
async def test_write_annotation_missing_text(connector: GrafanaConnector) -> None:
    with pytest.raises(ValueError, match="Grafana annotation write requires 'text' in data"):
        await connector.write(
            ConnectorPayload(resource="annotation", data={"tags": ["deploy"]})
        )


@respx.mock
async def test_write_dashboard(connector: GrafanaConnector) -> None:
    dashboard_json = {"title": "New Dashboard", "panels": []}
    respx.post(f"{_BASE}/api/dashboards/db").mock(
        return_value=httpx.Response(
            200,
            json={"uid": "new123", "title": "New Dashboard", "status": "success"},
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="dashboard",
            data={"dashboard": dashboard_json},
        )
    )
    assert result["uid"] == "new123"
    assert result["status"] == "success"


@respx.mock
async def test_write_dashboard_with_optional_fields(connector: GrafanaConnector) -> None:
    dashboard_json = {"title": "Updated Dashboard", "panels": []}
    respx.post(f"{_BASE}/api/dashboards/db").mock(
        return_value=httpx.Response(
            200,
            json={"uid": "upd456", "title": "Updated Dashboard", "status": "success"},
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="dashboard",
            data={
                "dashboard": dashboard_json,
                "overwrite": True,
                "folderId": 1,
                "folderUid": "folder1",
                "message": "Updated via API",
            },
        )
    )
    assert result["uid"] == "upd456"


@respx.mock
async def test_write_dashboard_missing_dashboard(connector: GrafanaConnector) -> None:
    with pytest.raises(ValueError, match="Grafana dashboard write requires 'dashboard' in data"):
        await connector.write(
            ConnectorPayload(resource="dashboard", data={"overwrite": True})
        )


async def test_query_invalid_resource(connector: GrafanaConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Grafana resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


async def test_write_invalid_resource(connector: GrafanaConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Grafana write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


@respx.mock
async def test_query_http_401(connector: GrafanaConnector) -> None:
    respx.get(f"{_BASE}/api/search", params={"type": "dash-db"}).mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="dashboards"))


@respx.mock
async def test_query_http_500(connector: GrafanaConnector) -> None:
    respx.get(f"{_BASE}/api/search", params={"type": "dash-db"}).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="dashboards"))


@respx.mock
async def test_write_http_401(connector: GrafanaConnector) -> None:
    respx.post(f"{_BASE}/api/annotations").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(resource="annotation", data={"text": "test"})
        )


@respx.mock
async def test_write_http_500(connector: GrafanaConnector) -> None:
    respx.post(f"{_BASE}/api/dashboards/db").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(resource="dashboard", data={"dashboard": {"title": "test"}})
        )
