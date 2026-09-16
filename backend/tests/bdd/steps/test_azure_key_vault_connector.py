"""Step definitions for the Azure Key Vault connector BDD feature.

Wires ``features/connectors/azure_key_vault.feature`` into the executing
suite (the improve-architecture product-map walk) by driving the REAL
``AzureKeyVaultConnector`` against a respx-mocked Azure Key Vault REST API
(api-version 7.4) — mirroring ``backend/tests/unit/connectors/test_azure_key_vault.py``
so the executing BDD surface locks the same contract the unit suite does:
token validation via ``GET /secrets?maxresults=1`` (200 => healthy, 401 =>
unhealthy), listing secrets / keys / certificates, single-secret / key /
certificate lookups, and the secret write family (create + soft-delete).
"""

import asyncio
import contextlib

import httpx
import pytest
import respx
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/azure_key_vault.feature")

VAULT_URL = "https://myvault.vault.azure.net"
_API_VERSION = "7.4"


@pytest.fixture
def ctx() -> dict:
    """Shared mutable context dict for the Azure Key Vault connector scenarios."""
    return {}


@given("an Azure Key Vault connector configured with valid credentials")
def akv_valid_connector(ctx: dict) -> None:
    from modulo.connectors.azure_key_vault import AzureKeyVaultConnector

    ctx["connector"] = AzureKeyVaultConnector(token="az_kv_test_token", vault_url=VAULT_URL)
    ctx["credentials_valid"] = True


@given("an Azure Key Vault connector configured with invalid credentials")
def akv_invalid_connector(ctx: dict) -> None:
    from modulo.connectors.azure_key_vault import AzureKeyVaultConnector

    ctx["connector"] = AzureKeyVaultConnector(token="bad_token", vault_url=VAULT_URL)
    ctx["credentials_valid"] = False


@when("the connector checks health")
def akv_health_check(ctx: dict) -> None:
    from modulo.connectors.azure_key_vault import AzureKeyVaultConnector

    connector = ctx["connector"]
    assert isinstance(connector, AzureKeyVaultConnector)
    if ctx["credentials_valid"]:
        response = httpx.Response(200, json={"value": []})
    else:
        response = httpx.Response(401, text="Unauthorized")
    with respx.mock:
        respx.get(f"{VAULT_URL}/secrets", params={"api-version": _API_VERSION, "maxresults": 1}).mock(
            return_value=response
        )
        result = asyncio.run(connector.health_check())
    ctx["health_result"] = result


@then(parsers.parse('the health check returns "{expected}"'))
def akv_health_result(ctx: dict, expected: str) -> None:
    result = ctx["health_result"]
    want_ok = expected == "healthy"
    assert result.ok is want_ok, f"expected health {expected!r}, got: {result.detail}"


@when("the connector queries secrets")
def akv_query_secrets(ctx: dict) -> None:
    from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
    from modulo.connectors.base import ConnectorQuery

    connector = ctx["connector"]
    assert isinstance(connector, AzureKeyVaultConnector)
    body = {"value": [{"id": f"{VAULT_URL}/secrets/secret1", "attributes": {"enabled": True}}]}
    with respx.mock:
        respx.get(f"{VAULT_URL}/secrets", params={"api-version": _API_VERSION}).mock(
            return_value=httpx.Response(200, json=body)
        )
        result = asyncio.run(connector.query(ConnectorQuery(resource="secrets")))
    ctx["records"] = result.records
    assert ctx["records"], "no secret records returned"


@when(parsers.parse('the connector queries secret "{name}"'))
def akv_query_secret(ctx: dict, name: str) -> None:
    from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
    from modulo.connectors.base import ConnectorQuery

    connector = ctx["connector"]
    assert isinstance(connector, AzureKeyVaultConnector)
    body = {"value": "my-secret-value", "id": f"{VAULT_URL}/secrets/{name}"}
    with respx.mock:
        respx.get(f"{VAULT_URL}/secrets/{name}", params={"api-version": _API_VERSION}).mock(
            return_value=httpx.Response(200, json=body)
        )
        result = asyncio.run(connector.query(ConnectorQuery(resource="secret", filters={"name": name})))
    ctx["records"] = result.records
    assert ctx["records"], f"no record returned for secret {name!r}"


@when("the connector queries keys")
def akv_query_keys(ctx: dict) -> None:
    from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
    from modulo.connectors.base import ConnectorQuery

    connector = ctx["connector"]
    assert isinstance(connector, AzureKeyVaultConnector)
    body = {"value": [{"kid": f"{VAULT_URL}/keys/key1", "attributes": {"enabled": True}}]}
    with respx.mock:
        respx.get(f"{VAULT_URL}/keys", params={"api-version": _API_VERSION}).mock(
            return_value=httpx.Response(200, json=body)
        )
        result = asyncio.run(connector.query(ConnectorQuery(resource="keys")))
    ctx["records"] = result.records
    assert ctx["records"], "no key records returned"


