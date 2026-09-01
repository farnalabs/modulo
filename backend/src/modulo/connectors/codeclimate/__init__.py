"""CodeClimateConnector — async Code Climate API v1 connector."""

import asyncio
from typing import Any

import httpx

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

_API_BASE = "https://api.codeclimate.com/v1"


class CodeClimateConnector(ConnectorBase):
    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.CODECLIMATE

    def _client(self) -> httpx.AsyncClient:
        return pinned_async_client_sync(
            _API_BASE,
            base_url=_API_BASE,
            headers={
                "Authorization": f"Token token={self._token}",
            },
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/repos", params={"limit": 1})
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="Code Climate API token validated")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid Code Climate auth token")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "repos":
                    return await self._list_repos(c, q)
                case "repo":
                    return await self._get_repo(c, q)
                case "snapshots":
                    return await self._list_snapshots(c, q)
                case "snapshot":
                    return await self._get_snapshot(c, q)
                case "test_reports":
                    return await self._list_test_reports(c, q)
                case "test_report":
                    return await self._get_test_report(c, q)
                case _:
                    raise ValueError(f"Unsupported Code Climate resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "test_report":
                    return await self._create_test_report(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported Code Climate write resource: {payload.resource!r}")

    async def _list_repos(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        github_slug = q.filters.get("github_slug")
        if github_slug:
            params["github_slug"] = github_slug
        if q.limit:
            params["limit"] = q.limit
        resp = await c.get("/repos", params=params)
        resp.raise_for_status()
        body = resp.json()
        data: list[dict[str, Any]] = _safe_records(body, "data")
        return ConnectorResult(records=data[: q.limit] if q.limit else data, total=len(data))

    async def _get_repo(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        repo_id = q.filters.get("id", "")
        if not repo_id:
            raise ValueError("Code Climate repo query requires 'id' in filters")
        resp = await c.get(f"/repos/{repo_id}")
        resp.raise_for_status()
        body = resp.json()
        record: dict[str, Any] = body.get("data", {}) if isinstance(body, dict) else {}
        return ConnectorResult(records=[record] if record else [])

    async def _list_snapshots(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        repo_id = q.filters.get("repo_id", "")
        if not repo_id:
            raise ValueError("Code Climate snapshots query requires 'repo_id' in filters")
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        resp = await c.get(f"/repos/{repo_id}/snapshots", params=params)
        resp.raise_for_status()
        body = resp.json()
        data: list[dict[str, Any]] = _safe_records(body, "data")
        return ConnectorResult(records=data[: q.limit] if q.limit else data, total=len(data))

    async def _get_snapshot(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        repo_id = q.filters.get("repo_id", "")
        snapshot_id = q.filters.get("id", "")
        if not repo_id:
            raise ValueError("Code Climate snapshot query requires 'repo_id' in filters")
        if not snapshot_id:
            raise ValueError("Code Climate snapshot query requires 'id' in filters")
        resp = await c.get(f"/repos/{repo_id}/snapshots/{snapshot_id}")
        resp.raise_for_status()
        body = resp.json()
        record: dict[str, Any] = body.get("data", {}) if isinstance(body, dict) else {}
        return ConnectorResult(records=[record] if record else [])

    async def _list_test_reports(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        repo_id = q.filters.get("repo_id", "")
        if not repo_id:
            raise ValueError("Code Climate test_reports query requires 'repo_id' in filters")
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        resp = await c.get(f"/repos/{repo_id}/test_reports", params=params)
        resp.raise_for_status()
        body = resp.json()
        data: list[dict[str, Any]] = _safe_records(body, "data")
        return ConnectorResult(records=data[: q.limit] if q.limit else data, total=len(data))

    async def _get_test_report(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        repo_id = q.filters.get("repo_id", "")
        report_id = q.filters.get("id", "")
        if not repo_id:
            raise ValueError("Code Climate test_report query requires 'repo_id' in filters")
        if not report_id:
            raise ValueError("Code Climate test_report query requires 'id' in filters")
        resp = await c.get(f"/repos/{repo_id}/test_reports/{report_id}")
        resp.raise_for_status()
        body = resp.json()
        record: dict[str, Any] = body.get("data", {}) if isinstance(body, dict) else {}
        return ConnectorResult(records=[record] if record else [])

    async def _create_test_report(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        repo_id = data.get("repo_id")
        if not repo_id:
            raise ValueError("Code Climate test_report write requires 'repo_id' in data")
        duration = data.get("duration")
        if duration is None:
            raise ValueError("Code Climate test_report write requires 'duration' in data")
        exit_code = data.get("exit_code")
        if exit_code is None:
            raise ValueError("Code Climate test_report write requires 'exit_code' in data")
        branch = data.get("branch", "")
        commit_sha = data.get("commit_sha", "")
        if not commit_sha:
            raise ValueError("Code Climate test_report write requires 'commit_sha' in data")
        body: dict[str, Any] = {
            "data": {
                "type": "test_reports",
                "attributes": {
                    "duration": duration,
                    "exit_code": exit_code,
                    "branch": branch,
                    "commit_sha": commit_sha,
                },
            },
        }
        files = data.get("files")
        if files is not None:
            body["data"]["attributes"]["files"] = files
        resp = await c.post(f"/repos/{repo_id}/test_reports", json=body)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result
