"""Unit tests for N8NConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.n8n import N8NConnector

TOKEN = "n8n_test_token"
BASE_URL = "http://localhost:5678"


@pytest.fixture()
def connector() -> N8NConnector:
    return N8NConnector(token=TOKEN, base_url=BASE_URL)


def test_connector_type(connector: N8NConnector) -> None:
    assert connector.connector_type == ConnectorType.N8N


# -- health_check -- #


@respx.mock
async def test_health_check_ok(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows", params={"limit": 1}).mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert "reachable" in result.detail


@respx.mock
async def test_health_check_invalid_token(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows", params={"limit": 1}).mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid n8n API token" in result.detail


@respx.mock
async def test_health_check_connect_error(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows", params={"limit": 1}).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Cannot connect" in result.detail


@respx.mock
async def test_health_check_other_status(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows", params={"limit": 1}).mock(
        return_value=httpx.Response(429, text="Too Many Requests")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "429" in result.detail


@respx.mock
async def test_health_check_generic_error(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows", params={"limit": 1}).mock(side_effect=RuntimeError("unexpected"))
    result = await connector.health_check()
    assert result.ok is False


# -- query: workflows -- #


@respx.mock
async def test_query_workflows(connector: N8NConnector) -> None:
    workflows = [{"id": "W1", "name": "Test", "active": True}, {"id": "W2", "name": "Prod", "active": False}]
    respx.get(f"{BASE_URL}/rest/workflows").mock(return_value=httpx.Response(200, json={"data": workflows}))
    result = await connector.query(ConnectorQuery(resource="workflows"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "Test"


@respx.mock
async def test_query_workflows_with_limit(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows", params={"limit": 1}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "W1", "name": "Only one"}]})
    )
    result = await connector.query(ConnectorQuery(resource="workflows", limit=1))
    assert len(result.records) == 1


@respx.mock
async def test_query_workflows_with_active_filter(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows", params={"active": "true"}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "W1", "name": "Active WF", "active": True}]})
    )
    result = await connector.query(ConnectorQuery(resource="workflows", filters={"active": "true"}))
    assert len(result.records) == 1
    assert result.records[0]["active"] is True


@respx.mock
async def test_query_workflows_with_tags_filter(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows", params={"tags": "production"}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "W2", "name": "Prod WF", "tags": ["production"]}]})
    )
    result = await connector.query(ConnectorQuery(resource="workflows", filters={"tags": "production"}))
    assert len(result.records) == 1


@respx.mock
async def test_query_workflows_with_cursor(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows", params={"cursor": "next_page"}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "W3"}], "nextCursor": "page3"})
    )
    result = await connector.query(ConnectorQuery(resource="workflows", cursor="next_page"))
    assert len(result.records) == 1
    assert result.next_cursor == "page3"


# -- query: single workflow -- #


@respx.mock
async def test_query_workflow_by_id(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows/W1").mock(
        return_value=httpx.Response(200, json={"data": {"id": "W1", "name": "Single WF"}})
    )
    result = await connector.query(ConnectorQuery(resource="workflow", filters={"id": "W1"}))
    assert len(result.records) == 1
    assert result.records[0]["id"] == "W1"


async def test_query_workflow_missing_id(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n workflow query requires 'id' filter"):
        await connector.query(ConnectorQuery(resource="workflow"))


# -- query: executions -- #


@respx.mock
async def test_query_executions(connector: N8NConnector) -> None:
    executions = [{"id": "E1", "status": "success"}, {"id": "E2", "status": "running"}]
    respx.get(f"{BASE_URL}/rest/executions").mock(return_value=httpx.Response(200, json={"data": executions}))
    result = await connector.query(ConnectorQuery(resource="executions"))
    assert len(result.records) == 2


@respx.mock
async def test_query_executions_with_status_filter(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/executions", params={"status": "success"}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "E1", "status": "success"}]})
    )
    result = await connector.query(ConnectorQuery(resource="executions", filters={"status": "success"}))
    assert len(result.records) == 1
    assert result.records[0]["status"] == "success"


@respx.mock
async def test_query_executions_with_workflow_id_filter(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/executions", params={"workflowId": "W1"}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "E1", "workflowId": "W1"}]})
    )
    result = await connector.query(ConnectorQuery(resource="executions", filters={"workflowId": "W1"}))
    assert len(result.records) == 1
    assert result.records[0]["workflowId"] == "W1"


@respx.mock
async def test_query_executions_with_limit(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/executions", params={"limit": 3}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": f"E{i}"} for i in range(3)]})
    )
    result = await connector.query(ConnectorQuery(resource="executions", limit=3))
    assert len(result.records) == 3


# -- query: single execution -- #


@respx.mock
async def test_query_execution_by_id(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/executions/E1").mock(
        return_value=httpx.Response(200, json={"data": {"id": "E1", "status": "success"}})
    )
    result = await connector.query(ConnectorQuery(resource="execution", filters={"id": "E1"}))
    assert len(result.records) == 1
    assert result.records[0]["id"] == "E1"


async def test_query_execution_missing_id(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n execution query requires 'id' filter"):
        await connector.query(ConnectorQuery(resource="execution"))


# -- query: webhooks -- #


@respx.mock
async def test_query_webhooks(connector: N8NConnector) -> None:
    webhooks = [{"id": "WH1", "name": "GitHub Push", "webhookId": "wh_123"}]
    respx.get(f"{BASE_URL}/rest/webhooks").mock(return_value=httpx.Response(200, json={"data": webhooks}))
    result = await connector.query(ConnectorQuery(resource="webhooks"))
    assert len(result.records) == 1
    assert result.records[0]["webhookId"] == "wh_123"


# -- query: credentials -- #


@respx.mock
async def test_query_credentials(connector: N8NConnector) -> None:
    creds = [{"id": "C1", "name": "GitHub PAT", "type": "github"}]
    respx.get(f"{BASE_URL}/rest/credentials").mock(return_value=httpx.Response(200, json={"data": creds}))
    result = await connector.query(ConnectorQuery(resource="credentials"))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "GitHub PAT"


# -- query: single credential -- #


@respx.mock
async def test_query_credential_by_id(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/credentials/C1").mock(
        return_value=httpx.Response(200, json={"data": {"id": "C1", "name": "My Cred"}})
    )
    result = await connector.query(ConnectorQuery(resource="credential", filters={"id": "C1"}))
    assert len(result.records) == 1


async def test_query_credential_missing_id(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n credential query requires 'id' filter"):
        await connector.query(ConnectorQuery(resource="credential"))


# -- query: tags -- #


@respx.mock
async def test_query_tags(connector: N8NConnector) -> None:
    tags = [{"id": "T1", "name": "production"}, {"id": "T2", "name": "staging"}]
    respx.get(f"{BASE_URL}/rest/tags").mock(return_value=httpx.Response(200, json={"data": tags}))
    result = await connector.query(ConnectorQuery(resource="tags"))
    assert len(result.records) == 2
    assert result.records[0]["name"] == "production"


# -- query: nodes (node-types) -- #


@respx.mock
async def test_query_nodes(connector: N8NConnector) -> None:
    node_types = [{"name": "n8n-nodes-base.httpRequest", "displayName": "HTTP Request"}]
    respx.get(f"{BASE_URL}/rest/node-types").mock(return_value=httpx.Response(200, json={"data": node_types}))
    result = await connector.query(ConnectorQuery(resource="nodes"))
    assert len(result.records) == 1
    assert result.records[0]["displayName"] == "HTTP Request"


# -- write: create workflow -- #


@respx.mock
async def test_write_workflow(connector: N8NConnector) -> None:
    respx.post(f"{BASE_URL}/rest/workflows").mock(
        return_value=httpx.Response(201, json={"data": {"id": "W1", "name": "Test WF", "active": False}})
    )
    result = await connector.write(ConnectorPayload(resource="workflow", data={"name": "Test WF"}))
    assert result["id"] == "W1"
    assert result["name"] == "Test WF"


async def test_write_workflow_missing_name(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n workflow creation requires 'name' in data"):
        await connector.write(ConnectorPayload(resource="workflow", data={}))


@respx.mock
async def test_write_workflow_with_full_data(connector: N8NConnector) -> None:
    respx.post(f"{BASE_URL}/rest/workflows").mock(
        return_value=httpx.Response(201, json={"data": {"id": "W2", "name": "Full WF", "active": False}})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="workflow",
            data={
                "name": "Full WF",
                "nodes": [{"id": "n1", "name": "Start", "type": "n8n-nodes-base.noOp"}],
                "connections": {},
                "settings": {"timezone": "UTC"},
                "staticData": {},
                "tags": ["production"],
            },
        )
    )
    assert result["id"] == "W2"


# -- write: update workflow -- #


@respx.mock
async def test_write_workflow_update(connector: N8NConnector) -> None:
    respx.put(f"{BASE_URL}/rest/workflows/W1").mock(
        return_value=httpx.Response(200, json={"data": {"id": "W1", "name": "Updated WF"}})
    )
    result = await connector.write(
        ConnectorPayload(resource="workflow_update", data={"id": "W1", "name": "Updated WF"})
    )
    assert result["name"] == "Updated WF"


async def test_write_workflow_update_missing_id(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n workflow update requires 'id' in data"):
        await connector.write(ConnectorPayload(resource="workflow_update", data={}))


# -- write: activate/deactivate -- #


@respx.mock
async def test_write_workflow_activate(connector: N8NConnector) -> None:
    respx.post(f"{BASE_URL}/rest/workflows/W1/activate").mock(
        return_value=httpx.Response(200, json={"data": {"id": "W1", "active": True}})
    )
    result = await connector.write(ConnectorPayload(resource="workflow_activate", data={"id": "W1"}))
    assert result["active"] is True


async def test_write_workflow_activate_missing_id(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n workflow activation requires 'id' in data"):
        await connector.write(ConnectorPayload(resource="workflow_activate", data={}))


@respx.mock
async def test_write_workflow_deactivate(connector: N8NConnector) -> None:
    respx.post(f"{BASE_URL}/rest/workflows/W1/deactivate").mock(
        return_value=httpx.Response(200, json={"data": {"id": "W1", "active": False}})
    )
    result = await connector.write(ConnectorPayload(resource="workflow_deactivate", data={"id": "W1"}))
    assert result["active"] is False


async def test_write_workflow_deactivate_missing_id(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n workflow deactivation requires 'id' in data"):
        await connector.write(ConnectorPayload(resource="workflow_deactivate", data={}))


# -- write: delete workflow -- #


@respx.mock
async def test_write_workflow_delete(connector: N8NConnector) -> None:
    respx.delete(f"{BASE_URL}/rest/workflows/W1").mock(
        return_value=httpx.Response(200, json={"data": {"id": "W1", "deleted": True}})
    )
    result = await connector.write(ConnectorPayload(resource="workflow_delete", data={"id": "W1"}))
    assert result["deleted"] is True


@respx.mock
async def test_write_workflow_delete_204(connector: N8NConnector) -> None:
    respx.delete(f"{BASE_URL}/rest/workflows/W1").mock(return_value=httpx.Response(204))
    result = await connector.write(ConnectorPayload(resource="workflow_delete", data={"id": "W1"}))
    assert result["deleted"] is True


async def test_write_workflow_delete_missing_id(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n workflow deletion requires 'id' in data"):
        await connector.write(ConnectorPayload(resource="workflow_delete", data={}))


# -- write: delete execution -- #


@respx.mock
async def test_write_execution_delete(connector: N8NConnector) -> None:
    respx.delete(f"{BASE_URL}/rest/executions/E1").mock(return_value=httpx.Response(204))
    result = await connector.write(ConnectorPayload(resource="execution_delete", data={"id": "E1"}))
    assert result["deleted"] is True


@respx.mock
async def test_write_execution_delete_200(connector: N8NConnector) -> None:
    respx.delete(f"{BASE_URL}/rest/executions/E1").mock(
        return_value=httpx.Response(200, json={"data": {"id": "E1", "deleted": True}})
    )
    result = await connector.write(ConnectorPayload(resource="execution_delete", data={"id": "E1"}))
    assert result["deleted"] is True


async def test_write_execution_delete_missing_id(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n execution deletion requires 'id' in data"):
        await connector.write(ConnectorPayload(resource="execution_delete", data={}))


# -- write: create credential -- #


@respx.mock
async def test_write_credential(connector: N8NConnector) -> None:
    respx.post(f"{BASE_URL}/rest/credentials").mock(
        return_value=httpx.Response(201, json={"data": {"id": "C1", "name": "My Cred", "type": "github"}})
    )
    result = await connector.write(
        ConnectorPayload(resource="credential", data={"name": "My Cred", "type": "github", "data": {"token": "abc"}})
    )
    assert result["id"] == "C1"
    assert result["type"] == "github"


async def test_write_credential_missing_name(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n credential creation requires 'name' and 'type' in data"):
        await connector.write(ConnectorPayload(resource="credential", data={"type": "github"}))


async def test_write_credential_missing_type(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n credential creation requires 'name' and 'type' in data"):
        await connector.write(ConnectorPayload(resource="credential", data={"name": "My Cred"}))


# -- write: retry execution -- #


@respx.mock
async def test_write_execution_retry(connector: N8NConnector) -> None:
    respx.post(f"{BASE_URL}/rest/executions/E1/retry").mock(
        return_value=httpx.Response(200, json={"data": {"id": "E1", "retryOf": "E0", "status": "running"}})
    )
    result = await connector.write(ConnectorPayload(resource="execution_retry", data={"id": "E1"}))
    assert result["status"] == "running"


async def test_write_execution_retry_missing_id(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="n8n execution retry requires 'id' in data"):
        await connector.write(ConnectorPayload(resource="execution_retry", data={}))


# -- error cases -- #


async def test_query_invalid_resource(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported n8n resource"):
        await connector.query(ConnectorQuery(resource="invalid_resource"))


async def test_write_invalid_resource(connector: N8NConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported n8n write resource"):
        await connector.write(ConnectorPayload(resource="invalid_resource", data={}))


@respx.mock
async def test_query_http_500(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="workflows"))


@respx.mock
async def test_write_http_403(connector: N8NConnector) -> None:
    respx.post(f"{BASE_URL}/rest/workflows").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(ConnectorPayload(resource="workflow", data={"name": "Test"}))


@respx.mock
async def test_query_workflows_empty_response(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows").mock(return_value=httpx.Response(200, json={"data": []}))
    result = await connector.query(ConnectorQuery(resource="workflows"))
    assert len(result.records) == 0
    assert result.total == 0


@respx.mock
async def test_query_executions_with_limit_and_cursor(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/executions", params={"limit": 10, "cursor": "abc"}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "E10"}], "nextCursor": "def"})
    )
    result = await connector.query(ConnectorQuery(resource="executions", limit=10, cursor="abc"))
    assert len(result.records) == 1
    assert result.next_cursor == "def"


@respx.mock
async def test_query_tags_with_limit(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/tags", params={"limit": 2}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "T1"}, {"id": "T2"}]})
    )
    result = await connector.query(ConnectorQuery(resource="tags", limit=2))
    assert len(result.records) == 2


@respx.mock
async def test_query_nodes_with_limit(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/node-types", params={"limit": 1}).mock(
        return_value=httpx.Response(200, json={"data": [{"name": "n8n-nodes-base.noOp"}]})
    )
    result = await connector.query(ConnectorQuery(resource="nodes", limit=1))
    assert len(result.records) == 1


@respx.mock
async def test_query_webhooks_with_limit(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/webhooks", params={"limit": 5}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": f"WH{i}"} for i in range(5)]})
    )
    result = await connector.query(ConnectorQuery(resource="webhooks", limit=5))
    assert len(result.records) == 5


@respx.mock
async def test_query_credentials_with_cursor(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/credentials", params={"cursor": "p2"}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "C2"}], "nextCursor": None})
    )
    result = await connector.query(ConnectorQuery(resource="credentials", cursor="p2"))
    assert len(result.records) == 1
    assert result.next_cursor is None


@respx.mock
async def test_write_workflow_activate_already_active(connector: N8NConnector) -> None:
    respx.post(f"{BASE_URL}/rest/workflows/W1/activate").mock(
        return_value=httpx.Response(200, json={"data": {"id": "W1", "active": True}})
    )
    result = await connector.write(ConnectorPayload(resource="workflow_activate", data={"id": "W1"}))
    assert result["active"] is True


@respx.mock
async def test_write_credential_with_full_data(connector: N8NConnector) -> None:
    respx.post(f"{BASE_URL}/rest/credentials").mock(
        return_value=httpx.Response(201, json={"data": {"id": "C2", "name": "Full Cred", "type": "slack"}})
    )
    result = await connector.write(
        ConnectorPayload(
            resource="credential",
            data={"name": "Full Cred", "type": "slack", "data": {"accessToken": "xoxb-..."}},
        )
    )
    assert result["id"] == "C2"


@respx.mock
async def test_health_check_network_timeout(connector: N8NConnector) -> None:
    respx.get(f"{BASE_URL}/rest/workflows", params={"limit": 1}).mock(side_effect=httpx.TimeoutException("timed out"))
    result = await connector.health_check()
    assert result.ok is False
