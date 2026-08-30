"""AzureReposConnector — async Azure Repos (Azure DevOps) API connector."""

import base64
from typing import Any

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
from modulo.core.ssrf import validate_outbound_url


class AzureReposConnector(ConnectorBase):
    """Read/write Azure Repos via the Azure DevOps REST API v7.0.

    Authentication uses a Personal Access Token (PAT) via HTTP Basic Auth
    with an empty username (":" + PAT encoded as Base64).

    The *organization* is the Azure DevOps organization name (e.g. "myorg"
    for ``https://dev.azure.com/myorg``).

    Supported query resources:
      "repos"    — list Git repositories; filters: {"project": "..."}
      "file"     — read a file; filters: {"project": "...", "repo": "...",
                   "path": "...", "ref": "main"}
      "pulls"    — list pull requests; filters: {"project": "...", "repo": "...",
                   "state": "active"}
      "commits"  — list commits; filters: {"project": "...", "repo": "...",
                   "branch": "main"}

    Supported write resources:
      "file"    — create/update a file via a commit; data: {"project": ...,
                  "repo": ..., "path": ..., "content": ..., "message": ...,
                  "branch": "main"}
      "pull"    — create a pull request; data: {"project": ..., "repo": ...,
                  "title": ..., "source_branch": ..., "target_branch": "main",
                  "description": "..."}
    """

    def __init__(self, token: str, organization: str) -> None:
        self._token = token
        self._organization = organization
        self._base_url = f"https://dev.azure.com/{organization}"

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.AZURE_REPOS

    def _headers(self) -> dict[str, str]:
        encoded = base64.b64encode(f":{self._token}".encode()).decode()
        return {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        validate_outbound_url(self._base_url)
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(),
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        """Verify API access by fetching the authenticated user's profile."""
        try:
            # Inside the try: a blocked base_url must be REPORTED as unhealthy by
            # the ValueError handler below, never raised out of a health check.
            validate_outbound_url(self._base_url)
            async with httpx.AsyncClient(headers=self._headers(), timeout=30) as client:
                r = await client.get(
                    "https://app.vssps.visualstudio.com/_apis/profile/profiles/me",
                    params={"api-version": "7.0"},
                )

            if r.status_code != 200:
                return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

            profile = r.json()
            if not isinstance(profile, dict):
                return HealthResult(ok=True, detail="")
            display_name = profile.get("displayName", "")
            return HealthResult(ok=True, detail=display_name)
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"Azure Repos API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="Azure Repos API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Azure Repos API connection error")
        except ValueError as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            match q.resource:
                case "repos":
                    project = q.filters.get("project", "")
                    r = await client.get(
                        f"/{project}/_apis/git/repositories",
                        params={"api-version": "7.0"},
                    )
                    r.raise_for_status()
                    body = r.json()
                    return ConnectorResult(
                        records=_safe_records(body, "value"),
                        total=_safe_paging_total(body, "count"),
                    )
                case "file":
                    project = q.filters["project"]
                    repo = q.filters["repo"]
                    path = q.filters["path"]
                    ref = q.filters.get("ref", "main")
                    r = await client.get(
                        f"/{project}/_apis/git/repositories/{repo}/items",
                        params={
                            "path": path,
                            "versionDescriptor.version": ref,
                            "api-version": "7.0",
                        },
                    )
                    r.raise_for_status()
                    return ConnectorResult(records=[{"content": r.text, "path": path, "ref": ref}])
                case "pulls":
                    project = q.filters["project"]
                    repo = q.filters["repo"]
                    state = q.filters.get("state", "active")
                    params: dict[str, Any] = {
                        "searchCriteria.status": state,
                        "api-version": "7.0",
                    }
                    r = await client.get(
                        f"/{project}/_apis/git/repositories/{repo}/pullrequests",
                        params=params,
                    )
                    r.raise_for_status()
                    body = r.json()
                    return ConnectorResult(
                        records=_safe_records(body, "value"),
                        total=_safe_paging_total(body, "count"),
                    )
                case "commits":
                    project = q.filters["project"]
                    repo = q.filters["repo"]
                    branch = q.filters.get("branch", "main")
                    params = {
                        "searchCriteria.itemVersion.version": branch,
                        "api-version": "7.0",
                    }
                    r = await client.get(
                        f"/{project}/_apis/git/repositories/{repo}/commits",
                        params=params,
                    )
                    r.raise_for_status()
                    body = r.json()
                    return ConnectorResult(
                        records=_safe_records(body, "value"),
                        total=_safe_paging_total(body, "count"),
                    )
                case _:
                    raise ValueError(f"Unsupported Azure Repos resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "file":
                    project = payload.data["project"]
                    repo = payload.data["repo"]
                    path = payload.data["path"]
                    content = payload.data["content"]
                    message = payload.data.get("message", "Update via Modulo")
                    branch = payload.data.get("branch", "main")

                    # Get the current commit ID for the branch
                    refs_r = await client.get(
                        f"/{project}/_apis/git/repositories/{repo}/refs",
                        params={"filter": f"heads/{branch}", "api-version": "7.0"},
                    )
                    refs_r.raise_for_status()
                    refs_body = refs_r.json()
                    refs = _safe_records(refs_body, "value")
                    if not refs:
                        raise ValueError(f"Branch {branch!r} not found in repo {repo!r}")
                    old_object_id = refs[0].get("objectId", "0000000000000000000000000000000000000000")

                    body: dict[str, Any] = {
                        "refUpdates": [
                            {
                                "name": f"refs/heads/{branch}",
                                "oldObjectId": old_object_id,
                            },
                        ],
                        "commits": [
                            {
                                "comment": message,
                                "changes": [
                                    {
                                        "changeType": "edit",
                                        "item": {"path": path},
                                        "newContent": {
                                            "content": content,
                                            "contentType": "rawtext",
                                        },
                                    },
                                ],
                            },
                        ],
                    }
                    r = await client.post(
                        f"/{project}/_apis/git/repositories/{repo}/pushes",
                        params={"api-version": "7.0"},
                        json=body,
                    )
                    r.raise_for_status()
                    result: dict[str, Any] = r.json()
                    return result
                case "pull":
                    project = payload.data["project"]
                    repo = payload.data["repo"]
                    body = {
                        "sourceRefName": f"refs/heads/{payload.data['source_branch']}",
                        "targetRefName": f"refs/heads/{payload.data.get('target_branch', 'main')}",
                        "title": payload.data["title"],
                    }
                    if "description" in payload.data:
                        body["description"] = payload.data["description"]
                    r = await client.post(
                        f"/{project}/_apis/git/repositories/{repo}/pullrequests",
                        params={"api-version": "7.0"},
                        json=body,
                    )
                    r.raise_for_status()
                    pr: dict[str, Any] = r.json()
                    return pr
                case _:
                    raise ValueError(f"Unsupported Azure Repos write resource: {payload.resource!r}")
