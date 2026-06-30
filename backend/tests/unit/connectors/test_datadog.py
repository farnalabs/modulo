"""Unit tests for DatadogConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.datadog import DatadogConnector

API_KEY = "dummy_api_key"
APP_KEY = "dummy_app_key"
_BASE = "https://api.datadoghq.com"
_BASE_EU = "https://api.datadoghq.eu"


@pytest.fixture()
def connector():
    return DatadogConnector(api_key=API_KEY, app_key=APP_KEY, site="us")


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.DATADOG


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/api/v1/validate").mock(
        return_value=httpx.Response(200, json={"valid": True})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Datadog API key validated"


@respx.mock
async def test_health_check_invalid_key(connector):
    respx.get(f"{_BASE}/api/v1/validate").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid" in result.detail


@respx.mock
async def test_health_check_network_error(connector):
    respx.get(f"{_BASE}/api/v1/validate").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


@respx.mock
async def test_query_monitors(connector):
    monitors = [
        {"id": 1, "name": "CPU Load", "status": "Alert"},
        {"id": 2, "name": "Memory Usage", "status": "OK"},
    ]
    respx.get(f"{_BASE}/api/v1/monitor").mock(
        return_value=httpx.Response(200, json=monitors)
    )
    result = await connector.query(ConnectorQuery(resource="monitors"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "CPU Load"


@respx.mock
async def test_query_monitors_with_filters(connector):
    respx.get(
        f"{_BASE}/api/v1/monitor",
        params={"name": "CPU", "tags": "env:prod"},
    ).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 1, "name": "CPU Load", "tags": ["env:prod"]}],
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="monitors",
            filters={"name": "CPU", "tags": "env:prod"},
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["name"] == "CPU Load"


@respx.mock
async def test_query_events(connector):
    respx.get(f"{_BASE}/api/v1/events").mock(
        return_value=httpx.Response(
            200, json={"events": [{"id": "e1", "title": "Deploy", "text": "v2 deployed"}]}
        )
    )
    result = await connector.query(ConnectorQuery(resource="events"))
    assert len(result.records) == 1
    assert result.records[0]["title"] == "Deploy"


@respx.mock
async def test_query_events_with_filters(connector):
    respx.get(
        f"{_BASE}/api/v1/events",
        params={"start": "1700000000", "end": "1700001000", "priority": "normal"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"events": [{"id": "e2", "title": "Event", "priority": "normal"}]},
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="events",
            filters={"start": "1700000000", "end": "1700001000", "priority": "normal"},
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_metrics(connector):
    respx.post(f"{_BASE}/api/v2/query/timeseries").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "m1", "attributes": {"metric": "cpu"}}]},
        )
    )
    result = await connector.query(ConnectorQuery(resource="metrics"))
    assert len(result.records) == 1
    assert result.records[0]["id"] == "m1"


@respx.mock
async def test_query_dashboards(connector):
    respx.get(f"{_BASE}/api/v2/dashboards").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "d1", "attributes": {"title": "System Dashboard"}}]},
        )
    )
    result = await connector.query(ConnectorQuery(resource="dashboards"))
    assert len(result.records) == 1
    assert result.records[0]["attributes"]["title"] == "System Dashboard"


@respx.mock
async def test_query_dashboards_with_filters(connector):
    respx.get(
        f"{_BASE}/api/v2/dashboards",
        params={"filter": "system"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "d2", "attributes": {"title": "System Overview"}}]},
        )
    )
    result = await connector.query(
        ConnectorQuery(resource="dashboards", filters={"filter": "system"})
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_logs(connector):
    respx.post(f"{_BASE}/api/v2/logs/events/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "log1", "attributes": {"message": "error"}}],
                "meta": {"page": {"after": "cursor123"}},
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="logs"))
    assert len(result.records) == 1
    assert result.records[0]["id"] == "log1"
    assert result.next_cursor == "cursor123"


@respx.mock
async def test_query_logs_with_filter(connector):
    respx.post(f"{_BASE}/api/v2/logs/events/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "log2", "attributes": {"message": "deploy"}}],
                "meta": {"page": {"after": "cursor456"}},
            },
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="logs",
            filters={"filter": {"query": "service:web"}, "sort": "-timestamp"},
        )
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_logs_with_cursor(connector):
    respx.post(f"{_BASE}/api/v2/logs/events/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "log3", "attributes": {"message": "next page"}}],
                "meta": {"page": {"after": "cursor789"}},
            },
        )
    )
    result = await connector.query(
        ConnectorQuery(resource="logs", cursor="cursor123")
    )
    assert len(result.records) == 1
    assert result.next_cursor == "cursor789"


@respx.mock
async def test_write_event(connector):
    respx.post(f"{_BASE}/api/v1/events").mock(
        return_value=httpx.Response(
            202,
            json={"event": {"id": "evt1", "title": "Deploy complete"}},
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="event",
            data={"title": "Deploy complete", "text": "v2.1.0 deployed"},
        )
    )
    assert result["id"] == "evt1"
    assert result["title"] == "Deploy complete"


@respx.mock
async def test_write_event_with_optional_fields(connector):
    respx.post(f"{_BASE}/api/v1/events").mock(
        return_value=httpx.Response(
            202,
            json={"event": {"id": "evt2", "title": "Alert", "priority": "normal"}},
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="event",
            data={
                "title": "Alert",
                "text": "CPU > 90%",
                "priority": "normal",
                "alert_type": "warning",
                "host": "web-01",
            },
        )
    )
    assert result["id"] == "evt2"


@respx.mock
async def test_write_event_missing_title(connector):
    with pytest.raises(ValueError, match="Datadog event write requires"):
        await connector.write(
            ConnectorPayload(resource="event", data={"text": "v2 deployed"})
        )


@respx.mock
async def test_write_monitor(connector):
    respx.post(f"{_BASE}/api/v1/monitor").mock(
        return_value=httpx.Response(
            200, json={"id": 42, "name": "Test Monitor", "type": "metric alert"}
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="monitor",
            data={"query": "avg(last_5m):cpu > 90", "type": "metric alert"},
        )
    )
    assert result["id"] == 42
    assert result["type"] == "metric alert"


@respx.mock
async def test_write_monitor_missing_query(connector):
    with pytest.raises(ValueError, match="Datadog monitor write requires"):
        await connector.write(
            ConnectorPayload(resource="monitor", data={"type": "metric alert"})
        )


@respx.mock
async def test_write_monitor_status(connector):
    respx.put(f"{_BASE}/api/v1/monitor/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "status": "Muted"})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="monitor_status",
            data={"monitor_id": 42, "status": "Muted"},
        )
    )
    assert result["status"] == "Muted"


@respx.mock
async def test_write_monitor_status_missing_id(connector):
    with pytest.raises(ValueError, match="Datadog monitor_status write requires"):
        await connector.write(
            ConnectorPayload(resource="monitor_status", data={"status": "Muted"})
        )


async def test_query_invalid_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Datadog resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


async def test_write_invalid_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Datadog write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


def test_constructor_us_site():
    c = DatadogConnector(api_key="key", app_key="app", site="us")
    assert c._base == "https://api.datadoghq.com"


def test_constructor_eu_site():
    c = DatadogConnector(api_key="key", app_key="app", site="eu")
    assert c._base == "https://api.datadoghq.eu"


def test_constructor_us3_site():
    c = DatadogConnector(api_key="key", app_key="app", site="us3")
    assert c._base == "https://api.us3.datadoghq.com"


def test_constructor_unknown_site():
    with pytest.raises(ValueError, match="Unknown Datadog site"):
        DatadogConnector(api_key="key", app_key="app", site="invalid")


@respx.mock
async def test_query_http_403(connector):
    respx.get(f"{_BASE}/api/v1/monitor").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="monitors"))


@respx.mock
async def test_query_http_500(connector):
    respx.get(f"{_BASE}/api/v1/monitor").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="monitors"))
