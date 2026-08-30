"""OnePasswordConnector — async 1Password Connect REST API connector."""

import asyncio
from typing import Any, cast

import httpx

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
    health_check_failure,
)


class OnePasswordConnector(ConnectorBase):
    def __init__(self, token: str, base_url: str = "http://localhost:8080") -> None:
        self._token = token
        self._base = base_url.rstrip("/")

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.ONEPASSWORD

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base,
            headers=self._headers(),
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/v1/vaults", params={"limit": 1})
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="1Password Connect token validated")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid 1Password Connect API token")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "vaults":
                    return await self._list_vaults(c, q)
                case "vault":
                    return await self._get_vault(c, q)
                case "items":
                    return await self._list_items(c, q)
                case "item":
                    return await self._get_item(c, q)
                case "item_by_title":
                    return await self._get_item_by_title(c, q)
                case "files":
                    return await self._list_files(c, q)
                case "file":
                    return await self._get_file(c, q)
                case _:
                    raise ValueError(f"Unsupported 1Password resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "item":
                    return await self._create_item(c, payload.data)
                case "item_update":
                    return await self._update_item(c, payload.data)
                case "item_delete":
                    return await self._delete_item(c, payload.data)
                case "item_archive":
                    return await self._archive_item(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported 1Password write resource: {payload.resource!r}")

    async def _list_vaults(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        resp = await c.get("/v1/vaults", params=params)
        resp.raise_for_status()
        body: list[dict[str, Any]] = resp.json()
        return ConnectorResult(records=body[: q.limit] if q.limit else body)

    async def _get_vault(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        vault_id = q.filters.get("vault_id", "")
        if not vault_id:
            raise ValueError("1Password vault query requires 'vault_id' in filters")
        resp = await c.get(f"/v1/vaults/{vault_id}")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return ConnectorResult(records=[body])

    async def _list_items(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        vault_id = q.filters.get("vault_id", "")
        if not vault_id:
            raise ValueError("1Password items query requires 'vault_id' in filters")
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        resp = await c.get(f"/v1/vaults/{vault_id}/items", params=params)
        resp.raise_for_status()
        body: list[dict[str, Any]] = resp.json()
        return ConnectorResult(records=body[: q.limit] if q.limit else body)

    async def _get_item(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        vault_id = q.filters.get("vault_id", "")
        if not vault_id:
            raise ValueError("1Password item query requires 'vault_id' in filters")
        item_id = q.filters.get("item_id", "")
        if not item_id:
            raise ValueError("1Password item query requires 'item_id' in filters")
        resp = await c.get(f"/v1/vaults/{vault_id}/items/{item_id}")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return ConnectorResult(records=[body])

    async def _get_item_by_title(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        vault_id = q.filters.get("vault_id", "")
        if not vault_id:
            raise ValueError("1Password item_by_title query requires 'vault_id' in filters")
        title = q.filters.get("title", "")
        if not title:
            raise ValueError("1Password item_by_title query requires 'title' in filters")
        resp = await c.get(f"/v1/vaults/{vault_id}/items", params={"filter[title]": title})
        resp.raise_for_status()
        body: list[dict[str, Any]] = resp.json()
        return ConnectorResult(records=body)

    async def _list_files(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        vault_id = q.filters.get("vault_id", "")
        if not vault_id:
            raise ValueError("1Password files query requires 'vault_id' in filters")
        item_id = q.filters.get("item_id", "")
        if not item_id:
            raise ValueError("1Password files query requires 'item_id' in filters")
        resp = await c.get(f"/v1/vaults/{vault_id}/items/{item_id}/files")
        resp.raise_for_status()
        body: list[dict[str, Any]] = resp.json()
        return ConnectorResult(records=body)

    async def _get_file(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        vault_id = q.filters.get("vault_id", "")
        if not vault_id:
            raise ValueError("1Password file query requires 'vault_id' in filters")
        item_id = q.filters.get("item_id", "")
        if not item_id:
            raise ValueError("1Password file query requires 'item_id' in filters")
        file_id = q.filters.get("file_id", "")
        if not file_id:
            raise ValueError("1Password file query requires 'file_id' in filters")
        resp = await c.get(f"/v1/vaults/{vault_id}/items/{item_id}/files/{file_id}/content")
        resp.raise_for_status()
        content = await resp.aread()
        return ConnectorResult(records=[{"file_id": file_id, "content": content.decode("utf-8", errors="replace")}])

    async def _create_item(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        vault_id = data.get("vault_id", "")
        if not vault_id:
            raise ValueError("1Password item write requires 'vault_id' in data")
        body: dict[str, Any] = {
            "type": data.get("type", "LOGIN"),
            "title": data.get("title", ""),
            "fields": data.get("fields", []),
        }
        resp = await c.post(f"/v1/vaults/{vault_id}/items", json=body)
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    async def _update_item(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        vault_id = data.get("vault_id", "")
        if not vault_id:
            raise ValueError("1Password item_update write requires 'vault_id' in data")
        item_id = data.get("item_id", "")
        if not item_id:
            raise ValueError("1Password item_update write requires 'item_id' in data")
        body: dict[str, Any] = {
            "type": data.get("type", "LOGIN"),
            "title": data.get("title", ""),
            "fields": data.get("fields", []),
        }
        resp = await c.put(f"/v1/vaults/{vault_id}/items/{item_id}", json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def _delete_item(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        vault_id = data.get("vault_id", "")
        if not vault_id:
            raise ValueError("1Password item_delete write requires 'vault_id' in data")
        item_id = data.get("item_id", "")
        if not item_id:
            raise ValueError("1Password item_delete write requires 'item_id' in data")
        resp = await c.delete(f"/v1/vaults/{vault_id}/items/{item_id}")
        if resp.status_code == 204:
            return {"status": "deleted", "vault_id": vault_id, "item_id": item_id}
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def _archive_item(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        vault_id = data.get("vault_id", "")
        if not vault_id:
            raise ValueError("1Password item_archive write requires 'vault_id' in data")
        item_id = data.get("item_id", "")
        if not item_id:
            raise ValueError("1Password item_archive write requires 'item_id' in data")
        resp = await c.patch(f"/v1/vaults/{vault_id}/items/{item_id}", json={"state": "archived"})
        if resp.status_code == 200:
            archive_result = resp.json()
            return {"status": "archived", "vault_id": vault_id, "item_id": item_id, "result": archive_result}
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())
