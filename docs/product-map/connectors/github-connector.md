---
id: feat-connectors-github
prd: 8.6
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/github_connector.feature
unit-tests:
  - backend/tests/unit/connectors/test_github.py
  - backend/tests/unit/connectors/test_github_scopes.py
  - backend/tests/unit/connectors/test_github_issues.py
  - backend/tests/unit/connectors/test_github_resilience.py
code:
  - backend/src/modulo/connectors/github/__init__.py
  - backend/src/modulo/connectors/base.py
depends-on:
  - feat-connectors-hub
status: partial
---

# GitHub Connector

Async GitHub REST API connector implementing `ConnectorBase`. Provides read/write access to GitHub repositories for agent pipelines. Authenticated via fine-grained PAT. Part of the `git-host` connector type family alongside `FilesystemConnector`.

## Behaviours

### Authentication — PAT-based API access

- [x] Authenticate all requests via `Authorization: Bearer <token>` header
- [x] Set `X-GitHub-Api-Version: 2022-11-28` header on all requests
- [x] Set `Accept: application/vnd.github+json` header
- [x] Use `httpx.AsyncClient` with 30-second timeout
- [x] `health_check()` calls `GET /user` to validate token
- [x] Return `HealthResult(ok=False)` with HTTP status and response body on non-200
- [x] Return `HealthResult(ok=True)` with authenticated user login on success
- [ ] Accept configurable API base URL for GHES (hard-coded to `api.github.com`)

### OAuth Scopes — capability verification

- [x] Declare required scopes via `REQUIRED_SCOPES` constant: `{"repo", "read:org"}` (classic PAT scopes from `X-OAuth-Scopes` header)
- [x] Verify scopes by reading `X-OAuth-Scopes` response header from `GET /user` during health check
- [x] Report missing scopes in health check detail with scope names (e.g. `Missing scopes: read:org`)
- [ ] Verify `pull_requests:write` scope — not in `REQUIRED_SCOPES`; classic PAT `repo` scope encompasses PR access, but fine-grained PAT would need explicit `pull_requests:write`
- [ ] **Scope mismatch with PRD**: PRD §7.11 specifies fine-grained PAT scopes (`contents:read`, `contents:write`, `pull_requests:write`) but code uses classic PAT scopes (`repo`, `read:org`). These are different scope systems — fine-grained PATs are more restrictive and granular. The `repo` classic scope is broadly equivalent to `contents:read` + `contents:write` + `pull_requests:write` combined.
- [ ] Report missing scopes as individually named errors (e.g. `missing_scope:repo`) — health check mentions scope names in prose but no machine-parseable error codes
- [ ] Block run start when scopes are insufficient (pre-run health check in ConnectorHub)

### PR Operations — listing and commenting

- [x] List pull requests via `query("pulls")` with `repo` and `state` filters
- [x] Filter PRs by state (default `"open"`)
- [x] Limit results via `q.limit`
- [x] Raise `ValueError` for unsupported resources in `query()`
- [ ] Post PR comment (BDD scenario exists in `github.feature` but no `write("pr_comment")` implementation)
- [ ] **Create PR** — `Capability.CREATE_PR` declared in `base.py` but no `write("pr")` implementation in `GitHubConnector`
- [ ] Merge PR — no implementation
- [ ] List PR files/changed files — not implemented
- [ ] Get PR diff — not implemented
- [ ] Request PR review — not implemented
- [ ] Add PR labels — not implemented
- [ ] `query("pulls")` does not support pagination cursor — `next_cursor` always `None`

### File Operations — read and write via Contents API

- [x] Read file via `query("file")` with `repo`, `path`, `ref` (branch) filters
- [x] Default `ref` to `"main"` when not specified
- [x] Call `GET /repos/{owner}/{repo}/contents/{path}?ref={ref}`
- [x] Return raw API response as single record in results
- [x] Write file via `write("file")` with `repo`, `path`, `content`, `message`, optional `sha`
- [x] Call `PUT /repos/{owner}/{repo}/contents/{path}` with JSON body
- [x] Include `sha` for updates (required by GitHub Contents API for existing files)
- [x] Return created/updated resource dict from API
- [x] `raise_for_status()` on HTTP errors
- [ ] File content is expected as raw string — no base64 encode/decode in connector; caller must handle encoding
- [ ] Recursive file listing — not implemented (`/contents` only returns one level)
- [ ] Batch file operations — not implemented
- [ ] Path traversal protection — relies on GitHub API server-side; no local validation

### Issue Operations — create, read, update, and comment

