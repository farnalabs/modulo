"""Unit tests for DatadogConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.datadog import DatadogConnector

API_KEY = "dd_api_key"
APP_KEY = "dd_app_key"
_BASE = "https://api.datadoghq.com"


@pytest.fixture
def connector():
    return DatadogConnector(api_key=API_KEY, app_key=APP_KEY)


# ---------------------------------------------------------------------------
# constructor / connector_type
# ---------------------------------------------------------------------------


def test_unknown_site_raises():
    with pytest.raises(ValueError, match="Unknown Datadog site"):
        DatadogConnector(api_key=API_KEY, app_key=APP_KEY, site="nope")


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.DATADOG


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/api/v1/validate").mock(return_value=httpx.Response(200, json={"valid": True}))
    result = await connector.health_check()
    assert result.ok is True
    assert "validated" in result.detail


@respx.mock
async def test_health_check_invalid_key(connector):
    respx.get(f"{_BASE}/api/v1/validate").mock(return_value=httpx.Response(403, text="Forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Datadog API key" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/api/v1/validate").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/api/v1/validate").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False


# ---------------------------------------------------------------------------
# query — monitors
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_monitors(connector):
    body = [{"id": 1, "name": "CPU high"}]
    respx.get(f"{_BASE}/api/v1/monitor").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="monitors"))
    assert result.total == 1
    assert result.records[0]["id"] == 1


# ---------------------------------------------------------------------------
# query — events
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_events(connector):
    body = {"events": [{"id": 1, "title": "deploy"}]}
    respx.get(f"{_BASE}/api/v1/events").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="events"))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — metrics
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_metrics(connector):
    body = {"data": [{"data": {"attributes": {"timeseries": []}}}]}
    respx.post(f"{_BASE}/api/v2/query/timeseries").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="metrics", filters={"formulas": [{"formula": "a"}], "queries": []}),
    )
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — dashboards
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_dashboards(connector):
    body = {"data": [{"id": "abc", "attributes": {"title": "Ops"}}]}
    respx.get(f"{_BASE}/api/v2/dashboards").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="dashboards"))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — logs
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_logs(connector):
    body = {"data": [{"id": "log1"}], "meta": {"page": {"after": "cursor1"}}}
    respx.post(f"{_BASE}/api/v2/logs/events/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="logs", cursor="prev"))
    assert len(result.records) == 1
    assert result.next_cursor == "cursor1"


@respx.mock
async def test_query_logs_no_next_cursor(connector):
    body = {"data": [{"id": "log1"}], "meta": {"page": {}}}
    respx.post(f"{_BASE}/api/v2/logs/events/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="logs"))
    assert result.next_cursor is None


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Datadog resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — event
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_event(connector):
    created = {"event": {"id": 1, "title": "Deploy"}}
    respx.post(f"{_BASE}/api/v1/events").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(resource="event", data={"title": "Deploy", "text": "done"}),
    )
    assert result["id"] == 1


async def test_write_event_missing_fields(connector):
    with pytest.raises(ValueError, match="'title' and 'text' in data"):
        await connector.write(ConnectorPayload(resource="event", data={"title": "Deploy"}))


# ---------------------------------------------------------------------------
# write — monitor
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_monitor(connector):
    created = {"id": 2, "name": "CPU high", "type": "metric alert"}
    respx.post(f"{_BASE}/api/v1/monitor").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="monitor",
            data={"query": "avg(last_5m):avg:system.cpu.user > 90", "type": "metric alert"},
        ),
    )
    assert result["id"] == 2


async def test_write_monitor_missing_fields(connector):
    with pytest.raises(ValueError, match="'query' and 'type' in data"):
        await connector.write(ConnectorPayload(resource="monitor", data={"query": "avg(...)"}))


# ---------------------------------------------------------------------------
# write — monitor_status
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_monitor_status(connector):
    updated = {"id": 2, "status": "Muted"}
    respx.put(f"{_BASE}/api/v1/monitor/2").mock(return_value=httpx.Response(200, json=updated))
    result = await connector.write(
        ConnectorPayload(resource="monitor_status", data={"monitor_id": 2, "status": "Muted"}),
    )
    assert result["id"] == 2


async def test_write_monitor_status_missing_id(connector):
    with pytest.raises(ValueError, match="'monitor_id' in data"):
        await connector.write(ConnectorPayload(resource="monitor_status", data={}))


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Datadog write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/api/v1/monitor").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="monitors"))
