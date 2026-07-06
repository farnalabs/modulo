"""Unit tests for AzureKeyVaultConnector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType

TOKEN = "az_kv_test_token"
VAULT_URL = "https://myvault.vault.azure.net"
_BASE = VAULT_URL


@pytest.fixture()
def connector() -> AzureKeyVaultConnector:
    return AzureKeyVaultConnector(token=TOKEN, vault_url=VAULT_URL)


def test_connector_type(connector: AzureKeyVaultConnector) -> None:
    assert connector.connector_type == ConnectorType.AZURE_KEY_VAULT


@respx.mock
async def test_health_check_ok(connector: AzureKeyVaultConnector) -> None:
    respx.get(f"{_BASE}/secrets", params={"api-version": "7.4", "maxresults": 1}).mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "Azure Key Vault token validated"


@respx.mock
async def test_health_check_invalid_token(connector: AzureKeyVaultConnector) -> None:
    respx.get(f"{_BASE}/secrets", params={"api-version": "7.4", "maxresults": 1}).mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid" in result.detail


@respx.mock
async def test_health_check_other_http_error(connector: AzureKeyVaultConnector) -> None:
    respx.get(f"{_BASE}/secrets", params={"api-version": "7.4", "maxresults": 1}).mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "HTTP 403" in result.detail


@respx.mock
async def test_health_check_network_error(connector: AzureKeyVaultConnector) -> None:
    respx.get(f"{_BASE}/secrets", params={"api-version": "7.4", "maxresults": 1}).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "connection refused" in result.detail


@respx.mock
async def test_query_secrets(connector: AzureKeyVaultConnector) -> None:
    secrets = {"value": [{"id": "https://myvault.vault.azure.net/secrets/secret1", "attributes": {"enabled": True}}]}
    respx.get(f"{_BASE}/secrets", params={"api-version": "7.4"}).mock(return_value=httpx.Response(200, json=secrets))
    result = await connector.query(ConnectorQuery(resource="secrets"))
    assert len(result.records) == 1
    assert "secret1" in result.records[0]["id"]


@respx.mock
async def test_query_secrets_with_limit(connector: AzureKeyVaultConnector) -> None:
    secrets = {"value": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]}
    respx.get(f"{_BASE}/secrets", params={"api-version": "7.4", "maxresults": 2}).mock(
        return_value=httpx.Response(200, json=secrets)
    )
    result = await connector.query(ConnectorQuery(resource="secrets", limit=2))
    assert len(result.records) == 2


@respx.mock
async def test_query_secrets_with_cursor(connector: AzureKeyVaultConnector) -> None:
    secrets = {"value": [{"id": "s3"}], "nextLink": "https://myvault.vault.azure.net/secrets?$skiptoken=abc"}
    respx.get(f"{_BASE}/secrets", params={"api-version": "7.4", "$skiptoken": "abc"}).mock(
        return_value=httpx.Response(200, json=secrets)
    )
    result = await connector.query(ConnectorQuery(resource="secrets", cursor="abc"))
    assert len(result.records) == 1
    assert result.next_cursor is not None


@respx.mock
async def test_query_secret(connector: AzureKeyVaultConnector) -> None:
    secret_data = {"value": "my-secret-value", "id": "https://myvault.vault.azure.net/secrets/my-secret"}
    respx.get(f"{_BASE}/secrets/my-secret", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(200, json=secret_data)
    )
    result = await connector.query(ConnectorQuery(resource="secret", filters={"name": "my-secret"}))
    assert len(result.records) == 1
    assert result.records[0]["value"] == "my-secret-value"


@respx.mock
async def test_query_secret_missing_name(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault secret query requires 'name'"):
        await connector.query(ConnectorQuery(resource="secret"))


@respx.mock
async def test_query_secret_versions(connector: AzureKeyVaultConnector) -> None:
    versions = {"value": [{"id": "v1", "attributes": {"enabled": True}}, {"id": "v2"}]}
    respx.get(f"{_BASE}/secrets/my-secret/versions", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(200, json=versions)
    )
    result = await connector.query(ConnectorQuery(resource="secret_versions", filters={"name": "my-secret"}))
    assert len(result.records) == 2


@respx.mock
async def test_query_secret_versions_missing_name(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault secret_versions query requires 'name'"):
        await connector.query(ConnectorQuery(resource="secret_versions"))


@respx.mock
async def test_query_secret_by_version(connector: AzureKeyVaultConnector) -> None:
    secret_data = {"value": "versioned-value", "id": "https://myvault.vault.azure.net/secrets/my-secret/abc123"}
    respx.get(f"{_BASE}/secrets/my-secret/abc123", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(200, json=secret_data)
    )
    result = await connector.query(
        ConnectorQuery(resource="secret_by_version", filters={"name": "my-secret", "version": "abc123"})
    )
    assert len(result.records) == 1
    assert result.records[0]["value"] == "versioned-value"


@respx.mock
async def test_query_secret_by_version_missing_name(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault secret_by_version query requires 'name'"):
        await connector.query(ConnectorQuery(resource="secret_by_version", filters={"version": "abc"}))


@respx.mock
async def test_query_secret_by_version_missing_version(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault secret_by_version query requires 'version'"):
        await connector.query(ConnectorQuery(resource="secret_by_version", filters={"name": "my-secret"}))


@respx.mock
async def test_query_keys(connector: AzureKeyVaultConnector) -> None:
    keys = {"value": [{"kid": "https://myvault.vault.azure.net/keys/key1", "attributes": {"enabled": True}}]}
    respx.get(f"{_BASE}/keys", params={"api-version": "7.4"}).mock(return_value=httpx.Response(200, json=keys))
    result = await connector.query(ConnectorQuery(resource="keys"))
    assert len(result.records) == 1


@respx.mock
async def test_query_keys_with_limit(connector: AzureKeyVaultConnector) -> None:
    keys = {"value": [{"kid": "k1"}, {"kid": "k2"}]}
    respx.get(f"{_BASE}/keys", params={"api-version": "7.4", "maxresults": 1}).mock(
        return_value=httpx.Response(200, json=keys)
    )
    result = await connector.query(ConnectorQuery(resource="keys", limit=1))
    assert len(result.records) == 1


@respx.mock
async def test_query_key(connector: AzureKeyVaultConnector) -> None:
    key_data = {"key": {"kid": "https://myvault.vault.azure.net/keys/key1", "kty": "RSA"}}
    respx.get(f"{_BASE}/keys/key1", params={"api-version": "7.4"}).mock(return_value=httpx.Response(200, json=key_data))
    result = await connector.query(ConnectorQuery(resource="key", filters={"name": "key1"}))
    assert len(result.records) == 1
    assert result.records[0]["key"]["kty"] == "RSA"


@respx.mock
async def test_query_key_missing_name(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault key query requires 'name'"):
        await connector.query(ConnectorQuery(resource="key"))


@respx.mock
async def test_query_certificates(connector: AzureKeyVaultConnector) -> None:
    certs = {"value": [{"id": "https://myvault.vault.azure.net/certificates/cert1"}]}
    respx.get(f"{_BASE}/certificates", params={"api-version": "7.4"}).mock(return_value=httpx.Response(200, json=certs))
    result = await connector.query(ConnectorQuery(resource="certificates"))
    assert len(result.records) == 1


@respx.mock
async def test_query_certificates_with_cursor(connector: AzureKeyVaultConnector) -> None:
    certs = {"value": [{"id": "c2"}], "nextLink": "https://myvault.vault.azure.net/certificates?$skiptoken=xyz"}
    respx.get(f"{_BASE}/certificates", params={"api-version": "7.4", "$skiptoken": "xyz"}).mock(
        return_value=httpx.Response(200, json=certs)
    )
    result = await connector.query(ConnectorQuery(resource="certificates", cursor="xyz"))
    assert len(result.records) == 1
    assert result.next_cursor is not None


@respx.mock
async def test_query_certificate(connector: AzureKeyVaultConnector) -> None:
    cert_data = {
        "id": "https://myvault.vault.azure.net/certificates/cert1",
        "policy": {"x509_props": {"subject": "CN=test"}},
    }
    respx.get(f"{_BASE}/certificates/cert1", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(200, json=cert_data)
    )
    result = await connector.query(ConnectorQuery(resource="certificate", filters={"name": "cert1"}))
    assert len(result.records) == 1
    assert result.records[0]["policy"]["x509_props"]["subject"] == "CN=test"


@respx.mock
async def test_query_certificate_missing_name(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault certificate query requires 'name'"):
        await connector.query(ConnectorQuery(resource="certificate"))


@respx.mock
async def test_write_secret(connector: AzureKeyVaultConnector) -> None:
    created = {
        "id": "https://myvault.vault.azure.net/secrets/new-secret",
        "value": "s3cret",
        "attributes": {"enabled": True},
    }
    respx.put(f"{_BASE}/secrets/new-secret", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(200, json=created)
    )
    result = await connector.write(ConnectorPayload(resource="secret", data={"name": "new-secret", "value": "s3cret"}))
    assert result["value"] == "s3cret"
    assert "new-secret" in result["id"]


@respx.mock
async def test_write_secret_with_content_type_and_tags(connector: AzureKeyVaultConnector) -> None:
    created = {
        "id": "https://myvault.vault.azure.net/secrets/tagged",
        "value": "val",
        "contentType": "text/plain",
        "tags": {"env": "prod"},
    }
    respx.put(f"{_BASE}/secrets/tagged", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(200, json=created)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="secret",
            data={"name": "tagged", "value": "val", "content_type": "text/plain", "tags": {"env": "prod"}},
        )
    )
    assert result["contentType"] == "text/plain"
    assert result["tags"]["env"] == "prod"


@respx.mock
async def test_write_secret_missing_name(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault secret write requires 'name'"):
        await connector.write(ConnectorPayload(resource="secret", data={"value": "val"}))


@respx.mock
async def test_write_secret_missing_value(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault secret write requires 'value'"):
        await connector.write(ConnectorPayload(resource="secret", data={"name": "my-secret"}))


@respx.mock
async def test_write_secret_update(connector: AzureKeyVaultConnector) -> None:
    updated = {"id": "https://myvault.vault.azure.net/secrets/existing", "attributes": {"enabled": False}}
    respx.patch(f"{_BASE}/secrets/existing", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(200, json=updated)
    )
    result = await connector.write(
        ConnectorPayload(resource="secret_update", data={"name": "existing", "enabled": False})
    )
    assert result["attributes"]["enabled"] is False


@respx.mock
async def test_write_secret_update_with_tags(connector: AzureKeyVaultConnector) -> None:
    updated = {"id": "https://myvault.vault.azure.net/secrets/existing", "tags": {"team": "platform"}}
    respx.patch(f"{_BASE}/secrets/existing", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(200, json=updated)
    )
    result = await connector.write(
        ConnectorPayload(
            resource="secret_update",
            data={"name": "existing", "tags": {"team": "platform"}, "content_type": "text/plain"},
        )
    )
    assert result["tags"]["team"] == "platform"


@respx.mock
async def test_write_secret_update_missing_name(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault secret_update write requires 'name'"):
        await connector.write(ConnectorPayload(resource="secret_update", data={"enabled": False}))


@respx.mock
async def test_write_secret_delete(connector: AzureKeyVaultConnector) -> None:
    deleted = {
        "id": "https://myvault.vault.azure.net/secrets/to-delete",
        "recoveryId": "https://myvault.vault.azure.net/deletedsecrets/to-delete",
    }
    respx.delete(f"{_BASE}/secrets/to-delete", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(200, json=deleted)
    )
    result = await connector.write(ConnectorPayload(resource="secret_delete", data={"name": "to-delete"}))
    assert "recoveryId" in result


@respx.mock
async def test_write_secret_delete_missing_name(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault secret_delete write requires 'name'"):
        await connector.write(ConnectorPayload(resource="secret_delete", data={}))


@respx.mock
async def test_write_secret_backup(connector: AzureKeyVaultConnector) -> None:
    backup_response = {"value": "base64-encoded-backup-blob"}
    respx.post(f"{_BASE}/secrets/my-secret/backup", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(200, json=backup_response)
    )
    result = await connector.write(ConnectorPayload(resource="secret_backup", data={"name": "my-secret"}))
    assert "value" in result


@respx.mock
async def test_write_secret_backup_missing_name(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault secret_backup write requires 'name'"):
        await connector.write(ConnectorPayload(resource="secret_backup", data={}))


@respx.mock
async def test_write_secret_restore(connector: AzureKeyVaultConnector) -> None:
    restored = {"id": "https://myvault.vault.azure.net/secrets/restored-secret", "value": "restored-val"}
    respx.post(f"{_BASE}/secrets/restore", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(200, json=restored)
    )
    result = await connector.write(ConnectorPayload(resource="secret_restore", data={"value": "base64-backup-blob"}))
    assert result["value"] == "restored-val"


@respx.mock
async def test_write_secret_restore_missing_value(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Azure Key Vault secret_restore write requires 'value'"):
        await connector.write(ConnectorPayload(resource="secret_restore", data={}))


@respx.mock
async def test_query_invalid_resource(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Azure Key Vault resource"):
        await connector.query(ConnectorQuery(resource="invalid"))


@respx.mock
async def test_write_invalid_resource(connector: AzureKeyVaultConnector) -> None:
    with pytest.raises(ValueError, match="Unsupported Azure Key Vault write resource"):
        await connector.write(ConnectorPayload(resource="invalid", data={}))


@respx.mock
async def test_query_http_401(connector: AzureKeyVaultConnector) -> None:
    respx.get(f"{_BASE}/secrets", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="secrets"))


@respx.mock
async def test_query_http_500(connector: AzureKeyVaultConnector) -> None:
    respx.get(f"{_BASE}/secrets", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.query(ConnectorQuery(resource="secrets"))


@respx.mock
async def test_write_http_403(connector: AzureKeyVaultConnector) -> None:
    respx.put(f"{_BASE}/secrets/blocked", params={"api-version": "7.4"}).mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connector.write(ConnectorPayload(resource="secret", data={"name": "blocked", "value": "val"}))
