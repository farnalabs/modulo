"""PyPIConnector — async PyPI JSON/XML-RPC API connector for package metadata."""

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

_API_BASE = "https://pypi.org/pypi"


class PyPIConnector(ConnectorBase):
    def __init__(self, token: str = "") -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.PYPI

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
                resp = await c.get("/")
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="PyPI registry reachable")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid PyPI auth token")
                if resp.status_code == 403:
                    return HealthResult(ok=False, detail="PyPI token lacks required permissions")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Cannot connect to PyPI registry")
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
                    return await self._search_packages(q)
                case "package_files":
                    return await self._get_package_files(c, q)
                case "simple_list":
                    return await self._simple_list(c, q)
                case _:
                    raise ValueError(f"Unsupported PyPI resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        raise ValueError(f"PyPI registry is read-only: cannot write resource {payload.resource!r}")

    async def _get_package(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        pkg = q.filters.get("package")
        if not pkg:
            raise ValueError("PyPI package query requires 'package' in filters")
        resp = await c.get(f"/{pkg}/json")
        resp.raise_for_status()
        body = resp.json()
        return ConnectorResult(records=[body], total=1)

    async def _get_package_version(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        pkg = q.filters.get("package")
        version = q.filters.get("version")
        if not pkg:
            raise ValueError("PyPI package_version query requires 'package' in filters")
        if not version:
            raise ValueError("PyPI package_version query requires 'version' in filters")
        resp = await c.get(f"/{pkg}/{version}/json")
        resp.raise_for_status()
        body = resp.json()
        return ConnectorResult(records=[body], total=1)

    async def _search_packages(self, q: ConnectorQuery) -> ConnectorResult:
        text = q.filters.get("text")
        if not text:
            raise ValueError("PyPI search query requires 'text' in filters")
        import xmlrpc.client
        spec = {"name": text, "summary": text}
        operator = q.filters.get("operator", "and")
        xml_body = xmlrpc.client.dumps((spec, operator), "search")
        async with httpx.AsyncClient(
            base_url=_API_BASE,
            timeout=30,
        ) as c:
            resp = await c.post("/", content=xml_body, headers={"Content-Type": "text/xml"})
            resp.raise_for_status()
            results = xmlrpc.client.loads(resp.text)[0][0]
        records = []
        for r in results:
            records.append({
                "name": r.get("name", ""),
                "version": r.get("version", ""),
                "summary": r.get("summary", ""),
                "author": r.get("author", ""),
                "author_email": r.get("author_email", ""),
                "maintainer": r.get("maintainer", ""),
                "maintainer_email": r.get("maintainer_email", ""),
                "home_page": r.get("home_page", ""),
                "license": r.get("license", ""),
                "description": r.get("description", ""),
                "platform": r.get("platform", ""),
                "downloads": r.get("downloads", 0),
            })
        return ConnectorResult(
            records=records,
            total=len(records),
        )

    async def _get_package_files(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        pkg = q.filters.get("package")
        version = q.filters.get("version")
        if not pkg:
            raise ValueError("PyPI package_files query requires 'package' in filters")
        if not version:
            raise ValueError("PyPI package_files query requires 'version' in filters")
        resp = await c.get(f"/{pkg}/{version}/json")
        resp.raise_for_status()
        body = resp.json()
        releases = body.get("releases", {})
        files = releases.get(version, [])
        return ConnectorResult(records=files, total=len(files))

    async def _simple_list(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        pkg = q.filters.get("package")
        if not pkg:
            raise ValueError("PyPI simple_list query requires 'package' in filters")
        resp = await c.get(f"/{pkg}/json")
        resp.raise_for_status()
        body = resp.json()
        releases = body.get("releases", {})
        all_versions = sorted(releases.keys(), reverse=True)
        return ConnectorResult(records=[{"versions": all_versions}], total=len(all_versions))
