"""Unit tests for OnePasswordConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.onepassword import OnePasswordConnector

TOKEN = "op_test_token"
_BASE = "http://localhost:8080"


@pytest.fixture
def connector():
    return OnePasswordConnector(token=TOKEN, base_url=_BASE)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.ONEPASSWORD


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_BASE}/v1/vaults").mock(return_value=httpx.Response(200, json=[]))
    result = await connector.health_check()
    assert result.ok is True
    assert "validated" in result.detail


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_BASE}/v1/vaults").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid 1Password Connect API token" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_BASE}/v1/vaults").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


# ---------------------------------------------------------------------------
# query — vaults / vault
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_vaults(connector):
    vaults = [{"id": "vault1", "name": "Personal"}]
    respx.get(f"{_BASE}/v1/vaults").mock(return_value=httpx.Response(200, json=vaults))
    result = await connector.query(ConnectorQuery(resource="vaults", limit=10))
    assert result.records[0]["id"] == "vault1"


@respx.mock
async def test_query_vault(connector):
    vault = {"id": "vault1", "name": "Personal"}
    respx.get(f"{_BASE}/v1/vaults/vault1").mock(return_value=httpx.Response(200, json=vault))
    result = await connector.query(ConnectorQuery(resource="vault", filters={"vault_id": "vault1"}))
    assert result.records[0]["id"] == "vault1"


async def test_query_vault_missing_id(connector):
    with pytest.raises(ValueError, match="'vault_id' in filters"):
        await connector.query(ConnectorQuery(resource="vault"))


# ---------------------------------------------------------------------------
# query — items / item / item_by_title
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_items(connector):
    items = [{"id": "item1", "title": "My Login"}]
    respx.get(f"{_BASE}/v1/vaults/vault1/items").mock(return_value=httpx.Response(200, json=items))
    result = await connector.query(ConnectorQuery(resource="items", filters={"vault_id": "vault1"}))
    assert result.records[0]["id"] == "item1"


async def test_query_items_missing_vault_id(connector):
    with pytest.raises(ValueError, match="'vault_id' in filters"):
        await connector.query(ConnectorQuery(resource="items"))


@respx.mock
async def test_query_item(connector):
    item = {"id": "item1", "title": "My Login"}
    respx.get(f"{_BASE}/v1/vaults/vault1/items/item1").mock(return_value=httpx.Response(200, json=item))
    result = await connector.query(
        ConnectorQuery(resource="item", filters={"vault_id": "vault1", "item_id": "item1"}),
    )
    assert result.records[0]["id"] == "item1"


async def test_query_item_missing_item_id(connector):
    with pytest.raises(ValueError, match="'item_id' in filters"):
        await connector.query(ConnectorQuery(resource="item", filters={"vault_id": "vault1"}))


@respx.mock
async def test_query_item_by_title(connector):
    items = [{"id": "item1", "title": "My Login"}]
    respx.get(f"{_BASE}/v1/vaults/vault1/items").mock(return_value=httpx.Response(200, json=items))
    result = await connector.query(
        ConnectorQuery(resource="item_by_title", filters={"vault_id": "vault1", "title": "My Login"}),
    )
    assert result.records[0]["id"] == "item1"


async def test_query_item_by_title_missing_title(connector):
    with pytest.raises(ValueError, match="'title' in filters"):
        await connector.query(ConnectorQuery(resource="item_by_title", filters={"vault_id": "vault1"}))


# ---------------------------------------------------------------------------
# query — files / file
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_files(connector):
    files = [{"id": "f1", "name": "secret.txt"}]
    respx.get(f"{_BASE}/v1/vaults/vault1/items/item1/files").mock(
        return_value=httpx.Response(200, json=files),
    )
    result = await connector.query(
        ConnectorQuery(resource="files", filters={"vault_id": "vault1", "item_id": "item1"}),
    )
    assert result.records[0]["id"] == "f1"


@respx.mock
async def test_query_file(connector):
    respx.get(f"{_BASE}/v1/vaults/vault1/items/item1/files/f1/content").mock(
        return_value=httpx.Response(200, content=b"top secret"),
    )
    result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"vault_id": "vault1", "item_id": "item1", "file_id": "f1"},
        ),
    )
    assert result.records[0]["content"] == "top secret"


async def test_query_file_missing_file_id(connector):
    with pytest.raises(ValueError, match="'file_id' in filters"):
        await connector.query(
            ConnectorQuery(resource="file", filters={"vault_id": "vault1", "item_id": "item1"}),
        )


# ---------------------------------------------------------------------------
# write — item
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_item(connector):
    created = {"id": "item_new", "title": "New Item", "type": "LOGIN"}
    respx.post(f"{_BASE}/v1/vaults/vault1/items").mock(return_value=httpx.Response(201, json=created))
    result = await connector.write(
        ConnectorPayload(
            resource="item",
            data={"vault_id": "vault1", "title": "New Item", "type": "LOGIN"},
        ),
    )
    assert result["id"] == "item_new"


async def test_write_item_missing_vault_id(connector):
    with pytest.raises(ValueError, match="'vault_id' in data"):
        await connector.write(ConnectorPayload(resource="item", data={"title": "New Item"}))


# ---------------------------------------------------------------------------
# write — item_update
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_item_update(connector):
    updated = {"id": "item1", "title": "Updated"}
    respx.put(f"{_BASE}/v1/vaults/vault1/items/item1").mock(return_value=httpx.Response(200, json=updated))
    result = await connector.write(
        ConnectorPayload(
            resource="item_update",
            data={"vault_id": "vault1", "item_id": "item1", "title": "Updated"},
        ),
    )
    assert result["title"] == "Updated"


async def test_write_item_update_missing_item_id(connector):
    with pytest.raises(ValueError, match="'item_id' in data"):
        await connector.write(
            ConnectorPayload(resource="item_update", data={"vault_id": "vault1"}),
        )


# ---------------------------------------------------------------------------
# write — item_delete
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_item_delete(connector):
    respx.delete(f"{_BASE}/v1/vaults/vault1/items/item1").mock(
        return_value=httpx.Response(204, content=b""),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="item_delete",
            data={"vault_id": "vault1", "item_id": "item1"},
        ),
    )
    assert result["status"] == "deleted"


async def test_write_item_delete_missing_fields(connector):
    with pytest.raises(ValueError, match="'vault_id' in data"):
        await connector.write(ConnectorPayload(resource="item_delete", data={"item_id": "item1"}))
    with pytest.raises(ValueError, match="'item_id' in data"):
        await connector.write(ConnectorPayload(resource="item_delete", data={"vault_id": "vault1"}))


# ---------------------------------------------------------------------------
# write — item_archive
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_item_archive(connector):
    respx.patch(f"{_BASE}/v1/vaults/vault1/items/item1").mock(
        return_value=httpx.Response(200, json={"id": "item1", "state": "archived"}),
    )
    result = await connector.write(
        ConnectorPayload(
            resource="item_archive",
            data={"vault_id": "vault1", "item_id": "item1"},
        ),
    )
    assert result["status"] == "archived"


# ---------------------------------------------------------------------------
# query / write — unsupported
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported 1Password resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported 1Password write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_BASE}/v1/vaults").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="vaults"))
