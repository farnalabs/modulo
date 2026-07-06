"""Unit tests for OnePasswordConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.onepassword import OnePasswordConnector

TOKEN = "op_test_token"
BASE_URL = "http://localhost:8080"


@pytest.fixture()
def connector() -> OnePasswordConnector:
    return OnePasswordConnector(token=TOKEN, base_url=BASE_URL)


def test_connector_type(connector: OnePasswordConnector) -> None:
    assert connector.connector_type == ConnectorType.ONEPASSWORD


@respx.mock
async def test_health_check_ok(connector: OnePasswordConnector) -> None:
    respx.get(f"{BASE_URL}/v1/vaults", params={"limit": 1}).mock(return_value=httpx.Response(200, json=[]))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "1Password Connect token validated"


@respx.mock
async def test_health_check_invalid_token(connector: OnePasswordConnector) -> None:
    respx.get(f"{BASE_URL}/v1/vaults", params={"limit": 1}).mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid" in result.detail


@respx.mock
async def test_health_check_other_http_error(connector: OnePasswordConnector) -> None:
    respx.get(f"{BASE_URL}/v1/vaults", params={"limit": 1}).mock(return_value=httpx.Response(403, text="Forbidden"))
    result = await connector.health_check()
    assert result.ok is False
    assert "HTTP 403" in result.detail


@respx.mock
async def test_health_check_network_error(connector: OnePasswordConnector) -> None:
    respx.get(f"{BASE_URL}/v1/vaults", params={"limit": 1}).mock(side_effect=httpx.ConnectError("connection refused"))
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


@respx.mock
async def test_query_vaults(connector: OnePasswordConnector) -> None:
    vaults = [{"id": "v1", "name": "Personal"}, {"id": "v2", "name": "Shared"}]
    respx.get(f"{BASE_URL}/v1/vaults").mock(return_value=httpx.Response(200, json=vaults))
    result = await connector.query(ConnectorQuery(resource="vaults"))
    assert len(result.records) == 2
    assert result.records[0]["id"] == "v1"


@respx.mock
async def test_query_vaults_with_limit(connector: OnePasswordConnector) -> None:
    vaults = [{"id": "v1"}, {"id": "v2"}]
    respx.get(f"{BASE_URL}/v1/vaults", params={"limit": 1}).mock(return_value=httpx.Response(200, json=vaults))
    result = await connector.query(ConnectorQuery(resource="vaults", limit=1))
    assert len(result.records) == 1


@respx.mock
async def test_query_vault(connector: OnePasswordConnector) -> None:
    vault = {"id": "v1", "name": "Personal"}
    respx.get(f"{BASE_URL}/v1/vaults/v1").mock(return_value=httpx.Response(200, json=vault))
    result = await connector.query(ConnectorQuery(resource="vault", filters={"vault_id": "v1"}))
    assert len(result.records) == 1
    assert result.records[0]["name"] == "Personal"


@respx.mock
async def test_query_vault_missing_id(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password vault query requires 'vault_id'"):
        await connector.query(ConnectorQuery(resource="vault"))


@respx.mock
async def test_query_items(connector: OnePasswordConnector) -> None:
    items = [{"id": "i1", "title": "My Login"}]
    respx.get(f"{BASE_URL}/v1/vaults/v1/items").mock(return_value=httpx.Response(200, json=items))
    result = await connector.query(ConnectorQuery(resource="items", filters={"vault_id": "v1"}))
    assert len(result.records) == 1
    assert result.records[0]["title"] == "My Login"


@respx.mock
async def test_query_items_missing_vault(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password items query requires 'vault_id'"):
        await connector.query(ConnectorQuery(resource="items"))


@respx.mock
async def test_query_items_with_limit(connector: OnePasswordConnector) -> None:
    items = [{"id": "i1"}, {"id": "i2"}, {"id": "i3"}]
    respx.get(f"{BASE_URL}/v1/vaults/v1/items", params={"limit": 2}).mock(return_value=httpx.Response(200, json=items))
    result = await connector.query(ConnectorQuery(resource="items", filters={"vault_id": "v1"}, limit=2))
    assert len(result.records) == 2


@respx.mock
async def test_query_item(connector: OnePasswordConnector) -> None:
    item = {"id": "i1", "title": "My Login", "fields": []}
    respx.get(f"{BASE_URL}/v1/vaults/v1/items/i1").mock(return_value=httpx.Response(200, json=item))
    result = await connector.query(ConnectorQuery(resource="item", filters={"vault_id": "v1", "item_id": "i1"}))
    assert len(result.records) == 1
    assert result.records[0]["title"] == "My Login"


@respx.mock
async def test_query_item_missing_vault(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password item query requires 'vault_id'"):
        await connector.query(ConnectorQuery(resource="item", filters={"item_id": "i1"}))


@respx.mock
async def test_query_item_missing_item(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password item query requires 'item_id'"):
        await connector.query(ConnectorQuery(resource="item", filters={"vault_id": "v1"}))


@respx.mock
async def test_query_item_by_title(connector: OnePasswordConnector) -> None:
    items = [{"id": "i1", "title": "My Login"}]
    respx.get(f"{BASE_URL}/v1/vaults/v1/items", params={"filter[title]": "My Login"}).mock(
        return_value=httpx.Response(200, json=items)
    )
    result = await connector.query(
        ConnectorQuery(resource="item_by_title", filters={"vault_id": "v1", "title": "My Login"})
    )
    assert len(result.records) == 1


@respx.mock
async def test_query_item_by_title_missing_vault(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password item_by_title query requires 'vault_id'"):
        await connector.query(ConnectorQuery(resource="item_by_title", filters={"title": "Login"}))


@respx.mock
async def test_query_item_by_title_missing_title(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password item_by_title query requires 'title'"):
        await connector.query(ConnectorQuery(resource="item_by_title", filters={"vault_id": "v1"}))


@respx.mock
async def test_query_files(connector: OnePasswordConnector) -> None:
    files = [{"id": "f1", "name": "attachment.txt"}]
    respx.get(f"{BASE_URL}/v1/vaults/v1/items/i1/files").mock(return_value=httpx.Response(200, json=files))
    result = await connector.query(ConnectorQuery(resource="files", filters={"vault_id": "v1", "item_id": "i1"}))
    assert len(result.records) == 1


@respx.mock
async def test_query_files_missing_vault(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password files query requires 'vault_id'"):
        await connector.query(ConnectorQuery(resource="files", filters={"item_id": "i1"}))


@respx.mock
async def test_query_files_missing_item(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password files query requires 'item_id'"):
        await connector.query(ConnectorQuery(resource="files", filters={"vault_id": "v1"}))


@respx.mock
async def test_query_file_content(connector: OnePasswordConnector) -> None:
    respx.get(f"{BASE_URL}/v1/vaults/v1/items/i1/files/f1/content").mock(
        return_value=httpx.Response(200, text="file content here")
    )
    result = await connector.query(
        ConnectorQuery(resource="file", filters={"vault_id": "v1", "item_id": "i1", "file_id": "f1"})
    )
    assert len(result.records) == 1
    assert result.records[0]["content"] == "file content here"


@respx.mock
async def test_query_file_missing_vault(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password file query requires 'vault_id'"):
        await connector.query(ConnectorQuery(resource="file", filters={"item_id": "i1", "file_id": "f1"}))


@respx.mock
async def test_query_file_missing_item(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password file query requires 'item_id'"):
        await connector.query(ConnectorQuery(resource="file", filters={"vault_id": "v1", "file_id": "f1"}))


@respx.mock
async def test_query_file_missing_file_id(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password file query requires 'file_id'"):
        await connector.query(ConnectorQuery(resource="file", filters={"vault_id": "v1", "item_id": "i1"}))


@respx.mock
async def test_write_create_item(connector: OnePasswordConnector) -> None:
    created = {"id": "new-item", "title": "New Login", "type": "LOGIN"}
    respx.post(f"{BASE_URL}/v1/vaults/v1/items").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="item",
            data={"vault_id": "v1", "title": "New Login", "type": "LOGIN", "fields": []},
        )
    )
    assert result["id"] == "new-item"
    assert result["title"] == "New Login"


@respx.mock
async def test_write_create_item_missing_vault(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password item write requires 'vault_id'"):
        await connector.write(ConnectorPayload(resource="item", data={"title": "New", "type": "LOGIN", "fields": []}))


@respx.mock
async def test_write_update_item(connector: OnePasswordConnector) -> None:
    updated = {"id": "i1", "title": "Updated Login"}
    respx.put(f"{BASE_URL}/v1/vaults/v1/items/i1").mock(return_value=httpx.Response(200, json=updated))
    result = await connector.write(
        ConnectorPayload(
            resource="item_update",
            data={"vault_id": "v1", "item_id": "i1", "title": "Updated Login", "fields": []},
        )
    )
    assert result["title"] == "Updated Login"


@respx.mock
async def test_write_update_item_missing_vault(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password item_update write requires 'vault_id'"):
        await connector.write(ConnectorPayload(resource="item_update", data={"item_id": "i1", "title": "Updated"}))


@respx.mock
async def test_write_update_item_missing_item(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password item_update write requires 'item_id'"):
        await connector.write(ConnectorPayload(resource="item_update", data={"vault_id": "v1", "title": "Updated"}))


@respx.mock
async def test_write_delete_item(connector: OnePasswordConnector) -> None:
    respx.delete(f"{BASE_URL}/v1/vaults/v1/items/i1").mock(return_value=httpx.Response(204))
    result = await connector.write(ConnectorPayload(resource="item_delete", data={"vault_id": "v1", "item_id": "i1"}))
    assert result["status"] == "deleted"


@respx.mock
async def test_write_delete_item_missing_vault(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password item_delete write requires 'vault_id'"):
        await connector.write(ConnectorPayload(resource="item_delete", data={"item_id": "i1"}))


@respx.mock
async def test_write_delete_item_missing_item(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password item_delete write requires 'item_id'"):
        await connector.write(ConnectorPayload(resource="item_delete", data={"vault_id": "v1"}))


@respx.mock
async def test_write_archive_item(connector: OnePasswordConnector) -> None:
    respx.delete(f"{BASE_URL}/v1/vaults/v1/items/i1").mock(return_value=httpx.Response(204))
    result = await connector.write(ConnectorPayload(resource="item_archive", data={"vault_id": "v1", "item_id": "i1"}))
    assert result["status"] == "archived"


@respx.mock
async def test_write_archive_item_missing_vault(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password item_archive write requires 'vault_id'"):
        await connector.write(ConnectorPayload(resource="item_archive", data={"item_id": "i1"}))


@respx.mock
async def test_write_archive_item_missing_item(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="1Password item_archive write requires 'item_id'"):
        await connector.write(ConnectorPayload(resource="item_archive", data={"vault_id": "v1"}))


@respx.mock
async def test_query_invalid_resource(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported 1Password resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


@respx.mock
async def test_write_invalid_resource(connector: OnePasswordConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported 1Password write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


@respx.mock
async def test_query_http_401(connector: OnePasswordConnector) -> None:
    respx.get(f"{BASE_URL}/v1/vaults").mock(return_value=httpx.Response(401, text="Unauthorized"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="vaults"))


@respx.mock
async def test_query_http_500(connector: OnePasswordConnector) -> None:
    respx.get(f"{BASE_URL}/v1/vaults").mock(return_value=httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="vaults"))


@respx.mock
async def test_write_http_403(connector: OnePasswordConnector) -> None:
    respx.post(f"{BASE_URL}/v1/vaults/v1/items").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(
            ConnectorPayload(
                resource="item",
                data={"vault_id": "v1", "title": "New", "type": "LOGIN", "fields": []},
            )
        )
