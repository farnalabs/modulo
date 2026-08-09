"""Unit tests for PagerDutyConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.pagerduty import PagerDutyConnector

TOKEN = "pd_test_token"
_BASE = "https://api.pagerduty.com"


@pytest.fixture
def connector():
    return PagerDutyConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.PAGERDUTY


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json={"users": []}))
    result = await connector.health_check()
    assert result.ok is True
    assert "validated" in result.detail


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid PagerDuty API token" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/users").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False


# ---------------------------------------------------------------------------
# query — incidents
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_incidents(connector):
    body = {"incidents": [{"id": "P1", "title": "Outage"}], "total": 1}
    respx.get(f"{_BASE}/incidents").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="incidents"))
    assert result.total == 1
    assert result.records[0]["id"] == "P1"


@respx.mock
async def test_query_incidents_pagination(connector):
    body = {"incidents": [{"id": "P1"}], "total": 2, "more": True}
    respx.get(f"{_BASE}/incidents").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="incidents"))
    assert result.next_cursor == "1"


# ---------------------------------------------------------------------------
# query — services / teams / users / escalation_policies / schedules / on_calls
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_services(connector):
    body = {"services": [{"id": "S1", "name": "API"}]}
    respx.get(f"{_BASE}/services").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="services"))
    assert len(result.records) == 1


@respx.mock
async def test_query_teams(connector):
    body = {"teams": [{"id": "T1", "name": "Core"}]}
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert len(result.records) == 1


@respx.mock
async def test_query_users(connector):
    body = {"users": [{"id": "U1", "name": "Alice"}]}
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="users"))
    assert len(result.records) == 1


@respx.mock
async def test_query_escalation_policies(connector):
    body = {"escalation_policies": [{"id": "E1"}]}
    respx.get(f"{_BASE}/escalation_policies").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="escalation_policies"))
    assert len(result.records) == 1


@respx.mock
async def test_query_schedules(connector):
    body = {"schedules": [{"id": "SC1"}]}
    respx.get(f"{_BASE}/schedules").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="schedules"))
    assert len(result.records) == 1


@respx.mock
async def test_query_on_calls(connector):
    body = {"oncalls": [{"user": {"id": "U1"}}]}
    respx.get(f"{_BASE}/oncalls").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="on_calls"))
    assert len(result.records) == 1


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported PagerDuty resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — incident
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_incident(connector):
    created = {"incident": {"id": "P10", "title": "Disk full"}}
    respx.post(f"{_BASE}/incidents").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(resource="incident", data={"title": "Disk full", "service_id": "S1"}),
    )
    assert result["id"] == "P10"


async def test_write_incident_missing_fields(connector):
    with pytest.raises(ValueError, match="'title' and 'service_id' in data"):
        await connector.write(ConnectorPayload(resource="incident", data={"title": "Disk full"}))


# ---------------------------------------------------------------------------
# write — incident_acknowledge / incident_resolve / note
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_incident_acknowledge(connector):
    updated = {"incident": {"id": "P10", "status": "acknowledged"}}
    respx.put(f"{_BASE}/incidents/P10").mock(return_value=httpx.Response(200, json=updated))
    result = await connector.write(
        ConnectorPayload(resource="incident_acknowledge", data={"incident_id": "P10"}),
    )
    assert result["status"] == "acknowledged"


async def test_write_incident_acknowledge_missing_id(connector):
    with pytest.raises(ValueError, match="'incident_id' in data"):
        await connector.write(ConnectorPayload(resource="incident_acknowledge", data={}))


@respx.mock
async def test_write_incident_resolve(connector):
    updated = {"incident": {"id": "P10", "status": "resolved"}}
    respx.put(f"{_BASE}/incidents/P10").mock(return_value=httpx.Response(200, json=updated))
    result = await connector.write(
        ConnectorPayload(resource="incident_resolve", data={"incident_id": "P10"}),
    )
    assert result["status"] == "resolved"


@respx.mock
async def test_write_note(connector):
    created = {"note": {"id": "N1", "content": "investigating"}}
    respx.post(f"{_BASE}/incidents/P10/notes").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(resource="note", data={"incident_id": "P10", "content": "investigating"}),
    )
    assert result["id"] == "N1"


async def test_write_note_missing_fields(connector):
    with pytest.raises(ValueError, match="'incident_id' and 'content' in data"):
        await connector.write(ConnectorPayload(resource="note", data={"incident_id": "P10"}))


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported PagerDuty write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/incidents").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="incidents"))