- [x] List issues via `query("issues")` with `repo` and optional `state`, `labels`, `since`, `per_page` filters
- [x] Get single issue by number via `query("issue")` with `repo` and `issue_number` filters
- [x] List issue comments via `query("issue_comments")` with `repo` and `issue_number` filters
- [x] List issue events via `query("issue_events")` with `repo` and `issue_number` filters
- [x] List assignees via `query("assignees")` with `repo` filter
- [x] List issue timeline via `query("timeline")` with `repo` and `issue_number` filters
- [x] Create issue via `write("issue")` with `repo`, `title`, optional `body`, `labels`, `assignees`
- [x] Update issue fields via `write("issue_update")` with `repo`, `issue_number` and any updatable field
- [x] Add issue comment via `write("issue_comment")` with `repo`, `issue_number`, `body`
- [x] Add labels to issue via `write("issue_label")` with `repo`, `issue_number`, `labels`
- [x] React to issue via `write("issue_reaction")` with `repo`, `issue_number`, `reaction` content
- [x] Create label via `write("label")` with `repo`, `name`, `color`, optional `description`
- [x] Create milestone via `write("milestone")` with `repo`, `title`, optional `description`, `due_on`
- [ ] Search issues — not implemented
- [ ] Assign issue — not implemented (`assignees` filter is query-only)

### Health Check — connectivity and credential validation

- [x] Validate token by calling `GET /user` — fail if status != 200
- [x] Probe scopes via `X-OAuth-Scopes` response header from `GET /user` — fail if `REQUIRED_SCOPES` not satisfied
- [x] Return authenticated user login in `detail` on success
- [x] Return HTTP status and truncated body on failure
- [ ] Full scope check — only `repo` and `read:org` are verified; `pull_requests:write` not probed
- [ ] Detect expired tokens vs insufficient scopes vs network errors — all collapsed to generic "HTTP {code}: {body}"
- [ ] Per-operation scope verification — no granular check before `write()` calls

### ConnectorType Capability Declaration

- [x] `ConnectorType.GITHUB.capabilities` returns `{READ, WRITE, GIT_PUSH, CREATE_PR}` in `base.py`
- [x] `GitHubConnector.connector_type` returns `ConnectorType.GITHUB`
- [ ] `CREATE_PR` capability declared but `write("pr")` not implemented — capability mismatch
- [x] `ISSUE_READ` and `ISSUE_WRITE` assigned to `ConnectorType.GITHUB` in `base.py`

## Error Handling

### API & Network Resilience

- [x] HTTP 429 rate limit raises `ValueError` with status code — tested
- [x] HTTP 500 server error raises `ValueError` with status code — tested
- [x] HTTP 422 unprocessable on write raises `ValueError` with status code — tested
- [x] HTTP 403 forbidden raises `ValueError` with status code — tested
- [x] Connection error raises `ValueError` with "connection error" message — tested
- [x] Invalid JSON response raises `ValueError` with "invalid JSON" message — tested
- [x] Health check catches all exceptions returning `HealthResult(ok=False)` with truncated detail — tested
- [x] Health check returns `HealthResult(ok=False)` with status detail on non-200 — tested

## Known Gaps

- [ ] **No OAuth flow**: PAT-only auth; no OAuth 2.0 authorization code flow for user-context operations
- [ ] **PR creation unimplemented**: `CREATE_PR` capability declared, but `write("pr")` resource handler missing
- [ ] **PR commenting not wired**: BDD scenario exists but no `write("pr_comment")` in connector code
- [ ] **Scope verification incomplete**: health check verifies `repo` and `read:org` classic PAT scopes; fine-grained PAT `pull_requests:write` not checked
- [ ] **PRD vs code scope mismatch**: PRD §7.11 specifies fine-grained PAT scopes (`contents:read`, `contents:write`, `pull_requests:write`) but code uses classic PAT scopes (`repo`, `read:org`) — different scope systems
- [ ] **No pagination**: `query("pulls")` and `query("repos")` don't return `next_cursor`
- [ ] **No GHES support**: API base URL is hard-coded to `https://api.github.com`
- [ ] **No rate-limit handling**: no 429 retry, no `X-RateLimit-Remaining` header inspection
- [ ] **No retry on transient HTTP errors**: errors are now wrapped as ValueError but no retry/backoff logic exists for 5xx or 429
- [ ] **Token expiry not distinguished from other errors**: errors are now wrapped as ValueError with status code/structure, but expired PAT, insufficient scopes, and network errors still lack distinct structured error types
- [ ] **Fine-grained PAT not supported**: code requires classic PAT `repo` scope; fine-grained PAT with `contents:read`, `contents:write`, `pull_requests:write` would fail `REQUIRED_SCOPES` check
- [ ] **`read:org` scope requirement unclear**: product map doesn't explain why `read:org` is required — may be unnecessary for most agent workflows

