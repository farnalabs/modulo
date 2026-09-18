"""Step definitions for the PagerDuty connector BDD feature.

Wires ``features/connectors/pagerduty.feature`` into the executing suite (the
2026-09-14 improve-architecture product-map walk) by driving the REAL
``PagerDutyConnector`` against a respx-mocked PagerDuty API — mirroring
``backend/tests/unit/connectors/test_pagerduty.py`` so the executing BDD surface
locks the same contract the unit suite does: token validation via ``/users``,
list incidents/services, and trigger/acknowledge/resolve incident writes.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/pagerduty.feature")

_TOKEN = "pd_test_token"
_BASE = "https://api.pagerduty.com"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the PagerDuty connector scenarios."""
    return {}


@given("a PagerDuty connector configured with valid credentials")
def pagerduty_connector_valid(ctx: dict) -> None:
    from modulo.connectors.pagerduty import PagerDutyConnector

    ctx["connector"] = PagerDutyConnector(token=_TOKEN)
    ctx["valid"] = True


@given("a PagerDuty connector configured with invalid credentials")
def pagerduty_connector_invalid(ctx: dict) -> None:
    from modulo.connectors.pagerduty import PagerDutyConnector

    ctx["connector"] = PagerDutyConnector(token=_TOKEN)
    ctx["valid"] = False


@when("the connector checks health")
def pagerduty_health_check(ctx: dict) -> None:
    connector = ctx["connector"]
    response = (
        httpx.Response(200, json={"users": [{"id": "U1"}]})
        if ctx["valid"]
        else httpx.Response(401, text="Unauthorized")
    )
    with respx.mock:
        respx.get(f"{_BASE}/users", params={"limit": 1}).mock(return_value=response)
        ctx["health_result"] = asyncio.run(connector.health_check())


@when("the connector queries incidents")
def pagerduty_query_incidents(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    incidents = [
        {"id": "I1", "title": "Production outage", "status": "triggered"},
        {"id": "I2", "title": "Degraded performance", "status": "acknowledged"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/incidents").mock(
            return_value=httpx.Response(200, json={"incidents": incidents, "total": 2, "more": False})
        )
        ctx["query_result"] = asyncio.run(ctx["connector"].query(ConnectorQuery(resource="incidents")))


@when("the connector queries services")
def pagerduty_query_services(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    services = [
        {"id": "S1", "name": "Web API", "status": "active"},
        {"id": "S2", "name": "Database", "status": "active"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/services").mock(
            return_value=httpx.Response(200, json={"services": services, "total": 2, "more": False})
        )
        ctx["query_result"] = asyncio.run(ctx["connector"].query(ConnectorQuery(resource="services")))


@when(parsers.parse('the connector triggers an incident with title "{title}" and service "{service}"'))
def pagerduty_write_trigger_incident(title: str, service: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.post(f"{_BASE}/incidents").mock(
            return_value=httpx.Response(201, json={"incident": {"id": "INC1", "title": title, "status": "triggered"}})
        )
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(ConnectorPayload(resource="incident", data={"title": title, "service_id": service}))
        )


@when(parsers.parse('the connector acknowledges incident "{incident_id}"'))
def pagerduty_write_acknowledge(incident_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.put(f"{_BASE}/incidents/{incident_id}").mock(
            return_value=httpx.Response(200, json={"incident": {"id": incident_id, "status": "acknowledged"}})
        )
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(ConnectorPayload(resource="incident_acknowledge", data={"incident_id": incident_id}))
        )


@when(parsers.parse('the connector resolves incident "{incident_id}"'))
def pagerduty_write_resolve(incident_id: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.put(f"{_BASE}/incidents/{incident_id}").mock(
            return_value=httpx.Response(200, json={"incident": {"id": incident_id, "status": "resolved"}})
        )
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(ConnectorPayload(resource="incident_resolve", data={"incident_id": incident_id}))
        )


@then(parsers.parse('the health check returns "{status}"'))
def pagerduty_health_result(status: str, ctx: dict) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    if status == "healthy":
        assert result.ok is True, f"Expected healthy, got: {result.detail}"
    else:
        assert result.ok is False, f"Expected unhealthy, got: {result.detail}"


@then("the result contains PagerDuty incidents")
def pagerduty_result_contains_incidents(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected incident records"
    assert result.records[0]["title"] == "Production outage", result.records[0]
    assert result.records[0]["id"] == "I1", result.records[0]


@then("the result contains PagerDuty services")
def pagerduty_result_contains_services(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected service records"
    assert result.records[0]["name"] == "Web API", result.records[0]
    assert result.records[0]["id"] == "S1", result.records[0]


@then("the incident is triggered successfully")
def pagerduty_incident_triggered(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["status"] == "triggered", result
    assert result["title"] == "Test incident", result


@then("the incident is acknowledged")
def pagerduty_incident_acknowledged(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["status"] == "acknowledged", result


@then("the incident is resolved")
def pagerduty_incident_resolved(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["status"] == "resolved", result