@when(parsers.parse('the connector queries key "{name}"'))
def akv_query_key(ctx: dict, name: str) -> None:
    from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
    from modulo.connectors.base import ConnectorQuery

    connector = ctx["connector"]
    assert isinstance(connector, AzureKeyVaultConnector)
    body = {"key": {"kid": f"{VAULT_URL}/keys/{name}", "kty": "RSA"}}
    with respx.mock:
        respx.get(f"{VAULT_URL}/keys/{name}", params={"api-version": _API_VERSION}).mock(
            return_value=httpx.Response(200, json=body)
        )
        result = asyncio.run(connector.query(ConnectorQuery(resource="key", filters={"name": name})))
    ctx["records"] = result.records
    assert ctx["records"], f"no record returned for key {name!r}"


@when("the connector queries certificates")
def akv_query_certificates(ctx: dict) -> None:
    from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
    from modulo.connectors.base import ConnectorQuery

    connector = ctx["connector"]
    assert isinstance(connector, AzureKeyVaultConnector)
    body = {"value": [{"id": f"{VAULT_URL}/certificates/cert1"}]}
    with respx.mock:
        respx.get(f"{VAULT_URL}/certificates", params={"api-version": _API_VERSION}).mock(
            return_value=httpx.Response(200, json=body)
        )
        result = asyncio.run(connector.query(ConnectorQuery(resource="certificates")))
    ctx["records"] = result.records
    assert ctx["records"], "no certificate records returned"


@when(parsers.parse('the connector queries certificate "{name}"'))
def akv_query_certificate(ctx: dict, name: str) -> None:
    from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
    from modulo.connectors.base import ConnectorQuery

    connector = ctx["connector"]
    assert isinstance(connector, AzureKeyVaultConnector)
    body = {"id": f"{VAULT_URL}/certificates/{name}", "policy": {"x509_props": {"subject": "CN=test"}}}
    with respx.mock:
        respx.get(f"{VAULT_URL}/certificates/{name}", params={"api-version": _API_VERSION}).mock(
            return_value=httpx.Response(200, json=body)
        )
        result = asyncio.run(connector.query(ConnectorQuery(resource="certificate", filters={"name": name})))
    ctx["records"] = result.records
    assert ctx["records"], f"no record returned for certificate {name!r}"


@when(parsers.parse('the connector creates secret "{name}" with value "{value}"'))
def akv_create_secret(ctx: dict, name: str, value: str) -> None:
    from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
    from modulo.connectors.base import ConnectorPayload

    connector = ctx["connector"]
    assert isinstance(connector, AzureKeyVaultConnector)
    body = {"id": f"{VAULT_URL}/secrets/{name}", "value": value, "attributes": {"enabled": True}}
    with respx.mock:
        respx.put(f"{VAULT_URL}/secrets/{name}", params={"api-version": _API_VERSION}).mock(
            return_value=httpx.Response(200, json=body)
        )
        result = asyncio.run(
            connector.write(ConnectorPayload(resource="secret", data={"name": name, "value": value}))
        )
    ctx["write_result"] = result
    assert result["value"] == value, result


@when(parsers.parse('the connector deletes secret "{name}"'))
def akv_delete_secret(ctx: dict, name: str) -> None:
    from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
    from modulo.connectors.base import ConnectorPayload

    connector = ctx["connector"]
    assert isinstance(connector, AzureKeyVaultConnector)
    body = {"id": f"{VAULT_URL}/secrets/{name}", "recoveryId": f"{VAULT_URL}/deletedsecrets/{name}"}
    with respx.mock:
        respx.delete(f"{VAULT_URL}/secrets/{name}", params={"api-version": _API_VERSION}).mock(
            return_value=httpx.Response(200, json=body)
        )
        result = asyncio.run(connector.write(ConnectorPayload(resource="secret_delete", data={"name": name})))
    ctx["write_result"] = result
    assert "recoveryId" in result, result


@then("the result contains secret metadata")
def akv_records_secret_metadata(ctx: dict) -> None:
    records = ctx["records"]
    assert records and "secret1" in records[0].get("id", ""), records


@then("the result contains the secret value")
def akv_records_secret_value(ctx: dict) -> None:
    records = ctx["records"]
    assert records and records[0].get("value") == "my-secret-value", records


@then("the result contains key metadata")
def akv_records_key_metadata(ctx: dict) -> None:
    records = ctx["records"]
    assert records and "key1" in records[0].get("kid", ""), records


@then("the result contains the key details")
def akv_records_key_details(ctx: dict) -> None:
    records = ctx["records"]
    assert records and records[0].get("key", {}).get("kty") == "RSA", records


@then("the result contains certificate metadata")
def akv_records_certificate_metadata(ctx: dict) -> None:
    records = ctx["records"]
    assert records and "cert1" in records[0].get("id", ""), records


@then("the result contains the certificate details")
def akv_records_certificate_details(ctx: dict) -> None:
    records = ctx["records"]
    assert records and records[0].get("policy", {}).get("x509_props", {}).get("subject") == "CN=test", records


@then("the secret is created successfully")
def akv_secret_created(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert result.get("value") == "s3cret", result


@then("the secret is soft-deleted")
def akv_secret_deleted(ctx: dict) -> None:
    result = ctx["write_result"]
    assert result is not None, "No write result"
    assert "recoveryId" in result, result
