"""NpmConnector — async npm Registry API connector for package metadata."""

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
        text = q.filters.get("text")
        if not text:
            raise ValueError("npm search query requires 'text' in filters")
        params: dict[str, Any] = {"text": text, "size": str(q.limit)}
        if q.filters.get("from"):
            params["from"] = str(q.filters["from"])
        if q.cursor:
            params["from"] = q.cursor
        resp = await c.get("/-/v1/search", params=params)
        resp.raise_for_status()
        body = resp.json()
        objects = body.get("objects", [])
        records = [o.get("package", o) for o in objects]
        return ConnectorResult(
            records=records,
            total=body.get("total", len(records)),
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
        scope = q.filters.get("scope")
        if not scope:
            raise ValueError("npm scope_packages query requires 'scope' in filters")
        params: dict[str, Any] = {"scope": scope, "size": str(q.limit)}
        resp = await c.get("/-/v1/search", params=params)
        resp.raise_for_status()
        body = resp.json()
        objects = body.get("objects", [])
        records = [o.get("package", o) for o in objects]
        return ConnectorResult(
            records=records,
            total=body.get("total", len(records)),
        )
