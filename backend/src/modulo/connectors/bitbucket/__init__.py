"""BitbucketConnector — async Bitbucket Cloud API connector."""

import base64
from typing import Any, cast

import httpx

from modulo.connectors._safe_page import safe_paging_total as _safe_paging_total
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

_BITBUCKET_API = "https://api.bitbucket.org/2.0"


class BitbucketConnector(ConnectorBase):
    """Read/write Bitbucket Cloud via the REST API v2.0.

    Supports two authentication modes:
      - OAuth 2.0: pass ``token`` (Bearer token)
      - App password: pass ``username`` and ``app_password`` (HTTP Basic)

    Supported query resources:
      "repos"   — list repositories for a workspace; filters: {"workspace": "myteam"}
      "file"    — read a file; filters: {"workspace": "myteam", "repo": "slug",
                 "path": "...", "ref": "main"}
      "pulls"   — list pull requests; filters: {"workspace": "myteam", "repo": "slug",
                 "state": "OPEN"}
      "issues"  — list issues; filters: {"workspace": "myteam", "repo": "slug",
                 "state": "new"}

    Supported write resources:
      "file"    — create/update a file; data: {"workspace": ..., "repo": ..., "path": ...,
                 "content": ..., "message": ...}
      "pull"    — create a pull request; data: {"workspace": ..., "repo": ...,
                 "title": ..., "source_branch": ..., "target_branch": ...,
                 "description": ...}
    """

    def __init__(
        self,
        token: str | None = None,
        username: str | None = None,
        app_password: str | None = None,
    ) -> None:
        if token:
            self._auth_header = {"Authorization": f"Bearer {token}"}
        elif username and app_password:
            encoded = base64.b64encode(f"{username}:{app_password}".encode()).decode()
            self._auth_header = {"Authorization": f"Basic {encoded}"}
        else:
            raise ValueError("Provide either token (OAuth 2.0) or username+app_password")

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.BITBUCKET

    def _headers(self) -> dict[str, str]:
        return {
            **self._auth_header,
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=_BITBUCKET_API, headers=self._headers(), timeout=30)

    async def health_check(self) -> HealthResult:
        """Verify API access by fetching the authenticated user."""
        try:
            async with self._client() as client:
                r = await client.get("/user")

            if r.status_code != 200:
                return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

            user_info = r.json()
            if not isinstance(user_info, dict):
                return HealthResult(ok=True, detail="")
            username = user_info.get("username", "") or user_info.get("display_name", "")
            return HealthResult(ok=True, detail=username)
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"Bitbucket API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="Bitbucket API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Bitbucket API connection error")
        except ValueError as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            match q.resource:
                case "repos":
                    return await self._query_repos(client, q)
                case "file":
                    return await self._query_file(client, q)
                case "pulls":
                    return await self._query_pulls(client, q)
                case "issues":
                    return await self._query_issues(client, q)
                case _:
                    raise ValueError(f"Unsupported Bitbucket resource: {q.resource!r}")

    @staticmethod
    def _require_filter(q: ConnectorQuery, key: str, resource: str) -> str:
        value = q.filters.get(key)
        if not value:
            raise ValueError(f"Bitbucket {resource} query requires '{key}' filter")
        return cast(str, value)

    async def _query_repos(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        workspace = q.filters.get("workspace", "")
        r = await client.get(
            f"/repositories/{workspace}",
            params={"pagelen": q.limit},
        )
        r.raise_for_status()
        body = r.json()
        return ConnectorResult(
            records=_safe_records(body, "values"),
            total=_safe_paging_total(body, "size"),
        )

    async def _query_file(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        workspace = self._require_filter(q, "workspace", "file")
        repo = self._require_filter(q, "repo", "file")
        path = self._require_filter(q, "path", "file")
        ref = q.filters.get("ref", "main")
        r = await client.get(
            f"/repositories/{workspace}/{repo}/src/{ref}/{path}",
            headers={**self._headers(), "Accept": "*/*"},
        )
        r.raise_for_status()
        return ConnectorResult(records=[{"content": r.text, "path": path, "ref": ref}])

    async def _query_pulls(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        workspace = self._require_filter(q, "workspace", "pulls")
        repo = self._require_filter(q, "repo", "pulls")
        state = q.filters.get("state", "OPEN")
        params: dict[str, Any] = {"pagelen": q.limit, "state": state}
        r = await client.get(
            f"/repositories/{workspace}/{repo}/pullrequests",
            params=params,
        )
        r.raise_for_status()
        body = r.json()
        return ConnectorResult(
            records=_safe_records(body, "values"),
            total=_safe_paging_total(body, "size"),
        )

    async def _query_issues(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        workspace = self._require_filter(q, "workspace", "issues")
        repo = self._require_filter(q, "repo", "issues")
        params: dict[str, Any] = {"pagelen": q.limit}
        if "state" in q.filters:
            params["state"] = q.filters["state"]
        r = await client.get(
            f"/repositories/{workspace}/{repo}/issues",
            params=params,
        )
        r.raise_for_status()
        body = r.json()
        return ConnectorResult(
            records=_safe_records(body, "values"),
            total=_safe_paging_total(body, "size"),
        )

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "file":
                    workspace = payload.data["workspace"]
                    repo = payload.data["repo"]
                    path = payload.data["path"]
                    body: dict[str, Any] = {
                        "message": payload.data.get("message", "Update via Modulo"),
                        "branch": payload.data.get("branch", "main"),
                        path: payload.data["content"],
                    }
                    r = await client.post(
                        f"/repositories/{workspace}/{repo}/src",
                        data=body,
                    )
                    r.raise_for_status()
                    result: dict[str, Any] = r.json()
                    return result
                case "pull":
                    workspace = payload.data["workspace"]
                    repo = payload.data["repo"]
                    body = {
                        "title": payload.data["title"],
                        "source": {"branch": {"name": payload.data["source_branch"]}},
                        "destination": {"branch": {"name": payload.data.get("target_branch", "main")}},
                    }
                    if "description" in payload.data:
                        body["description"] = payload.data["description"]
                    r = await client.post(
                        f"/repositories/{workspace}/{repo}/pullrequests",
                        json=body,
                    )
                    r.raise_for_status()
                    pr: dict[str, Any] = r.json()
                    return pr
                case _:
                    raise ValueError(f"Unsupported Bitbucket write resource: {payload.resource!r}")
