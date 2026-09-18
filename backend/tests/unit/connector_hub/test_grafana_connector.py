"""Unit tests for GrafanaConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.grafana import GrafanaConnector

TOKEN = "grafana_test_token"
_BASE = "http://localhost:3000"


@pytest.fixture
def connector():
    return GrafanaConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.GRAFANA


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/api/health").mock(return_value=httpx.Response(200, json={"database": "ok"}))
    result = await connector.health_check()
    assert result.ok is True
    assert "healthy" in result.detail


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/api/health").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Grafana API token" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/api/health").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/api/health").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False


# ---------------------------------------------------------------------------
# query — dashboards
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_dashboards(connector):
    body = [{"id": 1, "title": "Overview", "type": "dash-db"}]
    respx.get(f"{_BASE}/api/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="dashboards"))
    assert result.total == 1
    assert result.records[0]["title"] == "Overview"


# ---------------------------------------------------------------------------
# query — dashboard
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_dashboard(connector):
    body = {"dashboard": {"uid": "abc", "title": "Overview"}}
    respx.get(f"{_BASE}/api/dashboards/uid/abc").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="dashboard", filters={"uid": "abc"}))
    assert len(result.records) == 1
    assert result.records[0]["dashboard"]["uid"] == "abc"


async def test_query_dashboard_missing_uid(connector):
    with pytest.raises(ValueError, match="'uid' in filters"):
        await connector.query(ConnectorQuery(resource="dashboard"))


# ---------------------------------------------------------------------------
# query — alerts / alert_rules / datasources / folders / organizations / users / annotations
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_alerts(connector):
    body = [{"id": 1, "state": "alerting"}]
    respx.get(f"{_BASE}/api/alerts").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert result.total == 1


@respx.mock
async def test_query_alert_rules(connector):
    body = [{"uid": "rule1", "title": "High CPU"}]
    respx.get(f"{_BASE}/api/v1/provisioning/alert-rules").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="alert_rules"))
    assert result.total == 1


@respx.mock
async def test_query_datasources(connector):
    body = [{"id": 1, "name": "Prometheus"}]
    respx.get(f"{_BASE}/api/datasources").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="datasources"))
    assert result.total == 1


@respx.mock
async def test_query_folders(connector):
    body = [{"id": 1, "title": "Dashboards"}]
    respx.get(f"{_BASE}/api/folders").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="folders"))
    assert result.total == 1


@respx.mock
async def test_query_organizations(connector):
    body = [{"id": 1, "name": "Acme"}]
    respx.get(f"{_BASE}/api/orgs").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="organizations"))
    assert result.total == 1


@respx.mock
async def test_query_users(connector):
    body = [{"id": 1, "login": "alice"}]
    respx.get(f"{_BASE}/api/users").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="users"))
    assert result.total == 1


@respx.mock
async def test_query_annotations(connector):
    body = [{"id": 1, "text": "deploy"}]
    respx.get(f"{_BASE}/api/annotations").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="annotations"))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Grafana resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — annotation
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_annotation(connector):
    created = {"id": 5, "text": "deploy complete"}
    respx.post(f"{_BASE}/api/annotations").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(resource="annotation", data={"text": "deploy complete"}),
    )
    assert result["id"] == 5


async def test_write_annotation_missing_text(connector):
    with pytest.raises(ValueError, match="'text' in data"):
        await connector.write(ConnectorPayload(resource="annotation", data={}))


# ---------------------------------------------------------------------------
# write — dashboard
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_dashboard(connector):
    created = {"id": 7, "uid": "xyz", "status": "success"}
    respx.post(f"{_BASE}/api/dashboards/db").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="dashboard",
            data={"dashboard": {"title": "New", "uid": "xyz"}, "overwrite": True},
        ),
    )
    assert result["uid"] == "xyz"


async def test_write_dashboard_missing_dashboard(connector):
    with pytest.raises(ValueError, match="'dashboard' in data"):
        await connector.write(ConnectorPayload(resource="dashboard", data={}))


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Grafana write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/api/search").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="dashboards"))
