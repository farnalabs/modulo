"""Unit tests for AzureKeyVaultConnector — HTTP responses are mocked via httpx + respx."""

import httpx
import pytest
import respx

from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType

TOKEN = "kv_test_token"
_VAULT = "https://myvault.vault.azure.net"


@pytest.fixture
def connector():
    return AzureKeyVaultConnector(token=TOKEN, vault_url=_VAULT)


# ---------------------------------------------------------------------------
# connector_type
# ---------------------------------------------------------------------------


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.AZURE_KEY_VAULT


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{_VAULT}/secrets").mock(return_value=httpx.Response(200, json={"value": []}))
    result = await connector.health_check()
    assert result.ok is True
    assert "validated" in result.detail


@respx.mock
async def test_health_check_invalid_token(connector):
    respx.get(f"{_VAULT}/secrets").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid Azure Key Vault access token" in result.detail


@respx.mock
async def test_health_check_http_error(connector):
    respx.get(f"{_VAULT}/secrets").mock(return_value=httpx.Response(500, text="Internal Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert "500" in result.detail


@respx.mock
async def test_health_check_connect_error(connector):
    respx.get(f"{_VAULT}/secrets").mock(side_effect=httpx.ConnectError("boom"))
    result = await connector.health_check()
    assert result.ok is False


# ---------------------------------------------------------------------------
# query — secrets / secret
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_secrets(connector):
    body = {"value": [{"id": "https://myvault.vault.azure.net/secrets/db-pw"}]}
    respx.get(f"{_VAULT}/secrets").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="secrets"))
    assert len(result.records) == 1


@respx.mock
async def test_query_secrets_pagination(connector):
    body = {"value": [{"id": "x"}], "nextLink": "https://myvault.vault.azure.net/secrets?$skiptoken=abc"}
    respx.get(f"{_VAULT}/secrets").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="secrets"))
    assert result.next_cursor == "https://myvault.vault.azure.net/secrets?$skiptoken=abc"


@respx.mock
async def test_query_secret(connector):
    body = {"id": "https://myvault.vault.azure.net/secrets/db-pw", "value": "s3cret"}
    respx.get(f"{_VAULT}/secrets/db-pw").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="secret", filters={"name": "db-pw"}))
    assert len(result.records) == 1
    assert result.records[0]["value"] == "s3cret"


async def test_query_secret_missing_name(connector):
    with pytest.raises(ValueError, match="'name' in filters"):
        await connector.query(ConnectorQuery(resource="secret"))


# ---------------------------------------------------------------------------
# query — secret_versions / secret_by_version
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_secret_versions(connector):
    body = {"value": [{"id": "https://myvault.vault.azure.net/secrets/db-pw/1"}]}
    respx.get(f"{_VAULT}/secrets/db-pw/versions").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="secret_versions", filters={"name": "db-pw"}))
    assert len(result.records) == 1


async def test_query_secret_versions_missing_name(connector):
    with pytest.raises(ValueError, match="'name' in filters"):
        await connector.query(ConnectorQuery(resource="secret_versions"))


@respx.mock
async def test_query_secret_by_version(connector):
    body = {"id": "https://myvault.vault.azure.net/secrets/db-pw/1", "value": "v1"}
    respx.get(f"{_VAULT}/secrets/db-pw/1").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(
        ConnectorQuery(resource="secret_by_version", filters={"name": "db-pw", "version": "1"}),
    )
    assert len(result.records) == 1


async def test_query_secret_by_version_missing_version(connector):
    with pytest.raises(ValueError, match="'version' in filters"):
        await connector.query(ConnectorQuery(resource="secret_by_version", filters={"name": "db-pw"}))


# ---------------------------------------------------------------------------
# query — keys / key
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_keys(connector):
    body = {"value": [{"kid": "https://myvault.vault.azure.net/keys/signing"}]}
    respx.get(f"{_VAULT}/keys").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="keys"))
    assert len(result.records) == 1


@respx.mock
async def test_query_key(connector):
    body = {"kid": "https://myvault.vault.azure.net/keys/signing"}
    respx.get(f"{_VAULT}/keys/signing").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="key", filters={"name": "signing"}))
    assert len(result.records) == 1


async def test_query_key_missing_name(connector):
    with pytest.raises(ValueError, match="'name' in filters"):
        await connector.query(ConnectorQuery(resource="key"))


