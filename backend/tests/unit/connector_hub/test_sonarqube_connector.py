"""Unit tests for SonarQubeConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.sonarqube import SonarQubeConnector

TOKEN = "sq_test_token"
_BASE = "http://localhost:9000"
_API = f"{_BASE}/api"


@pytest.fixture
def connector():
    return SonarQubeConnector(token=TOKEN, base_url=_BASE)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.SONARQUBE


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_green(connector):
    respx.get(f"{_API}/system/health").mock(return_value=httpx.Response(200, json={"health": "GREEN"}))
    result = await connector.health_check()
    assert result.ok is True
    assert "GREEN" in result.detail


@respx.mock
async def test_health_check_yellow(connector):
    respx.get(f"{_API}/system/health").mock(return_value=httpx.Response(200, json={"health": "YELLOW"}))
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_red(connector):
    respx.get(f"{_API}/system/health").mock(return_value=httpx.Response(200, json={"health": "RED"}))
    result = await connector.health_check()
    assert result.ok is False
    assert "RED" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_API}/system/health").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_rate_limited(connector):
    respx.get(f"{_API}/system/health").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"}, text="Too Many Requests"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Rate limited" in result.detail
    assert "30" in result.detail


# ---------------------------------------------------------------------------
# query — projects
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_projects(connector):
    body = {"components": [{"key": "my-project", "name": "My Project"}], "paging": {"total": 1}}
    respx.get(f"{_API}/projects/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="projects", limit=10))
    assert result.total == 1
    assert result.records[0]["key"] == "my-project"


@respx.mock
async def test_query_projects_with_search(connector):
    body = {"components": [{"key": "my-project"}], "paging": {"total": 1}}
    respx.get(f"{_API}/projects/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="projects", filters={"search": "my"}, limit=10))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — project_analyses
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_project_analyses(connector):
    body = {"analyses": [{"key": "a1"}], "paging": {"total": 1}}
    respx.get(f"{_API}/project_analyses/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="project_analyses", filters={"project": "my-project"}, limit=10),
    )
    assert result.total == 1


async def test_query_project_analyses_missing_project(connector):
    with pytest.raises(ValueError, match="'project' filter"):
        await connector.query(ConnectorQuery(resource="project_analyses"))


# ---------------------------------------------------------------------------
# query — measures
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_measures(connector):
    body = {"component": {"measures": [{"metric": "coverage", "value": "80.0"}]}}
    respx.get(f"{_API}/measures/component").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(
            resource="measures",
            filters={"component": "my-project", "metricKeys": "coverage,bugs"},
        ),
    )
    assert result.total == 1
    assert result.records[0]["metric"] == "coverage"


async def test_query_measures_missing_filters(connector):
    with pytest.raises(ValueError, match="'component' filter"):
        await connector.query(ConnectorQuery(resource="measures", filters={"metricKeys": "coverage"}))
    with pytest.raises(ValueError, match="'metricKeys' filter"):
        await connector.query(ConnectorQuery(resource="measures", filters={"component": "my-project"}))


# ---------------------------------------------------------------------------
# query — issues
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issues(connector):
    body = {"issues": [{"key": "ISSUE1", "rule": "S107"}], "paging": {"total": 1}}
    respx.get(f"{_API}/issues/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="issues", filters={"component": "my-project"}, limit=10),
    )
    assert result.total == 1
    assert result.records[0]["key"] == "ISSUE1"


# ---------------------------------------------------------------------------
# query — quality gates
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_quality_gates(connector):
    body = {"qualitygates": [{"id": 1, "name": "Strict"}]}
    respx.get(f"{_API}/qualitygates/list").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="quality_gates", limit=10))
    assert result.total == 1


@respx.mock
async def test_query_quality_gate(connector):
    body = {"id": 1, "name": "Strict"}
    respx.get(f"{_API}/qualitygates/show").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="quality_gate", filters={"id": 1}))
    assert result.records[0]["id"] == 1


async def test_query_quality_gate_missing_id(connector):
    with pytest.raises(ValueError, match="'id' filter"):
        await connector.query(ConnectorQuery(resource="quality_gate"))


# ---------------------------------------------------------------------------
# query — metrics / plugins
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_metrics(connector):
    body = {"metrics": [{"key": "coverage"}], "paging": {"total": 1}}
    respx.get(f"{_API}/metrics/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="metrics", limit=10))
    assert result.total == 1


@respx.mock
async def test_query_plugins(connector):
    body = {"plugins": [{"key": "python", "name": "Python"}], "paging": {"total": 1}}
    respx.get(f"{_API}/plugins/installed").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="plugins", limit=10))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — hotspots
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_hotspots(connector):
    body = {"hotspots": [{"key": "H1"}], "paging": {"total": 1}}
    respx.get(f"{_API}/hotspots/search").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="hotspots", filters={"project": "my-project"}, limit=10),
    )
    assert result.total == 1


async def test_query_hotspots_missing_project(connector):
    with pytest.raises(ValueError, match="'project' filter"):
        await connector.query(ConnectorQuery(resource="hotspots"))


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported SonarQube resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — issue_comment
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_issue_comment(connector):
    respx.post(f"{_API}/issues/add_comment").mock(return_value=httpx.Response(200, json={"key": "C1"}))
    result = await connector.write(
        ConnectorPayload(resource="issue_comment", data={"issue": "ISSUE1", "text": "Looking into this"}),
    )
    assert result["key"] == "C1"


async def test_write_issue_comment_missing_fields(connector):
    with pytest.raises(ValueError, match="'issue' and 'text' in data"):
        await connector.write(ConnectorPayload(resource="issue_comment", data={"issue": "ISSUE1"}))


# ---------------------------------------------------------------------------
# write — issue_status
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_issue_status(connector):
    respx.post(f"{_API}/issues/do_transition").mock(return_value=httpx.Response(200, json={"key": "ISSUE1"}))
    result = await connector.write(
        ConnectorPayload(resource="issue_status", data={"issue": "ISSUE1", "transition": "resolve"}),
    )
    assert result["key"] == "ISSUE1"


async def test_write_issue_status_invalid_transition(connector):
    with pytest.raises(ValueError, match="Invalid SonarQube transition"):
        await connector.write(
            ConnectorPayload(resource="issue_status", data={"issue": "ISSUE1", "transition": "bogus"}),
        )


async def test_write_issue_status_missing_fields(connector):
    with pytest.raises(ValueError, match="'issue' and 'transition' in data"):
        await connector.write(ConnectorPayload(resource="issue_status", data={"issue": "ISSUE1"}))


# ---------------------------------------------------------------------------
# write — gate
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_gate(connector):
    respx.post(f"{_API}/qualitygates/create").mock(return_value=httpx.Response(200, json={"id": 5}))
    result = await connector.write(
        ConnectorPayload(resource="gate", data={"name": "Strict Gate"}),
    )
    assert result["id"] == 5


async def test_write_gate_missing_name(connector):
    with pytest.raises(ValueError, match="'name' in data"):
        await connector.write(ConnectorPayload(resource="gate", data={}))


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported SonarQube write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_API}/projects/search").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="projects", limit=10))
