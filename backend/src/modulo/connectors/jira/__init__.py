"""JiraConnector — async Jira Cloud REST API v3 connector."""

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


class JiraConnector(ConnectorBase):
    """Read/write Jira issues via the REST API v3.

    Config (from config_json):
      "instance" — your-domain.atlassian.net (without https://)

    Credentials (from credentials_ciphertext):
      "email"    — Atlassian account email (for Basic auth)
      "api_token" — Atlassian API token (for Basic auth)
    Or:
      "token"    — OAuth/Personal Access Token

    Supported query resources:
      "issue"    — get a single issue; filters: {"issue_key": "PROJ-123"}
      "search"   — JQL search; filters: {"jql": "project = PROJ", "max_results": 50}

    Supported write resources:
      "issue"    — create an issue; data: {"project": {"key": "PROJ"}, "summary": "...",
                   "issuetype": {"name": "Task"}, ...}
      "issue_update" — update an issue; data: {"issue_key": "PROJ-123", "fields": {...}}
    """

    def __init__(self, instance: str, creds: dict[str, str]) -> None:
        self._instance = instance.rstrip("/")
        self._base_url = f"https://{self._instance}/rest/api/3"
        self._auth: httpx.Auth | None = None
        self._token: str | None = None

        if "token" in creds:
            self._token = creds["token"]
        elif "email" in creds and "api_token" in creds:
            self._auth = httpx.BasicAuth(username=creds["email"], password=creds["api_token"])
        else:
            raise ValueError(
                "Jira credentials must contain either 'token' (PAT/OAuth) or 'email' + 'api_token' (Basic auth)"
            )

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.JIRA

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(),
            auth=self._auth,
            timeout=30,
        )

    async def _call_api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make an HTTP request with wrapped exception handling."""
        async with self._client() as client:
            try:
                r = await client.request(method, path, **kwargs)
                r.raise_for_status()
                return r
            except httpx.HTTPStatusError as exc:
                raise ValueError(
                    f"Jira API HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                ) from exc
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                raise ValueError(f"Jira API connection error: {exc}") from exc

    async def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        """Safely parse JSON response, wrapping decode errors."""
        try:
            return response.json()
        except Exception as exc:
            raise ValueError(f"Jira API invalid response: {exc}") from exc

    async def health_check(self) -> HealthResult:
        """Verify connectivity by fetching the current user's profile."""
        try:
            async with self._client() as client:
                r = await client.get("/myself")

            if r.status_code != 200:
                return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

            user_info = await self._parse_json(r)
            display_name = user_info.get("displayName", "")

            return HealthResult(ok=True, detail=display_name)
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as exc:
            return HealthResult(ok=False, detail=f"Jira API error: {exc}")
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "issue":
                issue_key = q.filters.get("issue_key")
                if not issue_key:
                    raise ValueError("Jira issue query requires 'issue_key' filter")
                r = await self._call_api("GET", f"/issue/{issue_key}")
                data: dict[str, Any] = await self._parse_json(r)
                return ConnectorResult(records=[data])
            case "search":
                jql = q.filters.get("jql", "")
                max_results = q.filters.get("max_results", q.limit)
                r = await self._call_api(
                    "POST",
                    "/search",
                    json={"jql": jql, "maxResults": max_results},
                )
                body: dict[str, Any] = await self._parse_json(r)
                issues: list[dict[str, Any]] = body.get("issues", [])
                total = body.get("total", len(issues))
                return ConnectorResult(records=issues, total=total)
            case _:
                raise ValueError(f"Unsupported Jira resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "issue":
                r = await self._call_api("POST", "/issue", json=payload.data)
                created: dict[str, Any] = await self._parse_json(r)
                return created
            case "issue_update":
                if "issue_key" not in payload.data:
                    raise ValueError("Jira issue update requires 'issue_key' in data")
                issue_key = payload.data["issue_key"]
                fields: dict[str, Any] = payload.data["fields"]
                r = await self._call_api("PUT", f"/issue/{issue_key}", json={"fields": fields})
                return {"issue_key": issue_key, "updated": True}
            case _:
                raise ValueError(f"Unsupported Jira write resource: {payload.resource!r}")
