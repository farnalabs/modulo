"""NpmConnector — async npm Registry API connector for package metadata."""

import asyncio
from typing import Any

import httpx

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_API_BASE = "https://registry.npmjs.org"
_NPM_SEARCH_ENDPOINT = "/-/v1/search"
_NPM_MAX_SEARCH_SIZE = 250


class NpmConnector(ConnectorBase):
    def __init__(self, token: str = "") -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.NPM

    def _client(self) -> httpx.AsyncClient:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return httpx.AsyncClient(
            base_url=_API_BASE,
            headers=headers,
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/-/v1/search", params={"text": "express", "size": 1})
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="npm registry reachable")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid npm auth token")
                if resp.status_code == 403:
                    return HealthResult(ok=False, detail="npm token lacks required permissions")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Cannot connect to npm registry")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "package":
                    return await self._get_package(c, q)
                case "package_version":
                    return await self._get_package_version(c, q)
                case "search":
                    return await self._search_packages(c, q)
                case "package_files":
                    return await self._get_package_files(c, q)
                case "scope_packages":
                    return await self._scope_packages(c, q)
                case _:
                    raise ValueError(f"Unsupported npm resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        raise ValueError(f"npm registry is read-only: cannot write resource {payload.resource!r}")

    async def _get_package(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        pkg = q.filters.get("package")
        if not pkg:
            raise ValueError("npm package query requires 'package' in filters")
        resp = await c.get(f"/{pkg}")
        resp.raise_for_status()
        body = resp.json()
        return ConnectorResult(records=[body], total=1)

    async def _get_package_version(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        pkg = q.filters.get("package")
        version = q.filters.get("version")
        if not pkg:
            raise ValueError("npm package_version query requires 'package' in filters")
        if not version:
            raise ValueError("npm package_version query requires 'version' in filters")
        resp = await c.get(f"/{pkg}/{version}")
        resp.raise_for_status()
        body = resp.json()
        return ConnectorResult(records=[body], total=1)

    async def _search_packages(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        return await self._search_registry(c, q, required_filter="text")

    async def _search_registry(
        self,
        c: httpx.AsyncClient,
        q: ConnectorQuery,
        *,
        required_filter: str,
    ) -> ConnectorResult:
        filter_value = q.filters.get(required_filter)
        if not filter_value:
            raise ValueError(f"npm {q.resource} query requires '{required_filter}' in filters")

        size = max(1, min(q.limit or 20, _NPM_MAX_SEARCH_SIZE))
        params: dict[str, Any] = {required_filter: filter_value, "size": str(size)}

        offset = 0
        if q.filters.get("from") is not None:
            try:
                offset = int(q.filters["from"])
            except (TypeError, ValueError):
                raise ValueError(
                    f"npm {q.resource} filter 'from' must be a numeric offset, got {q.filters['from']!r}"
                ) from None
        if q.cursor:
            try:
                offset = int(q.cursor)
            except (TypeError, ValueError):
                raise ValueError(f"npm {q.resource} cursor must be a numeric offset, got {q.cursor!r}") from None
        if offset:
            params["from"] = str(offset)

        resp = await c.get(_NPM_SEARCH_ENDPOINT, params=params)
        resp.raise_for_status()
        body = resp.json()
        objects = body.get("objects", [])
        records = [o.get("package", o) for o in objects]

        total = body.get("total", len(records))
        next_cursor = None
        if records:
            has_more = offset + len(records) < total if isinstance(total, int) else len(records) >= size
            if has_more:
                next_cursor = str(offset + len(records))

        return ConnectorResult(
            records=records,
            total=total,
            next_cursor=next_cursor,
        )

    async def _get_package_files(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        pkg = q.filters.get("package")
        version = q.filters.get("version")
        if not pkg:
            raise ValueError("npm package_files query requires 'package' in filters")
        if not version:
            raise ValueError("npm package_files query requires 'version' in filters")
        resp = await c.get(f"/{pkg}/{version}/files")
        resp.raise_for_status()
        body = resp.json()
        files = body if isinstance(body, list) else body.get("files", [])
        return ConnectorResult(records=files, total=len(files))

    async def _scope_packages(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        return await self._search_registry(c, q, required_filter="scope")
