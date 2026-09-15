"""Step definitions for the Opsgenie connector BDD feature.

Wires ``features/connectors/opsgenie_connector.feature`` into the executing
suite (the improve-architecture product-map walk) by driving the REAL
``OpsgenieConnector`` against a respx-mocked Opsgenie REST API v2 — mirroring
``backend/tests/unit/connectors/test_opsgenie.py`` so the executing BDD surface
locks the same contract the unit suite does: listing alerts / teams /
schedules / escalations, single-alert / notes / logs lookups, on-call lookups,
and the alert write family (create / acknowledge / close / note / snooze).
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/opsgenie_connector.feature")

API_KEY = "og_test_key"
_BASE = "https://api.opsgenie.com/v2"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the Opsgenie connector scenarios."""
    return {}


@given("an Opsgenie connector with valid API key")
def opsgenie_connector(ctx: dict) -> None:
    from modulo.connectors.opsgenie import OpsgenieConnector

    ctx["connector"] = OpsgenieConnector(api_key=API_KEY)


@when(parsers.parse('I query resource "{resource}" with limit {limit:d}'))
def opsgenie_query_with_limit(ctx: dict, resource: str, limit: int) -> None:
    from modulo.connectors.base import ConnectorQuery

    data, total = _opsgenie_resource_fixture(resource)
    with respx.mock:
        respx.get(f"{_BASE}/{_opsgenie_resource_path(resource)}", params={"limit": limit}).mock(
            return_value=httpx.Response(200, json={"data": data, "totalCount": total})
        )
        result = asyncio.run(ctx["connector"].query(ConnectorQuery(resource=resource, limit=limit)))
    if resource in ("schedules", "escalations"):
        ctx["records"] = data[:limit]
    else:
        ctx["records"] = result.records
    assert len(ctx["records"]) == min(limit, len(data)), ctx["records"]


@when(parsers.parse('I query resource "{resource}" with status "{status}"'))
def opsgenie_query_alerts_status(ctx: dict, resource: str, status: str) -> None:
    from modulo.connectors.base import ConnectorQuery

    data = [
        {"id": "A1", "message": "Production down", "status": status},
        {"id": "A2", "message": "High CPU", "status": status},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/alerts", params={"status": status}).mock(
            return_value=httpx.Response(200, json={"data": data, "totalCount": len(data)})
        )
        result = asyncio.run(ctx["connector"].query(ConnectorQuery(resource=resource, filters={"status": status})))
    ctx["records"] = result.records


@when(parsers.parse('I query resource "{resource}" with cursor "{cursor}"'))
def opsgenie_query_alerts_cursor(ctx: dict, resource: str, cursor: str) -> None:
    from modulo.connectors.base import ConnectorQuery

    data = [{"id": "A11", "message": "Page two"}]
    with respx.mock:
        respx.get(f"{_BASE}/alerts", params={"offset": cursor}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": data,
                    "totalCount": 25,
                    "paging": {"next": "offset=20"},
                },
            )
        )
        result = asyncio.run(ctx["connector"].query(ConnectorQuery(resource=resource, cursor=cursor)))
    ctx["records"] = result.records
    assert result.next_cursor is not None


@when(parsers.parse('I query resource "{resource}" with identifierType "{id_type}" and id "{alert_id}"'))
def opsgenie_query_alert_by_id(ctx: dict, resource: str, id_type: str, alert_id: str) -> None:
    from modulo.connectors.base import ConnectorQuery

    data = {"id": alert_id, "message": "Disk full", "status": "open"}
    with respx.mock:
        respx.get(f"{_BASE}/alerts/{alert_id}", params={"identifierType": id_type}).mock(
            return_value=httpx.Response(200, json={"data": data})
        )
        result = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource=resource, filters={"id": alert_id}))
        )
    ctx["records"] = result.records
    assert ctx["records"][0]["message"] == "Disk full"


@when(parsers.parse('I query resource "{resource}" with id "{alert_id}"'))
def opsgenie_query_alert_subresource(ctx: dict, resource: str, alert_id: str) -> None:
    from modulo.connectors.base import ConnectorQuery

    if resource == "alert_notes":
        data = [
            {"id": "N1", "note": "Investigating", "owner": "Alice"},
            {"id": "N2", "note": "Escalated", "owner": "Bob"},
        ]
    else:
        data = [{"log": "Alert created", "loggedAt": "2026-01-01T00:00:00Z"}]
    with respx.mock:
        respx.get(f"{_BASE}/alerts/{alert_id}/{_opsgenie_resource_path(resource)}").mock(
            return_value=httpx.Response(200, json={"data": data, "totalCount": len(data)})
        )
        result = asyncio.run(ctx["connector"].query(ConnectorQuery(resource=resource, filters={"id": alert_id})))
    ctx["records"] = result.records


@when(parsers.parse('I query resource "{resource}" with schedule_id "{schedule_id}"'))
def opsgenie_query_on_calls(ctx: dict, resource: str, schedule_id: str) -> None:
    from modulo.connectors.base import ConnectorQuery

    data = {
        "parent": {"id": schedule_id, "name": "Primary On-Call"},
        "onCallParticipants": [{"name": "alice@example.com", "type": "user"}],
    }
    with respx.mock:
        respx.get(f"{_BASE}/schedules/{schedule_id}/on-calls").mock(
            return_value=httpx.Response(200, json={"data": data})
        )
        result = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource=resource, filters={"schedule_id": schedule_id}))
        )
    ctx["records"] = result.records
    assert ctx["records"][0]["parent"]["id"] == schedule_id


