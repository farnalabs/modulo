"""Step definitions for the Grafana connector BDD feature.

Wires ``features/connectors/grafana.feature`` into the executing suite (the
2026-09-15 improve-architecture product-map walk) by driving the REAL
``GrafanaConnector`` against a respx-mocked Grafana API — mirroring
``backend/tests/unit/connectors/test_grafana.py`` so the executing BDD surface
locks the same contract the unit suite does: token validation via ``/api/health``,
list dashboards / dashboard-by-uid / alert rules / datasources queries, and
annotation creation writes.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/grafana.feature")

TOKEN = "glc_test_token"
_BASE = "http://localhost:3000"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the Grafana connector scenarios."""
    return {}


@given("a Grafana connector configured with valid credentials")
def grafana_connector_valid(ctx: dict) -> None:
    from modulo.connectors.grafana import GrafanaConnector

    ctx["connector"] = GrafanaConnector(token=TOKEN)
    ctx["valid"] = True


@given("a Grafana connector configured with invalid credentials")
def grafana_connector_invalid(ctx: dict) -> None:
    from modulo.connectors.grafana import GrafanaConnector

    ctx["connector"] = GrafanaConnector(token=TOKEN)
    ctx["valid"] = False


@when("the connector checks health")
def grafana_health_check(ctx: dict) -> None:
    connector = ctx["connector"]
    response = (
        httpx.Response(200, json={"commit": "abc123"}) if ctx["valid"] else httpx.Response(401, text="Unauthorized")
    )
    with respx.mock:
        respx.get(f"{_BASE}/api/health").mock(return_value=response)
        ctx["health_result"] = asyncio.run(connector.health_check())


@then(parsers.parse('the health check returns "{status}"'))
def grafana_health_result(status: str, ctx: dict) -> None:
    result = ctx["health_result"]
    assert result is not None, "No health check result"
    if status == "healthy":
        assert result.ok is True, f"Expected healthy, got: {result.detail}"
    else:
        assert result.ok is False, f"Expected unhealthy, got: {result.detail}"


@when("the connector queries dashboards")
def grafana_query_dashboards(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    dashboards = [
        {"uid": "d1", "title": "System Dashboard", "type": "dash-db"},
        {"uid": "d2", "title": "API Monitoring", "type": "dash-db"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/api/search", params={"type": "dash-db"}).mock(
            return_value=httpx.Response(200, json=dashboards)
        )
        ctx["query_result"] = asyncio.run(ctx["connector"].query(ConnectorQuery(resource="dashboards")))


@then("the result contains Grafana dashboards")
def grafana_result_contains_dashboards(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected dashboard records"
    assert result.records[0]["title"] == "System Dashboard", result.records[0]


@when(parsers.parse('the connector queries dashboard with uid "{uid}"'))
def grafana_query_dashboard_by_uid(uid: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    dashboard = {
        "dashboard": {"uid": uid, "title": "My Dashboard"},
        "meta": {"slug": "my-dashboard"},
    }
    with respx.mock:
        respx.get(f"{_BASE}/api/dashboards/uid/{uid}").mock(return_value=httpx.Response(200, json=dashboard))
        ctx["query_result"] = asyncio.run(
            ctx["connector"].query(ConnectorQuery(resource="dashboard", filters={"uid": uid}))
        )


@then("the result contains the Grafana dashboard")
def grafana_result_contains_dashboard(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected dashboard record"
    assert result.records[0]["dashboard"]["uid"] == "abc123", result.records[0]


@when("the connector queries alert rules")
def grafana_query_alert_rules(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    rules = [
        {"uid": "r1", "name": "High CPU Rule"},
        {"uid": "r2", "name": "Memory Rule"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/api/v1/provisioning/alert-rules").mock(return_value=httpx.Response(200, json=rules))
        ctx["query_result"] = asyncio.run(ctx["connector"].query(ConnectorQuery(resource="alert_rules")))


@then("the result contains Grafana alert rules")
def grafana_result_contains_alert_rules(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected alert rule records"
    assert result.records[0]["uid"] == "r1", result.records[0]


@when("the connector queries datasources")
def grafana_query_datasources(ctx: dict) -> None:
    from modulo.connectors.base import ConnectorQuery

    datasources = [
        {"id": 1, "name": "Prometheus", "type": "prometheus"},
        {"id": 2, "name": "Loki", "type": "loki"},
    ]
    with respx.mock:
        respx.get(f"{_BASE}/api/datasources").mock(return_value=httpx.Response(200, json=datasources))
        ctx["query_result"] = asyncio.run(ctx["connector"].query(ConnectorQuery(resource="datasources")))


@then("the result contains Grafana datasources")
def grafana_result_contains_datasources(ctx: dict) -> None:
    result = ctx["query_result"]
    assert result is not None, "No query result"
    assert result.records, "Expected datasource records"
    assert result.records[0]["name"] == "Prometheus", result.records[0]


@when(parsers.parse('the connector creates an annotation with text "{text}"'))
def grafana_write_annotation(text: str, ctx: dict) -> None:
    from modulo.connectors.base import ConnectorPayload

    with respx.mock:
        respx.post(f"{_BASE}/api/annotations").mock(return_value=httpx.Response(200, json={"id": 42, "text": text}))
        ctx["write_result"] = asyncio.run(
            ctx["connector"].write(ConnectorPayload(resource="annotation", data={"text": text}))
        )


@then("the annotation is created successfully")
def grafana_annotation_created(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result["text"] == "Deploy completed", result
