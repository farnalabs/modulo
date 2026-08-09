"""Unit tests for TrivyConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.trivy import TrivyConnector

TOKEN = "trivy_test_token"
_BASE = "http://localhost:8080"


@pytest.fixture
def connector():
    return TrivyConnector(token=TOKEN, base_url=_BASE)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.TRIVY


def test_base_url_trailing_slash_stripped():
    c = TrivyConnector(token=TOKEN, base_url=f"{_BASE}/")
    assert c._base_url == _BASE


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/trivy/v1/health").mock(return_value=httpx.Response(200, text="healthy"))
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/trivy/v1/health").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Trivy auth token" in result.detail


@respx.mock
async def test_health_check_forbidden(connector):
    respx.get(f"{_BASE}/trivy/v1/health").mock(return_value=httpx.Response(403, text="Forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "lacks required permissions" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/trivy/v1/health").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/trivy/v1/health").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Cannot connect to Trivy server" in result.detail


# ---------------------------------------------------------------------------
# query — artifact
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_artifact_image(connector):
    respx.post(f"{_BASE}/trivy/v1/artifact").mock(
        return_value=httpx.Response(200, json=[{"Target": "alpine:3.18", "Vulnerabilities": []}]),
    )
    result = await connector.query(
        ConnectorQuery(resource="artifact", filters={"image": "alpine:3.18"}),
    )
    assert result.total == 1
    assert result.records[0]["Target"] == "alpine:3.18"


@respx.mock
async def test_query_artifact_filesystem(connector):
    respx.post(f"{_BASE}/trivy/v1/artifact").mock(
        return_value=httpx.Response(200, json=[{"Target": "fs", "Vulnerabilities": []}]),
    )
    result = await connector.query(
        ConnectorQuery(resource="artifact", filters={"filesystem": "/"}),
    )
    assert result.total == 1


@respx.mock
async def test_query_artifact_repository(connector):
    respx.post(f"{_BASE}/trivy/v1/artifact").mock(
        return_value=httpx.Response(200, json={"Target": "repo", "Vulnerabilities": []}),
    )
    result = await connector.query(
        ConnectorQuery(resource="artifact", filters={"repository": "https://github.com/aquasecurity/trivy"}),
    )
    assert result.total == 1


@respx.mock
async def test_query_artifact_with_scan_options(connector):
    respx.post(f"{_BASE}/trivy/v1/artifact").mock(
        return_value=httpx.Response(200, json=[{"Target": "alpine:3.18"}]),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="artifact",
            filters={"image": "alpine:3.18", "scan_options": {"vuln-type": "os"}},
        ),
    )
    assert result.total == 1


async def test_query_artifact_missing_target(connector):
    with pytest.raises(ValueError, match="one of 'image', 'filesystem', or 'repository'"):
        await connector.query(ConnectorQuery(resource="artifact"))


# ---------------------------------------------------------------------------
# query — reports / report
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_reports(connector):
    respx.get(f"{_BASE}/trivy/v1/reports").mock(
        return_value=httpx.Response(200, json=[{"artifact": "alpine:3.18"}]),
    )
    result = await connector.query(ConnectorQuery(resource="reports", limit=5))
    assert result.total == 1


@respx.mock
async def test_query_reports_wrapped(connector):
    respx.get(f"{_BASE}/trivy/v1/reports").mock(
        return_value=httpx.Response(200, json={"reports": [{"artifact": "alpine:3.18"}]}),
    )
    result = await connector.query(ConnectorQuery(resource="reports", limit=5))
    assert result.total == 1


@respx.mock
async def test_query_report(connector):
    respx.get(f"{_BASE}/trivy/v1/reports/sha256:abc123").mock(
        return_value=httpx.Response(200, json={"artifact": "alpine:3.18", "digest": "sha256:abc123"}),
    )
    result = await connector.query(ConnectorQuery(resource="report", filters={"digest": "sha256:abc123"}))
    assert result.records[0]["digest"] == "sha256:abc123"


async def test_query_report_missing_digest(connector):
    with pytest.raises(ValueError, match="'digest' in filters"):
        await connector.query(ConnectorQuery(resource="report"))


# ---------------------------------------------------------------------------
# query — status / plugins
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_status(connector):
    respx.get(f"{_BASE}/trivy/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "healthy"}),
    )
    result = await connector.query(ConnectorQuery(resource="status"))
    assert result.records[0]["status"] == "healthy"


@respx.mock
async def test_query_plugins(connector):
    respx.get(f"{_BASE}/trivy/v1/plugins").mock(
        return_value=httpx.Response(200, json=[{"name": "plugin-a"}]),
    )
    result = await connector.query(ConnectorQuery(resource="plugins"))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Trivy resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# ---------------------------------------------------------------------------
# write — scan
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_scan(connector):
    respx.post(f"{_BASE}/trivy/v1/artifact").mock(
        return_value=httpx.Response(200, json=[{"Target": "alpine:3.18"}]),
    )
    result = await connector.write(
        ConnectorPayload(resource="scan", data={"image": "alpine:3.18"}),
    )
    assert result[0]["Target"] == "alpine:3.18"


async def test_write_scan_missing_target(connector):
    with pytest.raises(ValueError, match="one of 'image', 'filesystem', or 'repository'"):
        await connector.write(ConnectorPayload(resource="scan", data={}))


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Trivy write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/trivy/v1/reports").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="reports"))
