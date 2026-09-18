"""Unit tests for OpsgenieConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors._safe_page import safe_paging_total as _paging_total_count
from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.opsgenie import OpsgenieConnector, _next_offset_cursor

API_KEY = "og_test_key"
_BASE = "https://api.opsgenie.com/v2"


@pytest.fixture
def connector() -> OpsgenieConnector:
    return OpsgenieConnector(api_key=API_KEY)


def test_connector_type(connector: OpsgenieConnector) -> None:
    assert connector.connector_type == ConnectorType.OPSGENIE


# ── Health Check ──────────────────────────────────────────────────────


@respx.mock
async def test_health_check_ok(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts", params={"limit": 1}).mock(
        return_value=httpx.Response(200, json={"data": [], "totalCount": 0})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Opsgenie API key validated"


@respx.mock
async def test_health_check_invalid_key(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts", params={"limit": 1}).mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Opsgenie API key" in result.detail


@respx.mock
async def test_health_check_forbidden(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts", params={"limit": 1}).mock(return_value=httpx.Response(403, text="Forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Opsgenie API key" in result.detail


@respx.mock
async def test_health_check_network_error(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts", params={"limit": 1}).mock(side_effect=httpx.ConnectError("connection refused"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


@respx.mock
async def test_health_check_other_status(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts", params={"limit": 1}).mock(return_value=httpx.Response(429, text="Too Many Requests"))
    result = await connector.health_check()
    assert result.ok is False
    assert "429" in result.detail


# ── Query: alerts ─────────────────────────────────────────────────────


@respx.mock
async def test_query_alerts(connector: OpsgenieConnector) -> None:
    data = [
        {"id": "A1", "message": "Production down", "status": "open"},
        {"id": "A2", "message": "High CPU", "status": "open"},
    ]
    respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(200, json={"data": data, "totalCount": 2}))
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert len(result.records) == 2
    assert result.records[0]["message"] == "Production down"
    assert result.total == 2


@respx.mock
async def test_query_alerts_with_filters(connector: OpsgenieConnector) -> None:
    respx.get(
        f"{_BASE}/alerts",
        params={"status": "open", "priority": "P1"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "A3", "message": "Critical", "priority": "P1"}], "totalCount": 1},
        )
    )
    result = await connector.query(ConnectorQuery(resource="alerts", filters={"status": "open", "priority": "P1"}))
    assert len(result.records) == 1
    assert result.records[0]["priority"] == "P1"


@respx.mock
async def test_query_alerts_with_limit(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts", params={"limit": 5}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": f"A{i}", "message": f"Alert {i}"} for i in range(10)],
                "totalCount": 10,
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="alerts", limit=5))
    assert len(result.records) == 5


@respx.mock
async def test_query_alerts_with_cursor(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts", params={"offset": "10"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "A11", "message": "Page two"}],
                "totalCount": 25,
                "paging": {"next": "offset=20"},
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="alerts", cursor="10"))
    assert len(result.records) == 1
    assert result.next_cursor is not None


# ── Query: alerts — corrupt/non-finite paging hardening ───────────────


@respx.mock
async def test_query_alerts_corrupt_total_inf(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts").mock(
        return_value=httpx.Response(200, content=b'{"data": [{"id": "A1"}], "totalCount": 1e999}')
    )
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert len(result.records) == 1
    assert result.total == 0


@respx.mock
async def test_query_alerts_corrupt_total_garbage(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "A1"}], "totalCount": "not-a-number"},
        )
    )
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert len(result.records) == 1
    assert result.total == 0


@respx.mock
async def test_query_alerts_corrupt_total_bool(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "A1"}], "totalCount": True},
        )
    )
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert result.total == 0


@respx.mock
async def test_query_alerts_non_dict_paging(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "A1"}], "totalCount": 10, "paging": "next"},
        )
    )
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_query_alerts_paging_without_next(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "A1"}], "totalCount": 10, "paging": {"first": "offset=0"}},
        )
    )
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_query_alerts_garbage_offset_cursor(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts", params={"offset": "garbage"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "A1"}],
                "totalCount": 10,
                "paging": {"next": "offset=20"},
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="alerts", cursor="garbage"))
    assert len(result.records) == 1
    assert result.next_cursor == "1"


@respx.mock
async def test_query_teams_corrupt_total_inf(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/teams").mock(
        return_value=httpx.Response(200, content=b'{"data": [{"id": "T1"}], "totalCount": 1e999}')
    )
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert len(result.records) == 1
    assert result.total == 0


@respx.mock
async def test_query_alerts_corrupt_body_no_crash(connector: OpsgenieConnector) -> None:
    """A non-dict body from the alerts list endpoint must degrade to an empty
    page instead of crashing with AttributeError on ``.get()``."""
    respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert not result.records
    assert result.total is None


@respx.mock
async def test_query_alerts_non_list_data_no_crash(connector: OpsgenieConnector) -> None:
    """A corrupt body placing a non-list in ``data`` must fall back to an
    empty page instead of returning a bare string as the records list."""
    respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(200, json={"data": "not-a-list"}))
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert not result.records
    assert result.total is None


@respx.mock
async def test_query_teams_corrupt_body_no_crash(connector: OpsgenieConnector) -> None:
    """A non-dict teams body must degrade to an empty page, not crash."""
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert not result.records
    assert result.total is None


@respx.mock
async def test_query_schedules_corrupt_body_no_crash(connector: OpsgenieConnector) -> None:
    """A non-dict schedules body must degrade to an empty page, not crash."""
    respx.get(f"{_BASE}/schedules").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="schedules"))
    assert not result.records
    assert result.total is None


@respx.mock
async def test_query_escalations_corrupt_body_no_crash(connector: OpsgenieConnector) -> None:
    """A non-dict escalations body must degrade to an empty page, not crash."""
    respx.get(f"{_BASE}/escalations").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await connector.query(ConnectorQuery(resource="escalations"))
    assert not result.records
    assert result.total is None


# ── Pagination helpers — direct unit coverage ──────────────────────────


def test_paging_total_count() -> None:
    assert _paging_total_count({"totalCount": 25}, "totalCount") == 25
    assert _paging_total_count({"totalCount": "25"}, "totalCount") == 25
    assert _paging_total_count({"totalCount": 1e999}, "totalCount") == 0
    assert _paging_total_count({"totalCount": float("nan")}, "totalCount") == 0
    assert _paging_total_count({"totalCount": True}, "totalCount") == 0
    assert _paging_total_count({"totalCount": "garbage"}, "totalCount") == 0
    assert _paging_total_count({}, "totalCount") is None
    assert _paging_total_count(["garbage"], "totalCount") is None


def test_next_offset_cursor() -> None:
    assert _next_offset_cursor("10", [{"id": "A1"}], {"next": "offset=20"}) == "11"
    assert _next_offset_cursor(0, [{"id": "A1"}, {"id": "A2"}], {"next": "offset=20"}) == "2"
    assert _next_offset_cursor("garbage", [{"id": "A1"}], {"next": "offset=20"}) == "1"
    assert _next_offset_cursor(1e999, [{"id": "A1"}], {"next": "offset=20"}) == "1"
    assert _next_offset_cursor("10", [{"id": "A1"}], {"first": "offset=0"}) is None
    assert _next_offset_cursor("10", [{"id": "A1"}], "garbage") is None
    assert _next_offset_cursor("10", [{"id": "A1"}], None) is None


# ── Query: alert (single) ────────────────────────────────────────────


@respx.mock
async def test_query_alert_by_id(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts/abc-123", params={"identifierType": "id"}).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"id": "abc-123", "message": "Disk full", "status": "open"}},
        )
    )
    result = await connector.query(ConnectorQuery(resource="alert", filters={"id": "abc-123"}))
    assert len(result.records) == 1
    assert result.records[0]["message"] == "Disk full"


@respx.mock
async def test_query_alert_via_cursor(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts/abc-123", params={"identifierType": "id"}).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"id": "abc-123", "message": "Disk full"}},
        )
    )
    result = await connector.query(ConnectorQuery(resource="alert", filters={"id": "abc-123"}))
    assert len(result.records) == 1


async def test_query_alert_missing_id(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Opsgenie alert query requires 'id'"):
        await connector.query(ConnectorQuery(resource="alert", filters={}))


# ── Query: alert_notes ───────────────────────────────────────────────


@respx.mock
async def test_query_alert_notes(connector: OpsgenieConnector) -> None:
    notes = [
        {"id": "N1", "note": "Investigating", "owner": "Alice"},
        {"id": "N2", "note": "Escalated", "owner": "Bob"},
    ]
    respx.get(f"{_BASE}/alerts/abc-123/notes").mock(
        return_value=httpx.Response(200, json={"data": notes, "totalCount": 2})
    )
    result = await connector.query(ConnectorQuery(resource="alert_notes", filters={"id": "abc-123"}))
    assert len(result.records) == 2
    assert result.records[0]["note"] == "Investigating"


async def test_query_alert_notes_missing_id(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Opsgenie alert_notes query requires 'id'"):
        await connector.query(ConnectorQuery(resource="alert_notes", filters={}))


# ── Query: alert_logs ────────────────────────────────────────────────


@respx.mock
async def test_query_alert_logs(connector: OpsgenieConnector) -> None:
    logs = [{"log": "Alert created", "loggedAt": "2026-01-01T00:00:00Z"}]
    respx.get(f"{_BASE}/alerts/abc-123/logs").mock(
        return_value=httpx.Response(200, json={"data": logs, "totalCount": 1})
    )
    result = await connector.query(ConnectorQuery(resource="alert_logs", filters={"id": "abc-123"}))
    assert len(result.records) == 1
    assert result.records[0]["log"] == "Alert created"


async def test_query_alert_logs_missing_id(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Opsgenie alert_logs query requires 'id'"):
        await connector.query(ConnectorQuery(resource="alert_logs", filters={}))


# ── Query: teams ────────────────────────────────────────────────────


@respx.mock
async def test_query_teams(connector: OpsgenieConnector) -> None:
    teams = [
        {"id": "T1", "name": "Engineering"},
        {"id": "T2", "name": "Operations"},
    ]
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json={"data": teams, "totalCount": 2}))
    result = await connector.query(ConnectorQuery(resource="teams"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Engineering"


@respx.mock
async def test_query_teams_with_query_filter(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/teams", params={"query": "Eng"}).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "T1", "name": "Engineering"}], "totalCount": 1},
        )
    )
    result = await connector.query(ConnectorQuery(resource="teams", filters={"query": "Eng"}))
    assert len(result.records) == 1


# ── Query: schedules ────────────────────────────────────────────────


@respx.mock
async def test_query_schedules(connector: OpsgenieConnector) -> None:
    schedules = [{"id": "SCH1", "name": "Primary On-Call"}]
    respx.get(f"{_BASE}/schedules").mock(return_value=httpx.Response(200, json={"data": schedules, "totalCount": 1}))
    result = await connector.query(ConnectorQuery(resource="schedules"))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Primary On-Call"


@respx.mock
async def test_query_schedules_with_limit(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/schedules", params={"limit": 3}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": f"SCH{i}"} for i in range(5)],
                "totalCount": 5,
            },
        )
    )
    result = await connector.query(ConnectorQuery(resource="schedules", limit=3))
    assert len(result.records) == 3


# ── Query: on_calls ────────────────────────────────────────────────


@respx.mock
async def test_query_on_calls(connector: OpsgenieConnector) -> None:
    data = {
        "parent": {"id": "SCH1", "name": "Primary On-Call"},
        "onCallParticipants": [{"name": "alice@example.com", "type": "user"}],
    }
    respx.get(f"{_BASE}/schedules/SCH1/on-calls").mock(return_value=httpx.Response(200, json={"data": data}))
    result = await connector.query(ConnectorQuery(resource="on_calls", filters={"schedule_id": "SCH1"}))
    assert len(result.records) == 1
    assert result.records[0]["parent"]["id"] == "SCH1"


async def test_query_on_calls_missing_schedule_id(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Opsgenie on_calls query requires 'schedule_id'"):
        await connector.query(ConnectorQuery(resource="on_calls", filters={}))


@respx.mock
async def test_query_on_calls_via_cursor(connector: OpsgenieConnector) -> None:
    data = {"onCallParticipants": [{"name": "bob@example.com", "type": "user"}]}
    respx.get(f"{_BASE}/schedules/SCH1/on-calls").mock(return_value=httpx.Response(200, json={"data": data}))
    result = await connector.query(ConnectorQuery(resource="on_calls", cursor="SCH1"))
    assert len(result.records) == 1


# ── Query: escalations ─────────────────────────────────────────────


@respx.mock
async def test_query_escalations(connector: OpsgenieConnector) -> None:
    escalations = [
        {"id": "E1", "name": "Critical Escalation"},
        {"id": "E2", "name": "Standard Escalation"},
    ]
    respx.get(f"{_BASE}/escalations").mock(
        return_value=httpx.Response(200, json={"data": escalations, "totalCount": 2})
    )
    result = await connector.query(ConnectorQuery(resource="escalations"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Critical Escalation"


@respx.mock
async def test_query_escalations_with_limit(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/escalations", params={"limit": 1}).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "E1"}], "totalCount": 5},
        )
    )
    result = await connector.query(ConnectorQuery(resource="escalations", limit=1))
    assert len(result.records) == 1


# ── Write: create alert ──────────────────────────────────────────────


@respx.mock
async def test_write_alert(connector: OpsgenieConnector) -> None:
    respx.post(f"{_BASE}/alerts").mock(
        return_value=httpx.Response(
            201,
            json={
                "data": {
                    "id": "ALERT1",
                    "message": "Production down",
                    "priority": "P1",
                    "status": "open",
                }
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="alert",
            data={
                "message": "Production down",
                "priority": "P1",
                "description": "Server is unreachable",
            },
        )
    )
    assert result["id"] == "ALERT1"
    assert result["priority"] == "P1"


@respx.mock
async def test_write_alert_with_all_fields(connector: OpsgenieConnector) -> None:
    respx.post(f"{_BASE}/alerts").mock(
        return_value=httpx.Response(
            201,
            json={
                "data": {
                    "id": "ALERT2",
                    "message": "Full alert",
                    "tags": ["prod"],
                }
            },
        )
    )
    result = await connector.write(
        ConnectorPayload(
            resource="alert",
            data={
                "message": "Full alert",
                "description": "Everything",
                "priority": "P2",
                "source": "Terraform",
                "tags": ["prod", "critical"],
                "entity": "web-server-01",
                "alias": "alert-001",
                "responders": [{"type": "team", "id": "T1"}],
            },
        )
    )
    assert result["id"] == "ALERT2"


@respx.mock
async def test_write_alert_missing_message(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Opsgenie alert write requires 'message'"):
        await connector.write(ConnectorPayload(resource="alert", data={"priority": "P3"}))


# ── Write: acknowledge ───────────────────────────────────────────────


@respx.mock
async def test_write_acknowledge(connector: OpsgenieConnector) -> None:
    respx.post(f"{_BASE}/alerts/ALERT1/acknowledge").mock(
        return_value=httpx.Response(200, json={"data": {"success": True}})
    )
    result = await connector.write(ConnectorPayload(resource="alert_acknowledge", data={"id": "ALERT1"}))
    assert result["success"] is True


@respx.mock
async def test_write_acknowledge_with_note(connector: OpsgenieConnector) -> None:
    respx.post(f"{_BASE}/alerts/ALERT1/acknowledge").mock(
        return_value=httpx.Response(200, json={"data": {"success": True}})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="alert_acknowledge",
            data={"id": "ALERT1", "note": "Looking into it", "user": "alice"},
        )
    )
    assert result["success"] is True


async def test_write_acknowledge_missing_id(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Opsgenie alert_acknowledge write requires 'id'"):
        await connector.write(ConnectorPayload(resource="alert_acknowledge", data={}))


# ── Write: close ─────────────────────────────────────────────────────


@respx.mock
async def test_write_close(connector: OpsgenieConnector) -> None:
    respx.post(f"{_BASE}/alerts/ALERT1/close").mock(return_value=httpx.Response(200, json={"data": {"success": True}}))
    result = await connector.write(ConnectorPayload(resource="alert_close", data={"id": "ALERT1"}))
    assert result["success"] is True


@respx.mock
async def test_write_close_with_source_and_note(connector: OpsgenieConnector) -> None:
    respx.post(f"{_BASE}/alerts/ALERT1/close").mock(return_value=httpx.Response(200, json={"data": {"success": True}}))
    result = await connector.write(
        ConnectorPayload(
            resource="alert_close",
            data={"id": "ALERT1", "note": "Resolved by automation", "source": "Modulo"},
        )
    )
    assert result["success"] is True


async def test_write_close_missing_id(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Opsgenie alert_close write requires 'id'"):
        await connector.write(ConnectorPayload(resource="alert_close", data={}))


# ── Write: note ──────────────────────────────────────────────────────


@respx.mock
async def test_write_note(connector: OpsgenieConnector) -> None:
    respx.post(f"{_BASE}/alerts/ALERT1/notes").mock(
        return_value=httpx.Response(201, json={"data": {"id": "N1", "note": "Checking logs"}})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="alert_note",
            data={"id": "ALERT1", "note": "Checking logs"},
        )
    )
    assert result["id"] == "N1"


async def test_write_note_missing_id(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Opsgenie alert_note write requires 'id' and 'note'"):
        await connector.write(ConnectorPayload(resource="alert_note", data={"note": "Some note"}))


async def test_write_note_missing_note(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Opsgenie alert_note write requires 'id' and 'note'"):
        await connector.write(ConnectorPayload(resource="alert_note", data={"id": "ALERT1"}))


# ── Write: snooze ────────────────────────────────────────────────────


@respx.mock
async def test_write_snooze(connector: OpsgenieConnector) -> None:
    respx.post(f"{_BASE}/alerts/ALERT1/snooze").mock(return_value=httpx.Response(200, json={"data": {"success": True}}))
    result = await connector.write(
        ConnectorPayload(
            resource="alert_snooze",
            data={"id": "ALERT1", "end_time": "2026-07-01T00:00:00Z"},
        )
    )
    assert result["success"] is True


@respx.mock
async def test_write_snooze_with_note_and_user(connector: OpsgenieConnector) -> None:
    respx.post(f"{_BASE}/alerts/ALERT1/snooze").mock(return_value=httpx.Response(200, json={"data": {"success": True}}))
    result = await connector.write(
        ConnectorPayload(
            resource="alert_snooze",
            data={
                "id": "ALERT1",
                "end_time": "2026-07-01T06:00:00Z",
                "note": "Snoozed until morning",
                "user": "bob",
            },
        )
    )
    assert result["success"] is True


async def test_write_snooze_missing_id(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Opsgenie alert_snooze write requires 'id' and 'end_time'"):
        await connector.write(ConnectorPayload(resource="alert_snooze", data={"end_time": "2026-07-01T00:00:00Z"}))


async def test_write_snooze_missing_end_time(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Opsgenie alert_snooze write requires 'id' and 'end_time'"):
        await connector.write(ConnectorPayload(resource="alert_snooze", data={"id": "ALERT1"}))


# ── Error paths ──────────────────────────────────────────────────────


async def test_query_invalid_resource(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Opsgenie resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


async def test_write_invalid_resource(connector: OpsgenieConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Opsgenie write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


@respx.mock
async def test_query_http_500(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="alerts"))


@respx.mock
async def test_write_http_403(connector: OpsgenieConnector) -> None:
    respx.post(f"{_BASE}/alerts").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(ConnectorPayload(resource="alert", data={"message": "Test"}))


@respx.mock
async def test_query_alerts_empty(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(200, json={"data": [], "totalCount": 0}))
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_alert_not_found(connector: OpsgenieConnector) -> None:
    respx.get(f"{_BASE}/alerts/unknown", params={"identifierType": "id"}).mock(
        return_value=httpx.Response(200, json={"data": {}})
    )
    result = await connector.query(ConnectorQuery(resource="alert", filters={"id": "unknown"}))
    assert not result.records


# ── Auth header check ────────────────────────────────────────────────


@respx.mock
async def test_auth_header_sent(connector: OpsgenieConnector) -> None:
    route = respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(200, json={"data": [], "totalCount": 0}))
    await connector.query(ConnectorQuery(resource="alerts"))
    req = route.calls[0].request
    assert req.headers["Authorization"] == f"GenieKey {API_KEY}"


@respx.mock
async def test_query_alerts_non_numeric_cursor_does_not_crash(connector: OpsgenieConnector) -> None:
    """A non-numeric 'offset' cursor must not crash next-cursor arithmetic."""
    respx.get(f"{_BASE}/alerts", params={"offset": "abc"}).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "A1", "message": "Down"}], "totalCount": 25, "paging": {"next": "offset=10"}},
        )
    )
    result = await connector.query(ConnectorQuery(resource="alerts", cursor="abc"))
    assert len(result.records) == 1
    assert result.next_cursor == "1"


@respx.mock
async def test_query_alerts_non_finite_total_count_does_not_leak(connector: OpsgenieConnector) -> None:
    """A corrupt 'totalCount: 1e999' (json parses to inf) must not leak into the result."""
    respx.get(f"{_BASE}/alerts").mock(
        return_value=httpx.Response(
            200,
            text='{"data": [{"id": "A1", "message": "Down"}], "totalCount": 1e999}',
        )
    )
    result = await connector.query(ConnectorQuery(resource="alerts"))
    assert len(result.records) == 1
    assert result.total == 0
