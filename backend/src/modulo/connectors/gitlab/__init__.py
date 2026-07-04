"""GitLabConnector — async GitLab API connector via REST API v4."""

import base64
from typing import Any
from urllib.parse import quote

import httpx

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_GITLAB_API = "https://gitlab.com/api/v4"

REQUIRED_SCOPES = frozenset({"read_api", "write_repository", "api"})


def _project_path(project_id: str) -> str:
    """URL-encode a project path like 'group/subgroup/project'."""
    return quote(project_id, safe="")


class GitLabConnector(ConnectorBase):
    """Read/write GitLab via the REST API v4.

    Supported query resources:
      "projects"          — list projects accessible to the token
      "file"              — read a file
      "mrs"               — list merge requests (legacy, alias for merge_requests)
      "issues"            — list project issues (filters: state, labels, milestone, search, sort, order_by, assignee_id)
      "issue"             — get single issue by IID
      "labels"            — list project labels
      "label"             — get single label by ID
      "milestones"        — list project milestones
      "issue_notes"       — list notes on an issue
      "issue_discussions" — list discussions on an issue
      "merge_requests"    — list merge requests (filters: state, labels, milestone)
      "merge_request"     — get single MR by IID
      "branch"            — get single branch
      "branches"          — list branches
      "tags"              — list tags
      "pipelines"         — list pipelines
      "jobs"              — list jobs for a pipeline

    Supported write resources:
      "file"              — create/update a file
      "mr"                — create a merge request (legacy)
      "issue"             — create an issue
      "issue_update"      — update an issue (close/reopen, edit title/description)
      "issue_note"        — add a note to an issue
      "issue_label"       — replace labels on an issue
      "label"             — create a project label
      "milestone"         — create a project milestone
      "merge_request"     — create a merge request (filters: source_branch, target_branch, title, description)
      "pipeline_run"      — trigger a pipeline
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def _require_filter(self, filters: dict[str, Any], key: str, resource: str) -> Any:
        try:
            return filters[key]
        except KeyError:
            raise ValueError(f"Missing required filter {key!r} for GitLab resource {resource!r}")

    async def _call_api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make an HTTP request with wrapped exception handling."""
        async with self._client() as client:
            try:
                r = await client.request(method, path, **kwargs)
                r.raise_for_status()
                return r
            except httpx.HTTPStatusError as exc:
                raise ValueError(
                    f"GitLab API HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                ) from exc
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                raise ValueError(f"GitLab API connection error: {exc}") from exc

    async def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        """Safely parse JSON response, wrapping decode errors."""
        try:
            return response.json()
        except Exception as exc:
            raise ValueError(f"GitLab API invalid response: {exc}") from exc

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.GITLAB

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=_GITLAB_API, headers=self._headers(), timeout=30)

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as client:
                r = await client.get("/user")

            if r.status_code != 200:
                return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

            try:
                user_info = r.json()
            except Exception:
                return HealthResult(ok=False, detail=f"Invalid JSON in /user response: {r.text[:200]}")
            username = user_info.get("username", "")

            async with self._client() as client:
                projects_r = await client.get("/projects", params={"per_page": 1})
                if projects_r.status_code in (401, 403):
                    return HealthResult(ok=False, detail="Missing scopes: API access not granted")

            return HealthResult(ok=True, detail=username)
        except httpx.RequestError as e:
            return HealthResult(ok=False, detail=str(e))

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "projects":
                r = await self._call_api("GET", "/projects", params={"per_page": q.limit})
                try:
                    data: list[dict[str, Any]] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ConnectorResult(records=data, total=len(data))
            case "file":
                project = self._require_filter(q.filters, "project", q.resource)
                path = self._require_filter(q.filters, "path", q.resource)
                ref = q.filters.get("ref", "main")
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/repository/files/{quote(path, safe='')}",
                    params={"ref": ref},
                )
                try:
                    info: dict[str, Any] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                if "content" in info:
                    info["content"] = base64.b64decode(info["content"]).decode("utf-8")
                return ConnectorResult(records=[info])
            case "mrs" | "merge_requests":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                params: dict[str, Any] = {"per_page": q.limit}
                if "state" in q.filters:
                    params["state"] = q.filters["state"]
                if "labels" in q.filters:
                    params["labels"] = q.filters["labels"]
                if "milestone" in q.filters:
                    params["milestone"] = q.filters["milestone"]
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/merge_requests",
                    params=params,
                )
                try:
                    mrs: list[dict[str, Any]] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ConnectorResult(records=mrs, total=len(mrs))
            case "merge_request":
                project = self._require_filter(q.filters, "project", q.resource)
                mr_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/merge_requests/{mr_iid}",
                )
                try:
                    return ConnectorResult(records=[r.json()])
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
            case "issues":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                params = {"per_page": q.limit}
                for key in ("state", "labels", "milestone", "search", "sort", "order_by", "assignee_id"):
                    if key in q.filters:
                        params[key] = q.filters[key]
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues",
                    params=params,
                )
                try:
                    issues: list[dict[str, Any]] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ConnectorResult(records=issues, total=len(issues))
            case "issue":
                project = self._require_filter(q.filters, "project", q.resource)
                issue_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues/{issue_iid}",
                )
                try:
                    return ConnectorResult(records=[r.json()])
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
            case "labels":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/labels",
                    params={"per_page": q.limit},
                )
                try:
                    labels: list[dict[str, Any]] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ConnectorResult(records=labels, total=len(labels))
            case "label":
                project = self._require_filter(q.filters, "project", q.resource)
                label_id = self._require_filter(q.filters, "label_id", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/labels/{label_id}",
                )
                try:
                    return ConnectorResult(records=[r.json()])
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
            case "milestones":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/milestones",
                    params={"per_page": q.limit},
                )
                try:
                    milestones: list[dict[str, Any]] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ConnectorResult(records=milestones, total=len(milestones))
            case "issue_notes":
                project = self._require_filter(q.filters, "project", q.resource)
                issue_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                params = {"per_page": q.limit}
                for key in ("sort", "order_by"):
                    if key in q.filters:
                        params[key] = q.filters[key]
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues/{issue_iid}/notes",
                    params=params,
                )
                try:
                    notes: list[dict[str, Any]] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ConnectorResult(records=notes, total=len(notes))
            case "issue_discussions":
                project = self._require_filter(q.filters, "project", q.resource)
                issue_iid = self._require_filter(q.filters, "iid", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/issues/{issue_iid}/discussions",
                    params={"per_page": q.limit},
                )
                try:
                    discussions: list[dict[str, Any]] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ConnectorResult(records=discussions, total=len(discussions))
            case "branch":
                project = self._require_filter(q.filters, "project", q.resource)
                branch_name = self._require_filter(q.filters, "name", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/repository/branches/{quote(branch_name, safe='')}",
                )
                try:
                    return ConnectorResult(records=[r.json()])
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
            case "branches":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/repository/branches",
                    params={"per_page": q.limit},
                )
                try:
                    branches: list[dict[str, Any]] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ConnectorResult(records=branches, total=len(branches))
            case "tags":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/repository/tags",
                    params={"per_page": q.limit},
                )
                try:
                    tags: list[dict[str, Any]] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ConnectorResult(records=tags, total=len(tags))
            case "pipelines":
                project = self._require_filter(q.filters, "project", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/pipelines",
                    params={"per_page": q.limit},
                )
                try:
                    pipelines: list[dict[str, Any]] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ConnectorResult(records=pipelines, total=len(pipelines))
            case "jobs":
                project = self._require_filter(q.filters, "project", q.resource)
                pipeline_id = self._require_filter(q.filters, "pipeline_id", q.resource)
                encoded = _project_path(project)
                r = await self._call_api(
                    "GET",
                    f"/projects/{encoded}/pipelines/{pipeline_id}/jobs",
                    params={"per_page": q.limit},
                )
                try:
                    jobs: list[dict[str, Any]] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ConnectorResult(records=jobs, total=len(jobs))
            case _:
                raise ValueError(f"Unsupported GitLab resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "file":
                project = self._require_filter(payload.data, "project", payload.resource)
                path = self._require_filter(payload.data, "path", payload.resource)
                encoded = _project_path(project)
                body: dict[str, Any] = {
                    "content": payload.data["content"],
                    "commit_message": payload.data.get("message", "Update via Modulo"),
                }
                if payload.data.get("sha"):
                    body["sha"] = payload.data["sha"]
                r = await self._call_api(
                    "PUT",
                    f"/projects/{encoded}/repository/files/{quote(path, safe='')}",
                    json=body,
                )
                try:
                    result: dict[str, Any] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return result
            case "mr" | "merge_request":
                project = self._require_filter(payload.data, "project", payload.resource)
                source_branch = self._require_filter(payload.data, "source_branch", payload.resource)
                title = self._require_filter(payload.data, "title", payload.resource)
                encoded = _project_path(project)
                body = {
                    "source_branch": source_branch,
                    "target_branch": payload.data.get("target_branch", "main"),
                    "title": title,
                }
                if "description" in payload.data:
                    body["description"] = payload.data["description"]
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/merge_requests",
                    json=body,
                )
                try:
                    mr: dict[str, Any] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return mr
            case "issue":
                project = self._require_filter(payload.data, "project", payload.resource)
                title = self._require_filter(payload.data, "title", payload.resource)
                encoded = _project_path(project)
                body = {
                    "title": title,
                }
                if "description" in payload.data:
                    body["description"] = payload.data["description"]
                if "labels" in payload.data:
                    body["labels"] = payload.data["labels"]
                if "milestone_id" in payload.data:
                    body["milestone_id"] = payload.data["milestone_id"]
                if "assignee_ids" in payload.data:
                    body["assignee_ids"] = payload.data["assignee_ids"]
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/issues",
                    json=body,
                )
                try:
                    issue: dict[str, Any] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return issue
            case "issue_update":
                project = self._require_filter(payload.data, "project", payload.resource)
                issue_iid = self._require_filter(payload.data, "iid", payload.resource)
                encoded = _project_path(project)
                body = {}
                for key in ("state_event", "title", "description"):
                    if key in payload.data:
                        body[key] = payload.data[key]
                r = await self._call_api(
                    "PUT",
                    f"/projects/{encoded}/issues/{issue_iid}",
                    json=body,
                )
                try:
                    updated: dict[str, Any] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return updated
            case "issue_note":
                project = self._require_filter(payload.data, "project", payload.resource)
                issue_iid = self._require_filter(payload.data, "iid", payload.resource)
                encoded = _project_path(project)
                body = self._require_filter(payload.data, "body", payload.resource)
                body = {
                    "body": body,
                }
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/issues/{issue_iid}/notes",
                    json=body,
                )
                try:
                    note: dict[str, Any] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return note
            case "issue_label":
                project = self._require_filter(payload.data, "project", payload.resource)
                issue_iid = self._require_filter(payload.data, "iid", payload.resource)
                encoded = _project_path(project)
                labels = self._require_filter(payload.data, "labels", payload.resource)
                body = {
                    "labels": labels,
                }
                r = await self._call_api(
                    "PUT",
                    f"/projects/{encoded}/issues/{issue_iid}",
                    json=body,
                )
                try:
                    labeled: dict[str, Any] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return labeled
            case "label":
                project = self._require_filter(payload.data, "project", payload.resource)
                name = self._require_filter(payload.data, "name", payload.resource)
                encoded = _project_path(project)
                body = {
                    "name": name,
                    "color": payload.data.get("color", "#428BCA"),
                }
                if "description" in payload.data:
                    body["description"] = payload.data["description"]
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/labels",
                    json=body,
                )
                try:
                    label_res: dict[str, Any] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return label_res
            case "milestone":
                project = self._require_filter(payload.data, "project", payload.resource)
                title = self._require_filter(payload.data, "title", payload.resource)
                encoded = _project_path(project)
                body = {
                    "title": title,
                }
                if "description" in payload.data:
                    body["description"] = payload.data["description"]
                if "due_date" in payload.data:
                    body["due_date"] = payload.data["due_date"]
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/milestones",
                    json=body,
                )
                try:
                    ms: dict[str, Any] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return ms
            case "pipeline_run":
                project = self._require_filter(payload.data, "project", payload.resource)
                ref = self._require_filter(payload.data, "ref", payload.resource)
                encoded = _project_path(project)
                body = {
                    "ref": ref,
                }
                if "variables" in payload.data:
                    body["variables"] = payload.data["variables"]
                r = await self._call_api(
                    "POST",
                    f"/projects/{encoded}/pipeline",
                    json=body,
                )
                try:
                    pipeline: dict[str, Any] = r.json()
                except Exception as exc:
                    raise ValueError(f"GitLab API invalid response: {exc}") from exc
                return pipeline
            case _:
                raise ValueError(f"Unsupported GitLab write resource: {payload.resource!r}")
