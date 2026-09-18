"""AzureKeyVaultConnector — async Azure Key Vault REST API connector."""

import asyncio
from typing import Any

import httpx

from modulo.connectors._safe_cursor import safe_cursor as _safe_cursor
from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
    health_check_failure,
)
from modulo.core.ssrf import pinned_async_client_sync

_API_VERSION = "7.4"

# Pagination query parameter name shared across list endpoints (S1192).
_SKIP_TOKEN = "$skiptoken"  # nosec B105 — Azure pagination query-param name, not a credential


class AzureKeyVaultConnector(ConnectorBase):
    def __init__(self, token: str, vault_url: str) -> None:
        self._token = token
        self._base = vault_url.rstrip("/")

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.AZURE_KEY_VAULT

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        # PINNED TRANSPORT (FAR-520): validate + resolve the base_url's host
        # synchronously and pin the validated IP onto the transport, so the
        # connection never re-resolves the host at connect time (closes the
        # DNS-rebinding window). ``trust_env=False`` stops a proxy from
        # re-resolving the destination server-side and defeating the pin.
        return pinned_async_client_sync(
            self._base,
            base_url=self._base,
            headers=self._headers(),
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/secrets", params={"api-version": _API_VERSION, "maxresults": 1})
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="Azure Key Vault token validated")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid Azure Key Vault access token")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "secrets":
                    return await self._list_secrets(c, q)
                case "secret":
                    return await self._get_secret(c, q)
                case "secret_versions":
                    return await self._list_secret_versions(c, q)
                case "secret_by_version":
                    return await self._get_secret_by_version(c, q)
                case "keys":
                    return await self._list_keys(c, q)
                case "key":
                    return await self._get_key(c, q)
                case "certificates":
                    return await self._list_certificates(c, q)
                case "certificate":
                    return await self._get_certificate(c, q)
                case _:
                    raise ValueError(f"Unsupported Azure Key Vault resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "secret":
                    return await self._set_secret(c, payload.data)
                case "secret_update":
                    return await self._update_secret(c, payload.data)
                case "secret_delete":
                    return await self._delete_secret(c, payload.data)
                case "secret_backup":
                    return await self._backup_secret(c, payload.data)
                case "secret_restore":
                    return await self._restore_secret(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported Azure Key Vault write resource: {payload.resource!r}")

    async def _list_secrets(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"api-version": _API_VERSION}
        if q.limit:
            params["maxresults"] = q.limit
        if q.cursor:
            params[_SKIP_TOKEN] = q.cursor
        resp = await c.get("/secrets", params=params)
        resp.raise_for_status()
        body: object = resp.json()
        records = _safe_records(body, "value")
        next_link = body.get("nextLink") if isinstance(body, dict) else None
        next_cursor = _safe_cursor(next_link)
        return ConnectorResult(records=records[: q.limit] if q.limit else records, total=None, next_cursor=next_cursor)

    async def _get_secret(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        name = q.filters.get("name", "")
        if not name:
            raise ValueError("Azure Key Vault secret query requires 'name' in filters")
        resp = await c.get(f"/secrets/{name}", params={"api-version": _API_VERSION})
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return ConnectorResult(records=[body])

    async def _list_secret_versions(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        name = q.filters.get("name", "")
        if not name:
            raise ValueError("Azure Key Vault secret_versions query requires 'name' in filters")
        params: dict[str, Any] = {"api-version": _API_VERSION}
        if q.limit:
            params["maxresults"] = q.limit
        if q.cursor:
            params[_SKIP_TOKEN] = q.cursor
        resp = await c.get(f"/secrets/{name}/versions", params=params)
        resp.raise_for_status()
        body: object = resp.json()
        records = _safe_records(body, "value")
        next_link = body.get("nextLink") if isinstance(body, dict) else None
        next_cursor = _safe_cursor(next_link)
        return ConnectorResult(records=records[: q.limit] if q.limit else records, next_cursor=next_cursor)

    async def _get_secret_by_version(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        name = q.filters.get("name", "")
        if not name:
            raise ValueError("Azure Key Vault secret_by_version query requires 'name' in filters")
        version = q.filters.get("version", "")
        if not version:
            raise ValueError("Azure Key Vault secret_by_version query requires 'version' in filters")
        resp = await c.get(f"/secrets/{name}/{version}", params={"api-version": _API_VERSION})
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return ConnectorResult(records=[body])

    async def _list_keys(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"api-version": _API_VERSION}
        if q.limit:
            params["maxresults"] = q.limit
        if q.cursor:
            params[_SKIP_TOKEN] = q.cursor
        resp = await c.get("/keys", params=params)
        resp.raise_for_status()
        body: object = resp.json()
        records = _safe_records(body, "value")
        next_link = body.get("nextLink") if isinstance(body, dict) else None
        next_cursor = _safe_cursor(next_link)
        return ConnectorResult(records=records[: q.limit] if q.limit else records, next_cursor=next_cursor)

    async def _get_key(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        name = q.filters.get("name", "")
        if not name:
            raise ValueError("Azure Key Vault key query requires 'name' in filters")
        resp = await c.get(f"/keys/{name}", params={"api-version": _API_VERSION})
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return ConnectorResult(records=[body])

    async def _list_certificates(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {"api-version": _API_VERSION}
        if q.limit:
            params["maxresults"] = q.limit
        if q.cursor:
            params[_SKIP_TOKEN] = q.cursor
        resp = await c.get("/certificates", params=params)
        resp.raise_for_status()
        body: object = resp.json()
        records = _safe_records(body, "value")
        next_link = body.get("nextLink") if isinstance(body, dict) else None
        next_cursor = _safe_cursor(next_link)
        return ConnectorResult(records=records[: q.limit] if q.limit else records, next_cursor=next_cursor)

    async def _get_certificate(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        name = q.filters.get("name", "")
        if not name:
            raise ValueError("Azure Key Vault certificate query requires 'name' in filters")
        resp = await c.get(f"/certificates/{name}", params={"api-version": _API_VERSION})
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return ConnectorResult(records=[body])

    async def _set_secret(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        name = data.get("name", "")
        if not name:
            raise ValueError("Azure Key Vault secret write requires 'name' in data")
        value = data.get("value", "")
        if not value:
            raise ValueError("Azure Key Vault secret write requires 'value' in data")
        body: dict[str, Any] = {"value": value}
        if "content_type" in data:
            body["contentType"] = data["content_type"]
        if "tags" in data:
            body["tags"] = data["tags"]
        resp = await c.put(f"/secrets/{name}", params={"api-version": _API_VERSION}, json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def _update_secret(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        name = data.get("name", "")
        if not name:
            raise ValueError("Azure Key Vault secret_update write requires 'name' in data")
        body: dict[str, Any] = {}
        if "content_type" in data:
            body["contentType"] = data["content_type"]
        if "tags" in data:
            body["tags"] = data["tags"]
        if "enabled" in data:
            body["attributes"] = {"enabled": data["enabled"]}
        resp = await c.patch(f"/secrets/{name}", params={"api-version": _API_VERSION}, json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def _delete_secret(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        name = data.get("name", "")
        if not name:
            raise ValueError("Azure Key Vault secret_delete write requires 'name' in data")
        resp = await c.delete(f"/secrets/{name}", params={"api-version": _API_VERSION})
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def _backup_secret(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        name = data.get("name", "")
        if not name:
            raise ValueError("Azure Key Vault secret_backup write requires 'name' in data")
        resp = await c.post(f"/secrets/{name}/backup", params={"api-version": _API_VERSION})
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def _restore_secret(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        backup = data.get("value", "")
        if not backup:
            raise ValueError("Azure Key Vault secret_restore write requires 'value' in data")
        body: dict[str, Any] = {"value": backup}
        resp = await c.post("/secrets/restore", params={"api-version": _API_VERSION}, json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result
