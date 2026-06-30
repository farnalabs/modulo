"""Unit tests for SonarQubeConnector — HTTP responses are mocked via respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.sonarqube import SonarQubeConnector

TOKEN = "sqp_test_token"
BASE_URL = "https://sonarqube.company.com"
API_BASE = f"{BASE_URL}/api"


@pytest.fixture()
def connector():
    return SonarQubeConnector(token=TOKEN, base_url=BASE_URL)


@pytest.fixture()
def local_connector():
    return SonarQubeConnector(token=TOKEN)


# --- health_check ---

@respx.mock
async def test_health_check_green(connector):
    respx.get(f"{API_BASE}/system/health").mock(
        return_value=httpx.Response(200, json={"health": "GREEN"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert "GREEN" in result.detail


@respx.mock
async def test_health_check_yellow(connector):
    respx.get(f"{API_BASE}/system/health").mock(
        return_value=httpx.Response(200, json={"health": "YELLOW"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert "YELLOW" in result.detail


@respx.mock
async def test_health_check_red(connector):
    respx.get(f"{API_BASE}/system/health").mock(
        return_value=httpx.Response(200, json={"health": "RED"}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "RED" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{API_BASE}/system/health").mock(
        return_value=httpx.Response(401, text="Unauthorized"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


@respx.mock
async def test_health_check_connection_error(connector):
    respx.get(f"{API_BASE}/system/health").mock(side_effect=httpx.ConnectError("connection refused"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


@respx.mock
async def test_health_check_rate_limited(connector):
    respx.get(f"{API_BASE}/system/health").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "60"}),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Rate limited" in result.detail


@respx.mock
async def test_health_check_localhost_default(local_connector):
    respx.get("http://localhost:9000/api/system/health").mock(
        return_value=httpx.Response(200, json={"health": "GREEN"}),
    )
    result = await local_connector.health_check()
    assert result.ok is True


# --- query: projects ---

@respx.mock
async def test_query_projects(connector):
    projects = [
        {"key": "com.example:my-app", "name": "My App", "qualifier": "TRK", "visibility": "public"},
        {"key": "com.example:other", "name": "Other", "qualifier": "TRK", "visibility": "private"},
    ]
    respx.get(f"{API_BASE}/projects/search").mock(
        return_value=httpx.Response(200, json={
            "components": projects,
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 2},
        }),
    )
    result = await connector.query(ConnectorQuery(resource="projects", limit=100))
    assert len(result.records) == 2
    assert result.records[0]["key"] == "com.example:my-app"
    assert result.total == 2


@respx.mock
async def test_query_projects_with_search(connector):
    respx.get(f"{API_BASE}/projects/search").mock(
        return_value=httpx.Response(200, json={
            "components": [{"key": "com.example:my-app", "name": "My App"}],
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
        }),
    )
    result = await connector.query(ConnectorQuery(
        resource="projects", filters={"search": "my-app"}, limit=100,
    ))
    assert len(result.records) == 1


@respx.mock
async def test_query_projects_empty(connector):
    respx.get(f"{API_BASE}/projects/search").mock(
        return_value=httpx.Response(200, json={
            "components": [],
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 0},
        }),
    )
    result = await connector.query(ConnectorQuery(resource="projects", limit=100))
    assert len(result.records) == 0
    assert result.total == 0


# --- query: project_analyses ---

@respx.mock
async def test_query_project_analyses(connector):
    respx.get(f"{API_BASE}/project_analyses/search").mock(
        return_value=httpx.Response(200, json={
            "analyses": [{"key": "A1", "date": "2024-01-01", "project": "proj1"}],
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
        }),
    )
    result = await connector.query(ConnectorQuery(
        resource="project_analyses", filters={"project": "proj1"}, limit=100,
    ))
    assert len(result.records) == 1
    assert result.records[0]["key"] == "A1"


@respx.mock
async def test_query_project_analyses_missing_project(connector):
    with pytest.raises(ValueError, match="requires 'project' filter"):
        await connector.query(ConnectorQuery(resource="project_analyses"))


# --- query: measures ---

@respx.mock
async def test_query_measures(connector):
    respx.get(f"{API_BASE}/measures/component").mock(
        return_value=httpx.Response(200, json={
            "component": {
                "key": "proj1",
                "measures": [
                    {"metric": "coverage", "value": "85.3"},
                    {"metric": "bugs", "value": "12"},
                ],
            },
        }),
    )
    result = await connector.query(ConnectorQuery(
        resource="measures", filters={"component": "proj1", "metricKeys": "coverage,bugs"},
    ))
    assert len(result.records) == 2
    assert result.records[0]["metric"] == "coverage"


@respx.mock
async def test_query_measures_missing_component(connector):
    with pytest.raises(ValueError, match="requires 'component' filter"):
        await connector.query(ConnectorQuery(resource="measures", filters={"metricKeys": "coverage"}))


@respx.mock
async def test_query_measures_missing_metric_keys(connector):
    with pytest.raises(ValueError, match="requires 'metricKeys' filter"):
        await connector.query(ConnectorQuery(resource="measures", filters={"component": "proj1"}))


# --- query: issues ---

@respx.mock
async def test_query_issues(connector):
    respx.get(f"{API_BASE}/issues/search").mock(
        return_value=httpx.Response(200, json={
            "issues": [{"key": "ISSUE1", "component": "proj1", "status": "OPEN"}],
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
        }),
    )
    result = await connector.query(ConnectorQuery(
        resource="issues", filters={"component": "proj1", "status": "OPEN", "types": "BUG"},
    ))
    assert len(result.records) == 1
    assert result.records[0]["key"] == "ISSUE1"


@respx.mock
async def test_query_issues_with_all_filters(connector):
    respx.get(f"{API_BASE}/issues/search").mock(
        return_value=httpx.Response(200, json={
            "issues": [],
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 0},
        }),
    )
    result = await connector.query(ConnectorQuery(
        resource="issues", filters={
            "component": "proj1", "status": "OPEN", "types": "BUG",
            "severities": "CRITICAL", "resolved": "false",
            "assignee": "admin", "tags": "security",
            "createdAfter": "2024-01-01", "createdBefore": "2024-12-31",
        },
    ))
    assert len(result.records) == 0


# --- query: quality_gates ---

@respx.mock
async def test_query_quality_gates(connector):
    respx.get(f"{API_BASE}/qualitygates/list").mock(
        return_value=httpx.Response(200, json={
            "qualitygates": [
                {"id": 1, "name": "Sonar way"},
                {"id": 2, "name": "My Gate"},
            ],
        }),
    )
    result = await connector.query(ConnectorQuery(resource="quality_gates"))
    assert len(result.records) == 2
    assert result.total == 2


@respx.mock
async def test_query_quality_gates_empty(connector):
    respx.get(f"{API_BASE}/qualitygates/list").mock(
        return_value=httpx.Response(200, json={"qualitygates": []}),
    )
    result = await connector.query(ConnectorQuery(resource="quality_gates"))
    assert len(result.records) == 0


# --- query: quality_gate ---

@respx.mock
async def test_query_quality_gate_by_id(connector):
    respx.get(f"{API_BASE}/qualitygates/show").mock(
        return_value=httpx.Response(200, json={
            "id": 1, "name": "Sonar way",
            "conditions": [{"metric": "coverage", "op": "LT", "error": "80.0"}],
        }),
    )
    result = await connector.query(ConnectorQuery(
        resource="quality_gate", filters={"id": "1"},
    ))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Sonar way"


@respx.mock
async def test_query_quality_gate_missing_id(connector):
    with pytest.raises(ValueError, match="requires 'id' filter"):
        await connector.query(ConnectorQuery(resource="quality_gate"))


# --- query: metrics ---

@respx.mock
async def test_query_metrics(connector):
    respx.get(f"{API_BASE}/metrics/search").mock(
        return_value=httpx.Response(200, json={
            "metrics": [
                {"key": "coverage", "name": "Coverage", "type": "PERCENT"},
                {"key": "bugs", "name": "Bugs", "type": "INT"},
            ],
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 2},
        }),
    )
    result = await connector.query(ConnectorQuery(resource="metrics"))
    assert len(result.records) == 2


# --- query: plugins ---

@respx.mock
async def test_query_plugins(connector):
    respx.get(f"{API_BASE}/plugins/installed").mock(
        return_value=httpx.Response(200, json={
            "plugins": [
                {"key": "python", "name": "Python", "version": "1.0"},
            ],
        }),
    )
    result = await connector.query(ConnectorQuery(resource="plugins"))
    assert len(result.records) == 1
    assert result.records[0]["key"] == "python"


# --- query: hotspots ---

@respx.mock
async def test_query_hotspots(connector):
    respx.get(f"{API_BASE}/hotspots/search").mock(
        return_value=httpx.Response(200, json={
            "hotspots": [{"key": "H1", "component": "proj1", "status": "TO_REVIEW"}],
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
        }),
    )
    result = await connector.query(ConnectorQuery(
        resource="hotspots", filters={"project": "proj1"},
    ))
    assert len(result.records) == 1


@respx.mock
async def test_query_hotspots_missing_project(connector):
    with pytest.raises(ValueError, match="requires 'project' filter"):
        await connector.query(ConnectorQuery(resource="hotspots"))


# --- query: unsupported resource ---

async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported SonarQube resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# --- write: issue_comment ---

@respx.mock
async def test_write_issue_comment(connector):
    respx.post(f"{API_BASE}/issues/add_comment").mock(
        return_value=httpx.Response(200, json={
            "issue": {"key": "ISSUE1"},
        }),
    )
    result = await connector.write(ConnectorPayload(
        resource="issue_comment",
        data={"issue": "ISSUE1", "text": "Looking into this"},
    ))
    assert result["issue"]["key"] == "ISSUE1"


@respx.mock
async def test_write_issue_comment_missing_fields(connector):
    with pytest.raises(ValueError, match="requires 'issue' and 'text'"):
        await connector.write(ConnectorPayload(
            resource="issue_comment", data={"issue": "ISSUE1"},
        ))


# --- write: issue_status ---

@respx.mock
async def test_write_issue_status_confirm(connector):
    respx.post(f"{API_BASE}/issues/do_transition").mock(
        return_value=httpx.Response(200, json={"transition": "confirm"}),
    )
    result = await connector.write(ConnectorPayload(
        resource="issue_status",
        data={"issue": "ISSUE1", "transition": "confirm"},
    ))
    assert result["transition"] == "confirm"


@respx.mock
async def test_write_issue_status_resolve(connector):
    respx.post(f"{API_BASE}/issues/do_transition").mock(
        return_value=httpx.Response(200, json={"transition": "resolve"}),
    )
    result = await connector.write(ConnectorPayload(
        resource="issue_status",
        data={"issue": "ISSUE1", "transition": "resolve"},
    ))
    assert result["transition"] == "resolve"


@respx.mock
async def test_write_issue_status_invalid_transition(connector):
    with pytest.raises(ValueError, match="Invalid SonarQube transition"):
        await connector.write(ConnectorPayload(
            resource="issue_status",
            data={"issue": "ISSUE1", "transition": "invalid"},
        ))


async def test_write_issue_status_missing_fields(connector):
    with pytest.raises(ValueError, match="requires 'issue' and 'transition'"):
        await connector.write(ConnectorPayload(
            resource="issue_status", data={"issue": "ISSUE1"},
        ))


# --- write: gate ---

@respx.mock
async def test_write_create_quality_gate(connector):
    respx.post(f"{API_BASE}/qualitygates/create").mock(
        return_value=httpx.Response(200, json={"id": 10, "name": "Strict Gate"}),
    )
    result = await connector.write(ConnectorPayload(
        resource="gate", data={"name": "Strict Gate"},
    ))
    assert result["name"] == "Strict Gate"
    assert result["id"] == 10


async def test_write_create_quality_gate_missing_name(connector):
    with pytest.raises(ValueError, match="requires 'name' in data"):
        await connector.write(ConnectorPayload(
            resource="gate", data={},
        ))


# --- write: unsupported resource ---

async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported SonarQube write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# --- connector_type ---

def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.SONARQUBE
