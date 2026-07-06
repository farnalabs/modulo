"""Unit tests for PagerDutyConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.pagerduty import PagerDutyConnector

TOKEN = "pd_test_token"
_BASE = "https://api.pagerduty.com"


@pytest.fixture()
def connector() -> PagerDutyConnector:
    return PagerDutyConnector(token=TOKEN)


def test_connector_type(connector: PagerDutyConnector) -> None:
    assert connector.connector_type == ConnectorType.PAGERDUTY


@respx.mock
async def test_health_check_ok(connector: PagerDutyConnector) -> None:
    respx.get(f"{_BASE}/users", params={"limit": 1}).mock(
        return_value=httpx.Response(200, json={"users": [{"id": "U1"}]})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "PagerDuty API token validated"


@respx.mock
async def test_health_check_invalid_token(connector: PagerDutyConnector) -> None:
    respx.get(f"{_BASE}/users", params={"limit": 1}).mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid PagerDuty API token" in result.detail


@respx.mock
async def test_health_check_network_error(connector: PagerDutyConnector) -> None:
    respx.get(f"{_BASE}/users", params={"limit": 1}).mock(side_effect=httpx.ConnectError("connection refused"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


@respx.mock
async def test_health_check_other_status(connector: PagerDutyConnector) -> None:
    respx.get(f"{_BASE}/users", params={"limit": 1}).mock(return_value=httpx.Response(429, text="Too Many Requests"))
    result = await connector.health_check()
    assert result.ok is False
    assert "429" in result.detail


@respx.mock
async def test_query_incidents(connector: PagerDutyConnector) -> None:
    incidents = [
        {"id": "I1", "title": "Production outage", "status": "triggered"},
        {"id": "I2", "title": "Degraded performance", "status": "acknowledged"},
    ]
    respx.get(f"{_BASE}/incidents").mock(
        return_value=httpx.Response(200, json={"incidents": incidents, "total": 2, "more": False})
    )
    result = await connector.query(ConnectorQuery(resource="incidents"))
    assert len(result.records) == 2
    assert result.records[0]["title"] == "Production outage"
    assert result.total == 2


@respx.mock
async def test_query_incidents_with_filters(connector: PagerDutyConnector) -> None:
    respx.get(
        f"{_BASE}/incidents",
        params={"statuses": "triggered", "team_ids": "TEAM1"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "incidents": [{"id": "I3", "title": "Critical alert", "status": "triggered"}],
                "total": 1,
                "more": False,
            },
        )
    )
    result = await connector.query(
        ConnectorQuery(
            resource="incidents",
            filters={"statuses": "triggered", "team_ids": "TEAM1"},
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["id"] == "I3"


@respx.mock
async def test_query_incidents_with_cursor(connector: PagerDutyConnector) -> None:
    respx.get(
        f"{_BASE}/incidents",
        params={"offset": 25},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "incidents": [{"id": "I25", "title": "Next page incident"}],
                "total": 50,
                "more": True,
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="incidents", cursor="25"))
    assert len(result.records) == 1
    assert result.next_cursor is not None


@respx.mock
async def test_query_incidents_with_limit(connector: PagerDutyConnector) -> None:
    respx.get(
        f"{_BASE}/incidents",
        params={"limit": 5},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "incidents": [{"id": f"I{i}", "title": f"Incident {i}"} for i in range(10)],
                "total": 10,
                "more": False,
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="incidents", limit=5))
    assert len(result.records) == 5


@respx.mock
async def test_query_services(connector: PagerDutyConnector) -> None:
    services = [
        {"id": "S1", "name": "Web API", "status": "active"},
        {"id": "S2", "name": "Database", "status": "active"},
    ]
    respx.get(f"{_BASE}/services").mock(
        return_value=httpx.Response(200, json={"services": services, "total": 2, "more": False})
    )
    result = await connector.query(ConnectorQuery(resource="services"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Web API"


@respx.mock
async def test_query_services_with_query_filter(connector: PagerDutyConnector) -> None:
    respx.get(
        f"{_BASE}/services",
        params={"query": "api"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"services": [{"id": "S3", "name": "API Gateway"}], "total": 1, "more": False},
        )
    )
    result = await connector.query(ConnectorQuery(resource="services", filters={"query": "api"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "API Gateway"


@respx.mock
async def test_query_users(connector: PagerDutyConnector) -> None:
    users = [
        {"id": "U1", "name": "Alice", "email": "alice@example.com"},
        {"id": "U2", "name": "Bob", "email": "bob@example.com"},
    ]
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json={"users": users, "total": 2, "more": False}))
    result = await connector.query(ConnectorQuery(resource="users"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Alice"


@respx.mock
async def test_query_teams(connector: PagerDutyConnector) -> None:
    teams = [
        {"id": "T1", "name": "Engineering"},
        {"id": "T2", "name": "Operations"},
    ]
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json={"teams": teams, "total": 2, "more": False}))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Engineering"


@respx.mock
async def test_query_escalation_policies(connector: PagerDutyConnector) -> None:
    policies = [
        {"id": "EP1", "name": "Critical Escalation"},
        {"id": "EP2", "name": "Standard Escalation"},
    ]
    respx.get(f"{_BASE}/escalation_policies").mock(
        return_value=httpx.Response(200, json={"escalation_policies": policies, "total": 2, "more": False})
    )
    result = await connector.query(ConnectorQuery(resource="escalation_policies"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Critical Escalation"


@respx.mock
async def test_query_schedules(connector: PagerDutyConnector) -> None:
    schedules = [
        {"id": "SCH1", "name": "Primary On-Call"},
    ]
    respx.get(f"{_BASE}/schedules").mock(
        return_value=httpx.Response(200, json={"schedules": schedules, "total": 1, "more": False})
    )
    result = await connector.query(ConnectorQuery(resource="schedules"))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Primary On-Call"


@respx.mock
async def test_query_on_calls(connector: PagerDutyConnector) -> None:
    oncalls = [
        {"user": {"id": "U1", "summary": "Alice"}, "schedule": {"id": "SCH1"}},
    ]
    respx.get(f"{_BASE}/oncalls").mock(
        return_value=httpx.Response(200, json={"oncalls": oncalls, "total": 1, "more": False})
    )
    result = await connector.query(ConnectorQuery(resource="on_calls"))
    assert len(result.records) == 1
    assert result.records[0]["user"]["id"] == "U1"


@respx.mock
async def test_write_incident_trigger(connector: PagerDutyConnector) -> None:
    respx.post(f"{_BASE}/incidents").mock(
        return_value=httpx.Response(
            201,
            json={
                "incident": {
                    "id": "INC1",
                    "title": "Test incident",
                    "status": "triggered",
                }
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="incident",
            data={"title": "Test incident", "service_id": "SVC1"},
        )
    )
    assert result["id"] == "INC1"
    assert result["status"] == "triggered"


@respx.mock
async def test_write_incident_with_all_fields(connector: PagerDutyConnector) -> None:
    respx.post(f"{_BASE}/incidents").mock(
        return_value=httpx.Response(
            201,
            json={
                "incident": {
                    "id": "INC2",
                    "title": "Full incident",
                    "urgency": "high",
                    "status": "triggered",
                }
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="incident",
            data={
                "title": "Full incident",
                "service_id": "SVC2",
                "urgency": "high",
                "body": "Disk space critical",
                "escalation_policy_id": "EP1",
                "priority_id": "P1",
            },
        )
    )
    assert result["id"] == "INC2"


@respx.mock
async def test_write_incident_missing_title(connector: PagerDutyConnector) -> None:
    with pytest.raises(ValueError, match="PagerDuty incident write requires"):
        await connector.write(
            ConnectorPayload(
                resource="incident",
                data={"service_id": "SVC1"},
            )
        )


@respx.mock
async def test_write_incident_missing_service_id(connector: PagerDutyConnector) -> None:
    with pytest.raises(ValueError, match="PagerDuty incident write requires"):
        await connector.write(
            ConnectorPayload(
                resource="incident",
                data={"title": "Test incident"},
            )
        )


@respx.mock
async def test_write_incident_acknowledge(connector: PagerDutyConnector) -> None:
    respx.put(f"{_BASE}/incidents/INC1").mock(
        return_value=httpx.Response(
            200,
            json={
                "incident": {
                    "id": "INC1",
                    "status": "acknowledged",
                }
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="incident_acknowledge",
            data={"incident_id": "INC1"},
        )
    )
    assert result["status"] == "acknowledged"


@respx.mock
async def test_write_incident_resolve(connector: PagerDutyConnector) -> None:
    respx.put(f"{_BASE}/incidents/INC1").mock(
        return_value=httpx.Response(
            200,
            json={
                "incident": {
                    "id": "INC1",
                    "status": "resolved",
                }
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="incident_resolve",
            data={"incident_id": "INC1"},
        )
    )
    assert result["status"] == "resolved"


@respx.mock
async def test_write_note(connector: PagerDutyConnector) -> None:
    respx.post(f"{_BASE}/incidents/INC1/notes").mock(
        return_value=httpx.Response(
            201,
            json={
                "note": {
                    "id": "N1",
                    "content": "Investigating root cause",
                }
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="note",
            data={"incident_id": "INC1", "content": "Investigating root cause"},
        )
    )
    assert result["id"] == "N1"
    assert result["content"] == "Investigating root cause"


@respx.mock
async def test_write_acknowledge_missing_incident_id(connector: PagerDutyConnector) -> None:
    with pytest.raises(ValueError, match="PagerDuty incident_acknowledge write requires 'incident_id'"):
        await connector.write(
            ConnectorPayload(
                resource="incident_acknowledge",
                data={},
            )
        )


@respx.mock
async def test_write_resolve_missing_incident_id(connector: PagerDutyConnector) -> None:
    with pytest.raises(ValueError, match="PagerDuty incident_resolve write requires 'incident_id'"):
        await connector.write(
            ConnectorPayload(
                resource="incident_resolve",
                data={},
            )
        )


@respx.mock
async def test_write_note_missing_incident_id(connector: PagerDutyConnector) -> None:
    with pytest.raises(ValueError, match="PagerDuty note write requires 'incident_id' and 'content'"):
        await connector.write(
            ConnectorPayload(
                resource="note",
                data={"content": "Some note"},
            )
        )


@respx.mock
async def test_write_note_missing_content(connector: PagerDutyConnector) -> None:
    with pytest.raises(ValueError, match="PagerDuty note write requires 'incident_id' and 'content'"):
        await connector.write(
            ConnectorPayload(
                resource="note",
                data={"incident_id": "INC1"},
            )
        )


async def test_query_invalid_resource(connector: PagerDutyConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported PagerDuty resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


async def test_write_invalid_resource(connector: PagerDutyConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported PagerDuty write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


@respx.mock
async def test_query_http_500(connector: PagerDutyConnector) -> None:
    respx.get(f"{_BASE}/incidents").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="incidents"))


@respx.mock
async def test_write_http_403(connector: PagerDutyConnector) -> None:
    respx.post(f"{_BASE}/incidents").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(
                resource="incident",
                data={"title": "Test", "service_id": "SVC1"},
            )
        )


@respx.mock
async def test_query_incidents_with_cursor_passthrough(connector: PagerDutyConnector) -> None:
    respx.get(
        f"{_BASE}/incidents",
        params={"offset": 100},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "incidents": [{"id": "I100", "title": "Hundredth incident"}],
                "total": 200,
                "more": True,
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="incidents", cursor="100"))
    assert len(result.records) == 1
    assert result.next_cursor == "101"


@respx.mock
async def test_query_on_calls_with_limit(connector: PagerDutyConnector) -> None:
    respx.get(
        f"{_BASE}/oncalls",
        params={"limit": 3},
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "oncalls": [{"user": {"id": f"U{i}"}} for i in range(5)],
                "total": 5,
                "more": False,
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="on_calls", limit=3))
    assert len(result.records) == 3


@respx.mock
async def test_incident_acknowledge_already_resolved(connector: PagerDutyConnector) -> None:
    respx.put(f"{_BASE}/incidents/INC1").mock(
        return_value=httpx.Response(
            200,
            json={
                "incident": {
                    "id": "INC1",
                    "status": "resolved",
                }
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="incident_acknowledge",
            data={"incident_id": "INC1"},
        )
    )
    assert result["status"] == "resolved"


@respx.mock
async def test_query_users_with_team_filter(connector: PagerDutyConnector) -> None:
    respx.get(
        f"{_BASE}/users",
        params={"team_ids": "TEAM1"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"users": [{"id": "U3", "name": "Charlie"}], "total": 1, "more": False},
        )
    )
    result = await connector.query(ConnectorQuery(resource="users", filters={"team_ids": "TEAM1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Charlie"
