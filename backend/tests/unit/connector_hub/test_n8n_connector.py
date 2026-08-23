"""Unit tests for N8NConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.n8n import N8NConnector

TOKEN = "n8n_test_token"
_BASE = "http://localhost:5678"


@pytest.fixture
def connector():
    return N8NConnector(token=TOKEN, base_url=_BASE)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.N8N


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/rest/workflows").mock(return_value=httpx.Response(200, json={"data": []}))
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/rest/workflows").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid n8n API token" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_BASE}/rest/workflows").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Cannot connect to n8n" in result.detail


# ---------------------------------------------------------------------------
# query — workflows
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_workflows(connector):
    workflows = [{"id": "W1", "name": "Test Workflow", "active": True}]
    respx.get(f"{_BASE}/rest/workflows").mock(return_value=httpx.Response(200, json={"data": workflows}))
    result = await connector.query(ConnectorQuery(resource="workflows", limit=5))
    assert result.total == 1
    assert result.records[0]["id"] == "W1"


@respx.mock
async def test_query_workflows_with_active_filter(connector):
    workflows = [{"id": "W2", "name": "Active WF", "active": True}]
    respx.get(f"{_BASE}/rest/workflows").mock(return_value=httpx.Response(200, json={"data": workflows}))
    result = await connector.query(
        ConnectorQuery(resource="workflows", filters={"active": "true"}, limit=10),
    )
    assert result.total == 1


@respx.mock
async def test_query_workflows_with_cursor(connector):
    workflows = [{"id": "W3"}]
    respx.get(f"{_BASE}/rest/workflows").mock(
        return_value=httpx.Response(200, json={"data": workflows, "nextCursor": "abc"}),
    )
    result = await connector.query(ConnectorQuery(resource="workflows", cursor="prev"))
    assert result.next_cursor == "abc"


@respx.mock
async def test_query_workflow(connector):
    workflow = {"id": "W1", "name": "Test Workflow"}
    respx.get(f"{_BASE}/rest/workflows/W1").mock(return_value=httpx.Response(200, json={"data": workflow}))
    result = await connector.query(ConnectorQuery(resource="workflow", filters={"id": "W1"}))
    assert result.records[0]["id"] == "W1"


async def test_query_workflow_missing_id(connector):
    query = ConnectorQuery(resource="workflow")
    with pytest.raises(ValueError, match="'id' filter"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — executions
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_executions(connector):
    executions = [{"id": "E1", "status": "success"}]
    respx.get(f"{_BASE}/rest/executions").mock(return_value=httpx.Response(200, json={"data": executions}))
    result = await connector.query(
        ConnectorQuery(resource="executions", filters={"status": "success"}, limit=10),
    )
    assert result.total == 1


@respx.mock
async def test_query_execution(connector):
    execution = {"id": "E1", "status": "success"}
    respx.get(f"{_BASE}/rest/executions/E1").mock(return_value=httpx.Response(200, json={"data": execution}))
    result = await connector.query(ConnectorQuery(resource="execution", filters={"id": "E1"}))
    assert result.records[0]["id"] == "E1"


async def test_query_execution_missing_id(connector):
    query = ConnectorQuery(resource="execution")
    with pytest.raises(ValueError, match="'id' filter"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# query — webhooks / credentials / tags / nodes
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_webhooks(connector):
    webhooks = [{"id": "WH1", "name": "Inbound"}]
    respx.get(f"{_BASE}/rest/webhooks").mock(return_value=httpx.Response(200, json={"data": webhooks}))
    result = await connector.query(ConnectorQuery(resource="webhooks"))
    assert result.total == 1


@respx.mock
async def test_query_credentials(connector):
    credentials = [{"id": "C1", "name": "GitHub"}]
    respx.get(f"{_BASE}/rest/credentials").mock(return_value=httpx.Response(200, json={"data": credentials}))
    result = await connector.query(ConnectorQuery(resource="credentials"))
    assert result.total == 1


@respx.mock
async def test_query_credential(connector):
    credential = {"id": "C1", "name": "GitHub"}
    respx.get(f"{_BASE}/rest/credentials/C1").mock(return_value=httpx.Response(200, json={"data": credential}))
    result = await connector.query(ConnectorQuery(resource="credential", filters={"id": "C1"}))
    assert result.records[0]["id"] == "C1"


async def test_query_credential_missing_id(connector):
    query = ConnectorQuery(resource="credential")
    with pytest.raises(ValueError, match="'id' filter"):
        await connector.query(query)


@respx.mock
async def test_query_tags(connector):
    tags = [{"id": "T1", "name": "prod"}]
    respx.get(f"{_BASE}/rest/tags").mock(return_value=httpx.Response(200, json={"data": tags}))
    result = await connector.query(ConnectorQuery(resource="tags"))
    assert result.total == 1


@respx.mock
async def test_query_nodes(connector):
    nodes = [{"name": "HTTP Request", "type": "n8n-nodes-base.httpRequest"}]
    respx.get(f"{_BASE}/rest/node-types").mock(return_value=httpx.Response(200, json={"data": nodes}))
    result = await connector.query(ConnectorQuery(resource="nodes"))
    assert result.total == 1


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    query = ConnectorQuery(resource="invalid_resource")
    with pytest.raises(ValueError, match="Unsupported n8n resource"):
        await connector.query(query)


# ---------------------------------------------------------------------------
# write — workflow
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_workflow(connector):
    created = {"id": "W_new", "name": "Test Workflow"}
    respx.post(f"{_BASE}/rest/workflows").mock(return_value=httpx.Response(201, json={"data": created}))
    result = await connector.write(
        ConnectorPayload(resource="workflow", data={"name": "Test Workflow"}),
    )
    assert result["id"] == "W_new"


async def test_write_workflow_missing_name(connector):
    payload = ConnectorPayload(resource="workflow", data={})
    with pytest.raises(ValueError, match="'name' in data"):
        await connector.write(payload)


# ---------------------------------------------------------------------------
# write — workflow_update
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_workflow_update(connector):
    updated = {"id": "W1", "name": "Updated"}
    respx.put(f"{_BASE}/rest/workflows/W1").mock(return_value=httpx.Response(200, json={"data": updated}))
    result = await connector.write(
        ConnectorPayload(resource="workflow_update", data={"id": "W1", "name": "Updated"}),
    )
    assert result["name"] == "Updated"


async def test_write_workflow_update_missing_id(connector):
    with pytest.raises(ValueError, match="'id' in data"):
        await connector.write(ConnectorPayload(resource="workflow_update", data={"name": "x"}))


# ---------------------------------------------------------------------------
# write — workflow_activate / deactivate / delete
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_workflow_activate(connector):
    activated = {"id": "W1", "active": True}
    respx.post(f"{_BASE}/rest/workflows/W1/activate").mock(
        return_value=httpx.Response(200, json={"data": activated}),
    )
    result = await connector.write(ConnectorPayload(resource="workflow_activate", data={"id": "W1"}))
    assert result["active"] is True


@respx.mock
async def test_write_workflow_deactivate(connector):
    deactivated = {"id": "W1", "active": False}
    respx.post(f"{_BASE}/rest/workflows/W1/deactivate").mock(
        return_value=httpx.Response(200, json={"data": deactivated}),
    )
    result = await connector.write(ConnectorPayload(resource="workflow_deactivate", data={"id": "W1"}))
    assert result["active"] is False


@respx.mock
async def test_write_workflow_delete(connector):
    respx.delete(f"{_BASE}/rest/workflows/W1").mock(return_value=httpx.Response(204, content=b""))
    result = await connector.write(ConnectorPayload(resource="workflow_delete", data={"id": "W1"}))
    assert result["deleted"] is True


async def test_write_workflow_activate_missing_id(connector):
    with pytest.raises(ValueError, match="'id' in data"):
        await connector.write(ConnectorPayload(resource="workflow_activate", data={}))


# ---------------------------------------------------------------------------
# write — credential
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_credential(connector):
    created = {"id": "C_new", "name": "MyCred", "type": "github"}
    respx.post(f"{_BASE}/rest/credentials").mock(return_value=httpx.Response(201, json={"data": created}))
    result = await connector.write(
        ConnectorPayload(resource="credential", data={"name": "MyCred", "type": "github"}),
    )
    assert result["id"] == "C_new"


async def test_write_credential_missing_type(connector):
    with pytest.raises(ValueError, match="'name' and 'type' in data"):
        await connector.write(ConnectorPayload(resource="credential", data={"name": "BadCred", "type": ""}))


# ---------------------------------------------------------------------------
# write — execution_delete / retry
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_execution_delete(connector):
    respx.delete(f"{_BASE}/rest/executions/E1").mock(return_value=httpx.Response(204, content=b""))
    result = await connector.write(ConnectorPayload(resource="execution_delete", data={"id": "E1"}))
    assert result["deleted"] is True


@respx.mock
async def test_write_execution_retry(connector):
    retried = {"id": "E1"}
    respx.post(f"{_BASE}/rest/executions/E1/retry").mock(
        return_value=httpx.Response(200, json={"data": retried}),
    )
    result = await connector.write(ConnectorPayload(resource="execution_retry", data={"id": "E1"}))
    assert result["id"] == "E1"


async def test_write_execution_retry_missing_id(connector):
    with pytest.raises(ValueError, match="'id' in data"):
        await connector.write(ConnectorPayload(resource="execution_retry", data={}))


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported n8n write resource"):
        await connector.write(ConnectorPayload(resource="invalid_resource", data={"name": "test"}))


# ---------------------------------------------------------------------------
# write — unwrapped responses must not be echoed back as the result
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_unwrapped_response_returns_empty(connector):
    """A 2xx response without a 'data' wrapper must not be returned as-is.

    Regression: `result.get("data", result)` silently returned the whole
    response (e.g. an error envelope) as the created entity.
    """
    respx.post(f"{_BASE}/rest/workflows").mock(return_value=httpx.Response(200, json={"message": "oops"}))
    result = await connector.write(ConnectorPayload(resource="workflow", data={"name": "x"}))
    assert result == {}


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/rest/workflows").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="workflows"))
