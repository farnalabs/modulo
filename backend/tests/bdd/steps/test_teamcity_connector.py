"""Step definitions for the TeamCity connector BDD feature.

Wires ``features/connectors/teamcity_connector.feature`` into the executing
suite (the improve-architecture product-map walk) by driving the REAL
``TeamCityConnector`` against a respx-mocked TeamCity REST API — mirroring
``backend/tests/unit/connectors/test_teamcity.py`` so the executing BDD surface
locks the same contract the unit suite does: query projects / buildTypes /
agents, trigger a build (``buildQueue``), create a build type, and fail closed
on an unsupported query resource.
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/teamcity_connector.feature")

_BASE = "http://teamcity.example.com"
_TOKEN = "secret"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the TeamCity connector scenarios."""
    return {}


@given("a TeamCity connector with valid token")
def teamcity_connector(ctx: dict) -> None:
    from modulo.connectors.teamcity import TeamCityConnector

    ctx["connector"] = TeamCityConnector(token=_TOKEN, base_url=_BASE)


@when(parsers.parse('I query resource "{resource}"'))
def teamcity_query_resource(ctx: dict, resource: str) -> None:
    from modulo.connectors.base import ConnectorQuery

    connector = ctx["connector"]
    if resource == "projects":
        with respx.mock:
            respx.get(f"{_BASE}/app/rest/projects").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "project": [
                            {"id": "ProjectA", "name": "Project A"},
                            {"id": "ProjectB", "name": "Project B"},
                        ]
                    },
                )
            )
            result = asyncio.run(connector.query(ConnectorQuery(resource="projects", filters={})))
        ctx["records"] = result.records
    elif resource == "agents":
        with respx.mock:
            respx.get(f"{_BASE}/app/rest/agents").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "agent": [
                            {"name": "agent-1", "connected": True},
                            {"name": "agent-2", "connected": False},
                        ]
                    },
                )
            )
            result = asyncio.run(connector.query(ConnectorQuery(resource="agents", filters={})))
        ctx["records"] = result.records
    else:
        try:
            asyncio.run(connector.query(ConnectorQuery(resource=resource, filters={})))
        except ValueError as exc:
            ctx["error"] = exc
            return
        raise AssertionError(f"expected ValueError for unsupported query resource {resource!r}")


@when(parsers.parse('I query resource "{resource}" with filters project_id "{project_id}"'))
def teamcity_query_resource_with_filters(ctx: dict, resource: str, project_id: str) -> None:
    from modulo.connectors.base import ConnectorQuery

    connector = ctx["connector"]
    with respx.mock:
        respx.get(f"{_BASE}/app/rest/buildTypes").mock(
            return_value=httpx.Response(
                200,
                json={
                    "buildType": [
                        {"id": "BT1", "name": "Build Type 1", "projectId": project_id},
                    ]
                },
            )
        )
        result = asyncio.run(
            connector.query(ConnectorQuery(resource=resource, filters={"project_id": project_id}))
        )
    ctx["records"] = result.records


@when(parsers.parse('I write resource "{resource}" with buildTypeId "{build_type_id}"'))
def teamcity_write_build(ctx: dict, resource: str, build_type_id: str) -> None:
    from modulo.connectors.base import ConnectorPayload

    connector = ctx["connector"]
    with respx.mock:
        respx.post(f"{_BASE}/app/rest/buildQueue").mock(
            return_value=httpx.Response(200, json={"id": 42, "buildTypeId": build_type_id})
        )
        result = asyncio.run(
            connector.write(ConnectorPayload(resource=resource, data={"buildTypeId": build_type_id}))
        )
    assert result["id"] == "42", result
    assert result["buildTypeId"] == build_type_id, result
    ctx["write_result"] = result


@when(
    parsers.parse(
        'I write resource "{resource}" with buildTypeId "{build_type_id}", projectId "{project_id}", '
        'and name "{name}"'
    )
)
def teamcity_write_build_type(ctx: dict, resource: str, build_type_id: str, project_id: str, name: str) -> None:
    from modulo.connectors.base import ConnectorPayload

    connector = ctx["connector"]
    with respx.mock:
        respx.post(f"{_BASE}/app/rest/buildTypes").mock(
            return_value=httpx.Response(200, json={"id": build_type_id, "name": name})
        )
        result = asyncio.run(
            connector.write(
                ConnectorPayload(
                    resource=resource,
                    data={"buildTypeId": build_type_id, "projectId": project_id, "name": name},
                )
            )
        )
    assert result["id"] == build_type_id, result
    assert result["name"] == name, result
    ctx["write_result"] = result


@then("the result has records")
def teamcity_result_has_records(ctx: dict) -> None:
    records = ctx["records"]
    assert records is not None, "No query result"
    assert len(records) > 0, records


@then(parsers.parse("the records contain project metadata"))
def teamcity_records_contain_projects(ctx: dict) -> None:
    records = ctx["records"]
    assert any(r.get("id") == "ProjectA" for r in records), records
    assert any(r.get("name") == "Project B" for r in records), records


@then(parsers.parse("the records contain agent metadata"))
def teamcity_records_contain_agents(ctx: dict) -> None:
    records = ctx["records"]
    assert any(r.get("name") == "agent-1" and r.get("connected") is True for r in records), records
    assert any(r.get("name") == "agent-2" and r.get("connected") is False for r in records), records


@then("the write succeeds")
def teamcity_write_succeeds(ctx: dict) -> None:
    assert ctx["write_result"] is not None, "No write result"


@then("the result is an error")
def teamcity_result_is_error(ctx: dict) -> None:
    assert ctx.get("error") is not None, "Expected an error from the connector"