# ---------------------------------------------------------------------------
# query — certificates / certificate
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_certificates(connector):
    body = {"value": [{"id": "https://myvault.vault.azure.net/certificates/tls"}]}
    respx.get(f"{_VAULT}/certificates").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="certificates"))
    assert len(result.records) == 1


@respx.mock
async def test_query_certificate(connector):
    body = {"id": "https://myvault.vault.azure.net/certificates/tls"}
    respx.get(f"{_VAULT}/certificates/tls").mock(return_value=httpx.Response(200, json=body))
    result = await connector.query(ConnectorQuery(resource="certificate", filters={"name": "tls"}))
    assert len(result.records) == 1


async def test_query_certificate_missing_name(connector):
    with pytest.raises(ValueError, match="'name' in filters"):
        await connector.query(ConnectorQuery(resource="certificate"))


# ---------------------------------------------------------------------------
# query — unsupported resource
# ---------------------------------------------------------------------------


async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Azure Key Vault resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


# ---------------------------------------------------------------------------
# write — secret
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_secret(connector):
    created = {"id": "https://myvault.vault.azure.net/secrets/new-pw", "value": "s3cret"}
    respx.put(f"{_VAULT}/secrets/new-pw").mock(return_value=httpx.Response(200, json=created))
    result = await connector.write(
        ConnectorPayload(resource="secret", data={"name": "new-pw", "value": "s3cret"}),
    )
    assert result["id"] == created["id"]


async def test_write_secret_missing_name(connector):
    with pytest.raises(ValueError, match="'name' in data"):
        await connector.write(ConnectorPayload(resource="secret", data={"value": "x"}))


async def test_write_secret_missing_value(connector):
    with pytest.raises(ValueError, match="'value' in data"):
        await connector.write(ConnectorPayload(resource="secret", data={"name": "x"}))


# ---------------------------------------------------------------------------
# write — secret_update / secret_delete / secret_backup / secret_restore
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_secret_update(connector):
    updated = {"id": "https://myvault.vault.azure.net/secrets/x", "attributes": {"enabled": True}}
    respx.patch(f"{_VAULT}/secrets/x").mock(return_value=httpx.Response(200, json=updated))
    result = await connector.write(
        ConnectorPayload(resource="secret_update", data={"name": "x", "enabled": True}),
    )
    assert result["attributes"]["enabled"] is True


async def test_write_secret_update_missing_name(connector):
    with pytest.raises(ValueError, match="'name' in data"):
        await connector.write(ConnectorPayload(resource="secret_update", data={}))


@respx.mock
async def test_write_secret_delete(connector):
    deleted = {"id": "https://myvault.vault.azure.net/secrets/x"}
    respx.delete(f"{_VAULT}/secrets/x").mock(return_value=httpx.Response(200, json=deleted))
    result = await connector.write(ConnectorPayload(resource="secret_delete", data={"name": "x"}))
    assert result["id"] == deleted["id"]


async def test_write_secret_delete_missing_name(connector):
    with pytest.raises(ValueError, match="'name' in data"):
        await connector.write(ConnectorPayload(resource="secret_delete", data={}))


@respx.mock
async def test_write_secret_backup(connector):
    backed = {"value": "bXk="}
    respx.post(f"{_VAULT}/secrets/x/backup").mock(return_value=httpx.Response(200, json=backed))
    result = await connector.write(ConnectorPayload(resource="secret_backup", data={"name": "x"}))
    assert result["value"] == "bXk="


async def test_write_secret_backup_missing_name(connector):
    with pytest.raises(ValueError, match="'name' in data"):
        await connector.write(ConnectorPayload(resource="secret_backup", data={}))


@respx.mock
async def test_write_secret_restore(connector):
    restored = {"id": "https://myvault.vault.azure.net/secrets/x"}
    respx.post(f"{_VAULT}/secrets/restore").mock(return_value=httpx.Response(200, json=restored))
    result = await connector.write(ConnectorPayload(resource="secret_restore", data={"value": "bXk="}))
    assert result["id"] == restored["id"]


async def test_write_secret_restore_missing_value(connector):
    with pytest.raises(ValueError, match="'value' in data"):
        await connector.write(ConnectorPayload(resource="secret_restore", data={}))


# ---------------------------------------------------------------------------
# write — unsupported resource
# ---------------------------------------------------------------------------


async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Azure Key Vault write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


# ---------------------------------------------------------------------------
# HTTP error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_http_error(connector):
    respx.get(f"{_VAULT}/secrets").mock(return_value=httpx.Response(500, text="Internal Error"))
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="secrets"))
