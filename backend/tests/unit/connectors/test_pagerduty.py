"""Unit tests for PagerDutyConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors._safe_page import safe_paging_total as _paging_total
from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.pagerduty import PagerDutyConnector, _body_field, _next_offset_cursor

TOKEN = "pd_test_token"
_BASE = "https://api.pagerduty.com"


@pytest.fixture
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


@respx.mock
async def test_query_services_non_finite_offset_does_not_poison_cursor(connector: PagerDutyConnector) -> None:
    """A corrupt 'offset: 1e999' (json parses to inf) must not poison the next cursor."""
    respx.get(f"{_BASE}/services").mock(
        return_value=httpx.Response(
            200,
            text='{"services": [{"id": "S1", "name": "Web API"}], "total": 50, "more": true, "offset": 1e999}',
        )
    )
    result = await connector.query(ConnectorQuery(resource="services"))
    assert len(result.records) == 1
    assert result.next_cursor == "1"


@respx.mock
async def test_query_users_string_offset_does_not_crash(connector: PagerDutyConnector) -> None:
    """A string 'offset' from the API must not crash next-cursor arithmetic."""
    respx.get(f"{_BASE}/users").mock(
        return_value=httpx.Response(
            200,
            text='{"users": [{"id": "U1"}], "total": 10, "more": true, "offset": "25"}',
        )
    )
    result = await connector.query(ConnectorQuery(resource="users"))
    assert len(result.records) == 1
    assert result.next_cursor == "26"


@respx.mock
async def test_query_incidents_non_finite_total_does_not_leak(connector: PagerDutyConnector) -> None:
    """A corrupt 'total: 1e999' (json parses to inf) must not leak into the result."""
    respx.get(f"{_BASE}/incidents").mock(
        return_value=httpx.Response(
            200,
            text='{"incidents": [{"id": "I1"}], "total": 1e999, "more": false}',
        )
    )
    result = await connector.query(ConnectorQuery(resource="incidents"))
    assert len(result.records) == 1
    assert result.total == 0


# ── Helpers: corrupt/non-finite paging hardening ──────────────────────


def test_paging_total_missing() -> None:
    assert _paging_total({}, "total") is None


def test_paging_total_non_dict() -> None:
    assert _paging_total(["garbage"], "total") is None
    assert _paging_total("garbage", "total") is None
    assert _paging_total(None, "total") is None


def test_body_field() -> None:
    assert _body_field({"offset": 25, "more": True}, "offset", 0) == 25
    assert _body_field({"more": True}, "offset", 0) == 0
    assert _body_field(["garbage"], "offset", 0) == 0
    assert _body_field(["garbage"], "more", None) is None
    assert _body_field("garbage", "offset", 0) == 0
    assert _body_field(None, "offset", 0) == 0


def test_paging_total_valid() -> None:
    assert _paging_total({"total": 42}, "total") == 42


def test_paging_total_corrupt_inf() -> None:
    assert _paging_total({"total": float("inf")}, "total") == 0


def test_paging_total_corrupt_nan() -> None:
    assert _paging_total({"total": float("nan")}, "total") == 0


def test_paging_total_corrupt_bool() -> None:
    assert _paging_total({"total": True}, "total") == 0


def test_paging_total_corrupt_garbage() -> None:
    assert _paging_total({"total": "not-a-number"}, "total") == 0


def test_next_offset_cursor_no_more() -> None:
    assert _next_offset_cursor(0, [{"id": "A"}], None) is None
    assert _next_offset_cursor(0, [{"id": "A"}], False) is None


def test_next_offset_cursor_valid() -> None:
    assert _next_offset_cursor(25, [{"id": "A"}, {"id": "B"}], True) == "27"


def test_next_offset_cursor_corrupt_offset() -> None:
    assert _next_offset_cursor(float("inf"), [{"id": "A"}], True) == "1"
    assert _next_offset_cursor("garbage", [{"id": "A"}], True) == "1"
    assert _next_offset_cursor(True, [{"id": "A"}], True) == "1"


# ── Query: corrupt/non-finite response hardening ──────────────────────


@respx.mock
async def test_query_incidents_corrupt_total_inf(connector: PagerDutyConnector) -> None:
    respx.get(f"{_BASE}/incidents").mock(
        return_value=httpx.Response(200, content=b'{"incidents": [{"id": "I1"}], "total": 1e999, "more": false}')
    )
    result = await connector.query(ConnectorQuery(resource="incidents"))
    assert len(result.records) == 1
    assert result.total == 0


@respx.mock
async def test_query_incidents_garbage_cursor_does_not_crash(connector: PagerDutyConnector) -> None:
    """A non-numeric cursor falls back to offset 0 instead of raising ValueError."""
    respx.get(
        f"{_BASE}/incidents",
        params={"offset": 0},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"incidents": [{"id": "I0", "title": "First incident"}], "total": 1, "more": False},
        )
    )
    result = await connector.query(ConnectorQuery(resource="incidents", cursor="not-a-number"))
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_query_services_corrupt_total_garbage(connector: PagerDutyConnector) -> None:
    respx.get(f"{_BASE}/services").mock(
        return_value=httpx.Response(200, json={"services": [{"id": "S1"}], "total": "not-a-number", "more": False})
    )
    result = await connector.query(ConnectorQuery(resource="services"))
    assert len(result.records) == 1
    assert result.total == 0


@respx.mock
async def test_query_on_calls_corrupt_offset_no_poisoned_cursor(connector: PagerDutyConnector) -> None:
    respx.get(f"{_BASE}/oncalls").mock(
        return_value=httpx.Response(200, content=b'{"oncalls": [{"user": {"id": "U1"}}], "total": 1e999, "more": true}')
    )
    result = await connector.query(ConnectorQuery(resource="on_calls"))
    assert len(result.records) == 1
    assert result.total == 0
    assert result.next_cursor is None or result.next_cursor.isdigit()


@respx.mock
async def test_query_teams_final_page_no_cursor(connector: PagerDutyConnector) -> None:
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json={"teams": [{"id": "T1"}], "total": 1}))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert len(result.records) == 1
    assert result.total == 1
    assert result.next_cursor is None


# ── Query: corrupt/non-dict body hardening ────────────────────────────


@respx.mock
async def test_query_incidents_corrupt_body_no_crash(connector: PagerDutyConnector) -> None:
    """A non-dict incidents body must degrade to an empty page, not crash."""
    respx.get(f"{_BASE}/incidents").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="incidents"))
    assert not result.records
    assert result.total is None
    assert result.next_cursor is None


@respx.mock
async def test_query_incidents_non_list_records_no_crash(connector: PagerDutyConnector) -> None:
    """A corrupt body placing a non-list in ``incidents`` must fall back to an
    empty page instead of returning a bare string as the records list."""
    respx.get(f"{_BASE}/incidents").mock(return_value=httpx.Response(200, json={"incidents": "not-a-list"}))
    result = await connector.query(ConnectorQuery(resource="incidents"))
    assert not result.records
    assert result.total is None


@respx.mock
async def test_query_services_corrupt_body_no_crash(connector: PagerDutyConnector) -> None:
    """A non-dict services body must degrade to an empty page, not crash."""
    respx.get(f"{_BASE}/services").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="services"))
    assert not result.records
    assert result.total is None
    assert result.next_cursor is None


@respx.mock
async def test_query_users_corrupt_body_no_crash(connector: PagerDutyConnector) -> None:
    """A non-dict users body must degrade to an empty page, not crash."""
    respx.get(f"{_BASE}/users").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="users"))
    assert not result.records
    assert result.total is None
    assert result.next_cursor is None


@respx.mock
async def test_query_teams_corrupt_body_no_crash(connector: PagerDutyConnector) -> None:
    """A non-dict teams body must degrade to an empty page, not crash."""
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert not result.records
    assert result.total is None
    assert result.next_cursor is None


@respx.mock
async def test_query_escalation_policies_corrupt_body_no_crash(connector: PagerDutyConnector) -> None:
    """A non-dict escalation_policies body must degrade to an empty page, not crash."""
    respx.get(f"{_BASE}/escalation_policies").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="escalation_policies"))
    assert not result.records
    assert result.total is None
    assert result.next_cursor is None


@respx.mock
async def test_query_schedules_corrupt_body_no_crash(connector: PagerDutyConnector) -> None:
    """A non-dict schedules body must degrade to an empty page, not crash."""
    respx.get(f"{_BASE}/schedules").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="schedules"))
    assert not result.records
    assert result.total is None
    assert result.next_cursor is None


@respx.mock
async def test_query_on_calls_corrupt_body_no_crash(connector: PagerDutyConnector) -> None:
    """A non-dict oncalls body must degrade to an empty page, not crash."""
    respx.get(f"{_BASE}/oncalls").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="on_calls"))
    assert not result.records
    assert result.total is None
    assert result.next_cursor is None
