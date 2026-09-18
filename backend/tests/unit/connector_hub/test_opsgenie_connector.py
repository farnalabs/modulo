"""Unit tests for OpsgenieConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.opsgenie import OpsgenieConnector

API_KEY = "opsgenie_test_key"
_BASE = "https://api.opsgenie.com/v2"


@pytest.fixture
def connector():
    return OpsgenieConnector(api_key=API_KEY)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.OPSGENIE


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(200, json={"data": [], "totalCount": 0}))
    result = await connector.health_check()
    assert result.ok is True
    assert "validated" in result.detail


@respx.mock
async def test_health_check_invalid_key(connector):
    respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Opsgenie API key" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


# ---------------------------------------------------------------------------
# query — alerts
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_alerts(connector):
    alerts = [{"id": "abc-1", "message": "High CPU"}, {"id": "abc-2", "message": "Disk full"}]
    respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(200, json={"data": alerts, "totalCount": 2}))
    result = await connector.query(ConnectorQuery(resource="alerts", limit=10))
    assert result.total == 2
    assert result.records[0]["id"] == "abc-1"


@respx.mock
async def test_query_alerts_with_status_filter(connector):
    alerts = [{"id": "abc-1", "message": "High CPU"}]
    respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(200, json={"data": alerts, "totalCount": 1}))
    result = await connector.query(
        ConnectorQuery(resource="alerts", filters={"status": "open"}, limit=10),
    )
    assert result.total == 1


@respx.mock
async def test_query_alerts_with_cursor(connector):
    alerts = [{"id": "abc-3", "message": "Down"}]
    respx.get(f"{_BASE}/alerts").mock(
        return_value=httpx.Response(200, json={"data": alerts, "totalCount": 11, "paging": {"next": "..."}}),
    )
    result = await connector.query(ConnectorQuery(resource="alerts", cursor="10"))
    assert result.records[0]["id"] == "abc-3"
    assert result.next_cursor is not None


# ---------------------------------------------------------------------------
# query — alert
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_alert(connector):
    alert = {"id": "abc-123", "message": "High CPU"}
    respx.get(f"{_BASE}/alerts/abc-123").mock(return_value=httpx.Response(200, json={"data": alert}))
    result = await connector.query(ConnectorQuery(resource="alert", filters={"id": "abc-123"}))
    assert result.records[0]["id"] == "abc-123"


async def test_query_alert_missing_id(connector):
    query = ConnectorQuery(resource="alert")
    with pytest.raises(ValueError, match="'id' in filters"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — alert_notes / alert_logs
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_alert_notes(connector):
    notes = [{"id": "note-1", "note": "Investigating"}]
    respx.get(f"{_BASE}/alerts/abc-123/notes").mock(return_value=httpx.Response(200, json={"data": notes}))
    result = await connector.query(ConnectorQuery(resource="alert_notes", filters={"id": "abc-123"}))
    assert result.records[0]["id"] == "note-1"


@respx.mock
async def test_query_alert_logs(connector):
    logs = [{"id": "log-1", "log": "created"}]
    respx.get(f"{_BASE}/alerts/abc-123/logs").mock(return_value=httpx.Response(200, json={"data": logs}))
    result = await connector.query(ConnectorQuery(resource="alert_logs", filters={"id": "abc-123"}))
    assert result.records[0]["id"] == "log-1"


# ---------------------------------------------------------------------------
# query — teams / schedules / escalations
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_teams(connector):
    teams = [{"id": "team-1", "name": "SRE"}]
    respx.get(f"{_BASE}/teams").mock(return_value=httpx.Response(200, json={"data": teams, "totalCount": 1}))
    result = await connector.query(ConnectorQuery(resource="teams", limit=10))
    assert result.records[0]["name"] == "SRE"


@respx.mock
async def test_query_schedules(connector):
    schedules = [{"id": "sch-456", "name": "OnCall"}]
    respx.get(f"{_BASE}/schedules").mock(
        return_value=httpx.Response(200, json={"data": schedules, "totalCount": 1}),
    )
    result = await connector.query(ConnectorQuery(resource="schedules", limit=10))
    assert result.records[0]["id"] == "sch-456"


@respx.mock
async def test_query_escalations(connector):
    escalations = [{"id": "esc-1", "name": "L1"}]
    respx.get(f"{_BASE}/escalations").mock(
        return_value=httpx.Response(200, json={"data": escalations, "totalCount": 1}),
    )
    result = await connector.query(ConnectorQuery(resource="escalations", limit=10))
    assert result.records[0]["id"] == "esc-1"


# ---------------------------------------------------------------------------
# query — on_calls
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_on_calls(connector):
    data = {"onCallParticipants": [{"name": "alice"}]}
    respx.get(f"{_BASE}/schedules/sch-456/on-calls").mock(return_value=httpx.Response(200, json={"data": data}))
    result = await connector.query(ConnectorQuery(resource="on_calls", filters={"schedule_id": "sch-456"}))
    assert result.records[0]["onCallParticipants"][0]["name"] == "alice"


async def test_query_on_calls_missing_schedule_id(connector):
    query = ConnectorQuery(resource="on_calls")
    with pytest.raises(ValueError, match="'schedule_id' in filters or cursor"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    query = ConnectorQuery(resource="invalid")
    with pytest.raises(ValueError, match="Unsupported Opsgenie resource"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# write — alert
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_alert(connector):
    created = {"result": "Request will be processed", "requestId": "req-1"}
    respx.post(f"{_BASE}/alerts").mock(return_value=httpx.Response(202, json={"data": created}))
    result = await connector.write(
        ConnectorPayload(
            resource="alert",
            data={"message": "Production down"},
        ),
    )
    assert result["requestId"] == "req-1"


@respx.mock
async def test_write_alert_unwrapped_response_returns_empty(connector):
    """A 2xx response without a 'data' wrapper must not be returned as-is.

    Regression: `result.get("data", result)` silently returned the whole
    response (e.g. an error envelope) as the created alert.
    """
    respx.post(f"{_BASE}/alerts").mock(return_value=httpx.Response(202, json={"message": "oops"}))
    result = await connector.write(
        ConnectorPayload(
            resource="alert",
            data={"message": "Production down"},
        ),
    )
    assert result == {}


async def test_write_alert_missing_message(connector):
    payload = ConnectorPayload(resource="alert", data={})
    with pytest.raises(ValueError, match="'message' in data"):
        await connector.write(payload)


# ---------------------------------------------------------------------------
# write — alert_acknowledge / alert_close
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_alert_acknowledge(connector):
    result_body = {"result": "Alert acknowledged", "requestId": "req-2"}
    respx.post(f"{_BASE}/alerts/alert-789/acknowledge").mock(
        return_value=httpx.Response(202, json={"data": result_body}),
    )
    result = await connector.write(
        ConnectorPayload(resource="alert_acknowledge", data={"id": "alert-789"}),
    )
    assert result["requestId"] == "req-2"


async def test_write_alert_acknowledge_missing_id(connector):
    payload = ConnectorPayload(resource="alert_acknowledge", data={})
    with pytest.raises(ValueError, match="'id' in data"):
        await connector.write(payload)


@respx.mock
async def test_write_alert_close(connector):
    result_body = {"result": "Alert closed", "requestId": "req-3"}
    respx.post(f"{_BASE}/alerts/alert-789/close").mock(
        return_value=httpx.Response(202, json={"data": result_body}),
    )
    result = await connector.write(
        ConnectorPayload(resource="alert_close", data={"id": "alert-789"}),
    )
    assert result["requestId"] == "req-3"


# ---------------------------------------------------------------------------
# write — alert_note / alert_snooze
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_alert_note(connector):
    result_body = {"result": "Note added", "requestId": "req-4"}
    respx.post(f"{_BASE}/alerts/alert-789/notes").mock(
        return_value=httpx.Response(202, json={"data": result_body}),
    )
    result = await connector.write(
        ConnectorPayload(resource="alert_note", data={"id": "alert-789", "note": "Investigating"}),
    )
    assert result["requestId"] == "req-4"


async def test_write_alert_note_missing_fields(connector):
    with pytest.raises(ValueError, match="'id' and 'note' in data"):
        await connector.write(ConnectorPayload(resource="alert_note", data={"id": "alert-789"}))
    with pytest.raises(ValueError, match="'id' and 'note' in data"):
        await connector.write(ConnectorPayload(resource="alert_note", data={"note": "x"}))


@respx.mock
async def test_write_alert_snooze(connector):
    result_body = {"result": "Alert snoozed", "requestId": "req-5"}
    respx.post(f"{_BASE}/alerts/alert-789/snooze").mock(
        return_value=httpx.Response(202, json={"data": result_body}),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="alert_snooze",
            data={"id": "alert-789", "end_time": "2026-07-01T00:00:00Z"},
        ),
    )
    assert result["requestId"] == "req-5"


async def test_write_alert_snooze_missing_fields(connector):
    with pytest.raises(ValueError, match="'id' and 'end_time' in data"):
        await connector.write(ConnectorPayload(resource="alert_snooze", data={"id": "alert-789"}))
    with pytest.raises(ValueError, match="'id' and 'end_time' in data"):
        await connector.write(ConnectorPayload(resource="alert_snooze", data={"end_time": "x"}))


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Opsgenie write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/alerts").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="alerts"))
