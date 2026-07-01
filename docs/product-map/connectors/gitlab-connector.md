---
id: feat-connectors-gitlab
prd: 8.6
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/connector_health.feature
  - backend/tests/bdd/features/connectors/gitlab_issues.feature
unit-tests:
  - backend/tests/unit/connectors/test_gitlab.py
  - backend/tests/unit/connectors/test_gitlab_issues.py
code:
  - backend/src/modulo/connectors/gitlab/__init__.py
  - backend/src/modulo/connectors/base.py
depends-on:
  - feat-connectors-hub
status: partial
qa-history:
  - 2026-07-01: improve-architecture (index 33) — fixed raw KeyError in 14 query + 8 write case branches by replacing bare dict["key"] with _require_filter(). Added 9 missing-filter error-path unit tests. Marked [ ]→[x] for "Missing required filter raises ValueError". Updated capabilities description to reflect ISSUE_READ, ISSUE_WRITE, ISSUE_SEARCH, TRIGGER_RUN. 62/62 unit tests pass.
---

# GitLab Connector

Async GitLab REST API v4 connector implementing `ConnectorBase`. Provides read/write access to GitLab projects for agent pipelines. Authenticated via Personal Access Token. Part of the `git-host` connector type family alongside `FilesystemConnector` and `GitHubConnector`.

## Behaviours

### Authentication — PAT-based API access

- [x] Authenticate all requests via `Authorization: Bearer <token>` header
- [x] Set `Accept: application/json` header on all requests
- [x] Use `httpx.AsyncClient` with base URL `https://gitlab.com/api/v4`
- [x] `health_check()` calls `GET /user` to validate token
- [x] Return `HealthResult(ok=False)` with HTTP status on non-200
- [x] Return `HealthResult(ok=True)` with authenticated user info on success
- [ ] Accept configurable base URL for self-hosted GitLab instances (hard-coded to `gitlab.com/api/v4`)
- [ ] Report `X-Request-Id` on API errors for GitLab support debugging

### OAuth Scopes — capability verification

- [x] Declare required scopes: `read_api`, `write_repository`, `api` (code constant)
- [x] Verify `read_api` scope by probing `GET /projects` during health check
- [ ] Verify `write_repository` scope during health check — not probed
- [ ] Verify `api` scope during health check — not probed
- [ ] Report missing scopes individually in health check detail
- [ ] Block run start when scopes are insufficient (pre-run health check in ConnectorHub)

### Project Operations — listing and discovery

- [x] List projects via `query("projects")` with optional search filter
- [x] Return project ID, name, and web URL in results
- [x] Raise `ValueError` for unsupported resources in `query()`
- [ ] Support pagination cursor via `Link` header or pagination query params
- [ ] Filter projects by membership, visibility, or ownership
- [x] Support `limit` parameter in project queries

### File Operations — read and write via Repository Files API

- [x] Read file via `query("file")` with `project_id`, `path`, `ref` filters
- [x] Decode base64 content from GitLab API response
- [x] Write file via `write("file")` with `project_id`, `path`, `content`, `commit_message`, optional `branch`
- [x] Call `PUT /projects/{id}/repository/files/{path}` with JSON body
- [x] Default branch to `"main"` when not specified
- [x] Return created/updated file info from API
- [x] `raise_for_status()` on HTTP errors
- [ ] Recursive directory listing — not implemented
- [ ] Batch file operations — not implemented
- [ ] File deletion — not implemented
- [ ] Path traversal protection — relies on GitLab API server-side; no local validation

### Merge Request Operations — listing and creation

- [x] List merge requests via `query("mrs")` with `project_id` and optional `state` filter
- [x] Default MR state filter to `"opened"`
- [x] Create merge request via `write("mr")` with `project_id`, `title`, `description`, `source_branch`, `target_branch`
- [ ] Add merge request comment — not implemented
- [ ] Merge merge request — not implemented
- [ ] Accept merge request with squash — not implemented
- [ ] List MR diff/changed files — not implemented
- [ ] Set MR labels — not implemented
- [ ] Request MR approval — not implemented
- [ ] Approve MR via API — not implemented
- [ ] `query("mrs")` does not support pagination cursor
- [ ] Pagination for `query("mrs")` — `next_cursor` always `None`

### Capability Declaration

- [x] `ConnectorType.GITLAB` defined in `base.py` enum
- [x] `GitLabConnector.connector_type` returns `ConnectorType.GITLAB`
- [x] `ConnectorType.GITLAB.capabilities` returns `{read, write, git_push, create_pr, issue_read, issue_write, issue_search, trigger_run}` in `base.py`

### Health Check — connectivity and credential validation

- [x] Validate token by calling `GET /user` — fail if status != 200
- [x] Probe API access by calling `GET /projects` during health check
- [x] Return authenticated user info in `detail` on success
- [x] Skip per-project repository-level checks
- [ ] Detect expired tokens vs insufficient scopes vs network errors
- [ ] Per-operation scope verification — no granular check before `write()` calls
- [ ] Report self-hosted GitLab version for diagnostic purposes

### Error Handling

- [x] Missing required filter key raises ValueError with descriptive message
- [ ] API error (non-2xx response) raises httpx.HTTPStatusError via raise_for_status()
- [ ] Network/socket error propagates as httpx exception during health check
- [ ] Missing token during construction is not validated until first API call
- [ ] Project path encoding fails gracefully on malformed project IDs (e.g. None, numbers)

## Known Gaps

- [ ] **No self-hosted GitLab support**: API base URL is hard-coded to `https://gitlab.com/api/v4`
- [ ] **File deletion unimplemented**: no `write("file_delete")` or equivalent
- [ ] **MR operations limited**: only listing and creation work — no comments, merges, approvals, or labels
- [ ] **Scope verification incomplete**: health check doesn't verify individual scopes
- [ ] **No pagination**: `query("projects")` and `query("mrs")` don't return `next_cursor`
- [x] **BDD scenarios**: `gitlab_issues.feature` (25 scenarios) covers GitLab operations + `connector_health.feature` (shared) for health checks
- [x] **Unit tests**: `test_gitlab.py` and `test_gitlab_issues.py` cover connector behaviour
- [ ] **No rate-limit handling**: no 429 retry, no GitLab `RateLimit-*` header inspection

