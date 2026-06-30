"""Unit tests for TrivyConnector — HTTP responses are mocked via respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.trivy import TrivyConnector

TOKEN = "test_token"
BASE_URL = "http://localhost:8080"


@pytest.fixture()
def connector():
    return TrivyConnector(token=TOKEN, base_url=BASE_URL)


@pytest.fixture()
def connector_noauth():
    return TrivyConnector(base_url=BASE_URL)


# --- connector_type ---


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.TRIVY


def test_connector_type_capabilities():
    caps = ConnectorType.TRIVY.capabilities
    assert "read" in caps
    assert "vulnerability_scanning" in caps
    assert "monitoring" in caps


# --- health_check ---


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{BASE_URL}/trivy/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"}),
    )
    result = await connector.health_check()
    assert result.ok is True
    assert "healthy" in result.detail


@respx.mock
async def test_health_check_unauthorized(connector):
    respx.get(f"{BASE_URL}/trivy/v1/health").mock(
        return_value=httpx.Response(401, text="Unauthorized"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid" in result.detail


@respx.mock
async def test_health_check_forbidden(connector):
    respx.get(f"{BASE_URL}/trivy/v1/health").mock(
        return_value=httpx.Response(403, text="Forbidden"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "permissions" in result.detail


@respx.mock
async def test_health_check_connection_error(connector):
    respx.get(f"{BASE_URL}/trivy/v1/health").mock(
        side_effect=httpx.ConnectError("connection refused"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Cannot connect" in result.detail


@respx.mock
async def test_health_check_timeout(connector):
    respx.get(f"{BASE_URL}/trivy/v1/health").mock(
        side_effect=httpx.TimeoutException("timed out"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "timed out" in result.detail


@respx.mock
async def test_health_check_generic_error(connector):
    respx.get(f"{BASE_URL}/trivy/v1/health").mock(
        side_effect=ValueError("weird error"),
    )
    result = await connector.health_check()
    assert result.ok is False


@respx.mock
async def test_health_check_other_status(connector):
    respx.get(f"{BASE_URL}/trivy/v1/health").mock(
        return_value=httpx.Response(500, text="Internal Server Error"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_no_auth(connector_noauth):
    respx.get(f"{BASE_URL}/trivy/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"}),
    )
    result = await connector_noauth.health_check()
    assert result.ok is True


# --- query: artifact with image ---


@respx.mock
async def test_query_artifact_image(connector):
    respx.post(f"{BASE_URL}/trivy/v1/artifact").mock(
        return_value=httpx.Response(
            200,
            json={
                "Results": [
                    {"Target": "alpine:3.18", "Vulnerabilities": [{"VulnerabilityID": "CVE-2023-0001"}]},
                ],
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="artifact", filters={"image": "alpine:3.18"})
    )
    assert len(result.records) > 0
    assert result.records[0]["Results"][0]["Target"] == "alpine:3.18"


@respx.mock
async def test_query_artifact_with_scan_options(connector):
    respx.post(f"{BASE_URL}/trivy/v1/artifact").mock(
        return_value=httpx.Response(
            200,
            json={"Results": [{"Target": "alpine:3.18", "Vulnerabilities": []}]},
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="artifact",
            filters={"image": "alpine:3.18", "scan_options": ["--severity", "CRITICAL"]},
        )
    )
    assert len(result.records) == 1


# --- query: artifact with filesystem ---


@respx.mock
async def test_query_artifact_filesystem(connector):
    respx.post(f"{BASE_URL}/trivy/v1/artifact").mock(
        return_value=httpx.Response(
            200,
            json={"Results": [{"Target": "/app", "Vulnerabilities": []}]},
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="artifact", filters={"filesystem": "/app"})
    )
    assert len(result.records) == 1
    assert result.records[0]["Results"][0]["Target"] == "/app"


# --- query: artifact with repository ---


@respx.mock
async def test_query_artifact_repository(connector):
    respx.post(f"{BASE_URL}/trivy/v1/artifact").mock(
        return_value=httpx.Response(
            200,
            json={"Results": [{"Target": "github.com/aquasecurity/trivy"}]},
        ),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="artifact",
            filters={"repository": "https://github.com/aquasecurity/trivy"},
        )
    )
    assert len(result.records) == 1


# --- query: artifact missing target ---


@respx.mock
async def test_query_artifact_missing_target(connector):
    with pytest.raises(ValueError, match="requires one of"):
        await connector.query(ConnectorQuery(resource="artifact"))


# --- query: reports ---


@respx.mock
async def test_query_reports(connector):
    respx.get(f"{BASE_URL}/trivy/v1/reports").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"digest": "sha256:abc", "ArtifactName": "alpine:3.18"},
                {"digest": "sha256:def", "ArtifactName": "ubuntu:22.04"},
            ],
        ),
    )
    result = await connector.query(ConnectorQuery(resource="reports", limit=10))
    assert len(result.records) == 2
    assert result.total == 2


@respx.mock
async def test_query_reports_with_cursor(connector):
    respx.get(f"{BASE_URL}/trivy/v1/reports", params={"cursor": "next-token"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"digest": "sha256:ghi", "ArtifactName": "debian:11"}],
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="reports", limit=100, cursor="next-token")
    )
    assert len(result.records) == 1


# --- query: report by digest ---


@respx.mock
async def test_query_report(connector):
    respx.get(f"{BASE_URL}/trivy/v1/reports/sha256:abc123").mock(
        return_value=httpx.Response(
            200,
            json={
                "digest": "sha256:abc123",
                "ArtifactName": "alpine:3.18",
                "Results": [{"Vulnerabilities": []}],
            },
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="report", filters={"digest": "sha256:abc123"})
    )
    assert len(result.records) == 1
    assert result.records[0]["digest"] == "sha256:abc123"


@respx.mock
async def test_query_report_not_found(connector):
    respx.get(f"{BASE_URL}/trivy/v1/reports/sha256:nonexistent").mock(
        return_value=httpx.Response(200, json={}),
    )
    result = await connector.query(
        ConnectorQuery(resource="report", filters={"digest": "sha256:nonexistent"})
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_report_missing_digest(connector):
    with pytest.raises(ValueError, match="requires 'digest' in filters"):
        await connector.query(ConnectorQuery(resource="report"))


# --- query: status ---


@respx.mock
async def test_query_status(connector):
    respx.get(f"{BASE_URL}/trivy/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"}),
    )
    result = await connector.query(ConnectorQuery(resource="status"))
    assert len(result.records) == 1
    assert result.records[0]["status"] == "ok"


# --- query: plugins ---


@respx.mock
async def test_query_plugins(connector):
    respx.get(f"{BASE_URL}/trivy/v1/plugins").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "plugin-a", "version": "1.0.0"},
                {"name": "plugin-b", "version": "2.0.0"},
            ],
        ),
    )
    result = await connector.query(ConnectorQuery(resource="plugins"))
    assert len(result.records) == 2
    assert result.total == 2


@respx.mock
async def test_query_plugins_empty(connector):
    respx.get(f"{BASE_URL}/trivy/v1/plugins").mock(
        return_value=httpx.Response(200, json=[]),
    )
    result = await connector.query(ConnectorQuery(resource="plugins"))
    assert len(result.records) == 0
    assert result.total == 0


# --- query: unsupported resource ---


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Trivy resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


# --- write: scan with image ---


@respx.mock
async def test_write_scan_image(connector):
    respx.post(f"{BASE_URL}/trivy/v1/artifact").mock(
        return_value=httpx.Response(
            200,
            json={"Results": [{"Target": "alpine:3.18", "Vulnerabilities": []}]},
        ),
    )
    result = await connector.write(
        ConnectorPayload(resource="scan", data={"image": "alpine:3.18"})
    )
    assert "Results" in result
    assert result["Results"][0]["Target"] == "alpine:3.18"


@respx.mock
async def test_write_scan_image_with_options(connector):
    respx.post(f"{BASE_URL}/trivy/v1/artifact").mock(
        return_value=httpx.Response(
            200,
            json={"Results": [{"Target": "alpine:3.18", "Vulnerabilities": []}]},
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="scan",
            data={
                "image": "alpine:3.18",
                "scan_options": ["--severity", "CRITICAL", "--scanners", "vuln"],
            },
        )
    )
    assert result["Results"][0]["Target"] == "alpine:3.18"


# --- write: scan with filesystem ---


@respx.mock
async def test_write_scan_filesystem(connector):
    respx.post(f"{BASE_URL}/trivy/v1/artifact").mock(
        return_value=httpx.Response(
            200,
            json={"Results": [{"Target": "/app", "Vulnerabilities": []}]},
        ),
    )
    result = await connector.write(
        ConnectorPayload(resource="scan", data={"filesystem": "/app"})
    )
    assert result["Results"][0]["Target"] == "/app"


# --- write: scan with repository ---


@respx.mock
async def test_write_scan_repository(connector):
    respx.post(f"{BASE_URL}/trivy/v1/artifact").mock(
        return_value=httpx.Response(
            200,
            json={"Results": [{"Target": "github.com/org/repo"}]},
        ),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="scan",
            data={"repository": "https://github.com/org/repo"},
        )
    )
    assert "Results" in result


# --- write: scan missing target ---


@respx.mock
async def test_write_scan_missing_target(connector):
    with pytest.raises(ValueError, match="requires one of"):
        await connector.write(ConnectorPayload(resource="scan", data={}))


# --- write: unsupported resource ---


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Trivy write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# --- default constructor (no auth) ---


def test_constructor_defaults():
    c = TrivyConnector()
    assert c._token == ""
    assert c._base_url == "http://localhost:8080"


def test_constructor_custom_base():
    c = TrivyConnector(base_url="https://trivy.internal:9090")
    assert c._base_url == "https://trivy.internal:9090"


def test_constructor_removes_trailing_slash():
    c = TrivyConnector(base_url="http://localhost:8080/")
    assert c._base_url == "http://localhost:8080"