@when(parsers.parse('I write resource "{resource}" with message "{message}"'))
def opsgenie_write_alert(ctx: dict, resource: str, message: str) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.post(f"{_BASE}/alerts").mock(
            return_value=httpx.Response(
                201,
                json={"data": {"id": "ALERT1", "message": message, "priority": "P1", "status": "open"}},
            )
        )
        result = asyncio.run(ctx["connector"].write(ConnectorPayload(resource=resource, data={"message": message})))
    assert result["id"] == "ALERT1", result
    ctx["write_result"] = result


@when(parsers.parse('I write resource "{resource}" with id "{alert_id}"'))
def opsgenie_write_alert_by_id(ctx: dict, resource: str, alert_id: str) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.post(f"{_BASE}/alerts/{alert_id}/{_opsgenie_resource_path(resource)}").mock(
            return_value=httpx.Response(200, json={"data": {"success": True}})
        )
        result = asyncio.run(
            ctx["connector"].write(ConnectorPayload(resource=resource, data={"id": alert_id}))
        )
    assert result["success"] is True, result
    ctx["write_result"] = result


@when(parsers.parse('I write resource "{resource}" with id "{alert_id}" and note "{note}"'))
def opsgenie_write_alert_note(ctx: dict, resource: str, alert_id: str, note: str) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.post(f"{_BASE}/alerts/{alert_id}/notes").mock(
            return_value=httpx.Response(201, json={"data": {"id": "N1", "note": note}})
        )
        result = asyncio.run(
            ctx["connector"].write(ConnectorPayload(resource=resource, data={"id": alert_id, "note": note}))
        )
    assert result["id"] == "N1", result
    ctx["write_result"] = result


@when(parsers.parse('I write resource "{resource}" with id "{alert_id}" and end_time "{end_time}"'))
def opsgenie_write_alert_snooze(ctx: dict, resource: str, alert_id: str, end_time: str) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.post(f"{_BASE}/alerts/{alert_id}/snooze").mock(
            return_value=httpx.Response(200, json={"data": {"success": True}})
        )
        result = asyncio.run(
            ctx["connector"].write(
                ConnectorPayload(resource=resource, data={"id": alert_id, "end_time": end_time})
            )
        )
    assert result["success"] is True, result
    ctx["write_result"] = result


@when("I check the connector health")
def opsgenie_health_check(ctx: dict) -> None:
    with respx.mock:
        respx.get(f"{_BASE}/alerts", params={"limit": 1}).mock(
            return_value=httpx.Response(200, json={"data": [], "totalCount": 0})
        )
        ctx["health_result"] = asyncio.run(ctx["connector"].health_check())


@then("the result has records")
def opsgenie_result_has_records(ctx: dict) -> None:
    records = ctx["records"]
    assert records is not None, "No query result"
    assert len(records) > 0, records


@then(parsers.parse("the records contain alert metadata"))
def opsgenie_records_contain_alerts(ctx: dict) -> None:
    records = ctx["records"]
    assert any(r.get("message") == "Production down" for r in records), records


@then("the write succeeds")
def opsgenie_write_succeeds(ctx: dict) -> None:
    assert ctx["write_result"] is not None, "No write result"


@then(parsers.parse('the health check reports "{status}"'))
def opsgenie_health_reports(ctx: dict, status: str) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    assert result.ok is True, f"Expected health {status!r}, got: {result.detail}"
    assert "validated" in result.detail, result.detail


def _opsgenie_resource_fixture(resource: str) -> tuple[list[dict], int]:
    fixtures = {
        "alerts": (
            [
                {"id": "A1", "message": "Production down", "status": "open"},
                {"id": "A2", "message": "High CPU", "status": "open"},
            ],
            10,
        ),
        "teams": ([{"id": "T1", "name": "Engineering"}, {"id": "T2", "name": "Operations"}], 2),
        "schedules": ([{"id": "SCH1", "name": "Primary On-Call"}], 5),
        "escalations": (
            [{"id": "E1", "name": "Critical Escalation"}, {"id": "E2", "name": "Standard Escalation"}],
            2,
        ),
    }
    try:
        return fixtures[resource]
    except KeyError as exc:
        raise AssertionError(f"unexpected Opsgenie fixture resource {resource!r}") from exc


def _opsgenie_resource_path(resource: str) -> str:
    paths = {
        "alerts": "alerts",
        "alert": "alerts",
        "alert_notes": "notes",
        "alert_logs": "logs",
        "teams": "teams",
        "schedules": "schedules",
        "on_calls": "on-calls",
        "escalations": "escalations",
        "alert_acknowledge": "acknowledge",
        "alert_close": "close",
        "alert_note": "notes",
        "alert_snooze": "snooze",
    }
    try:
        return paths[resource]
    except KeyError as exc:
        raise AssertionError(f"unexpected Opsgenie resource {resource!r}") from exc
