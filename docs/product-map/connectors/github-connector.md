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
- [x] Accept configurable API base URL for GHES via `GitHubConnector(token, base_url=...)`

### OAuth Scopes — capability verification

- [x] Declare required scopes via `REQUIRED_SCOPES` constant: `{"repo", "read:org"}` (classic PAT scopes from `X-OAuth-Scopes` header)
- [x] Verify scopes by reading `X-OAuth-Scopes` response header from `GET /user` during health check
- [x] Report missing scopes in health check detail with scope names (e.g. `Missing scopes: read:org`)
- [ ] Verify `pull_requests:write` scope — not in `REQUIRED_SCOPES`; classic PAT `repo` scope encompasses PR access, but fine-grained PAT would need explicit `pull_requests:write`
- [ ] **Scope mismatch with PRD**: PRD §7.11 specifies fine-grained PAT scopes (`contents:read`, `contents:write`, `pull_requests:write`) but code uses classic PAT scopes (`repo`, `read:org`). These are different scope systems — fine-grained PATs are more restrictive and granular. The `repo` classic scope is broadly equivalent to `contents:read` + `contents:write` + `pull_requests:write` combined.
- [ ] Report missing scopes as individually named errors (e.g. `missing_scope:repo`) — health check mentions scope names in prose but no machine-parseable error codes
- [ ] Block run start when scopes are insufficient (pre-run health check in ConnectorHub)

### PR Operations — listing, creating, commenting, and file inspection

- [x] List pull requests via `query("pulls")` with `repo` and `state` filters
- [x] Filter PRs by state (default `"open"`), sort, and direction
- [x] Limit results via `q.limit`
- [x] Raise `ValueError` for unsupported resources in `query()`
- [x] Post review comment on a PR via `write("pr_comment")` with `repo`, `pull_number`, `body`
- [x] **Create PR** — `write("pr")` with `repo`, `title`, `head`, `base`, optional `body`, `draft`, `maintainer_can_modify`
- [x] **Update PR** — `write("pr_update")` with `repo`, `pull_number`, optional `title`, `body`, `state`, `base`
- [x] List commits on a PR via `query("pr_commits")` with `repo` and `pull_number`
- [x] List changed files on a PR via `query("pr_files")` with `repo` and `pull_number`
- [x] `query("pulls")` supports pagination via Link header parsing — `next_cursor` is populated
- [ ] Merge PR — no implementation
- [ ] Get PR diff — not implemented
- [ ] Request PR review — not implemented
- [ ] Add PR labels — not implemented

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

## Resilience & Integration Robustness

### Retry/Backoff

- [x] Retry on HTTP 429 (rate limit) with exponential backoff — max 3 retries
- [x] Retry on HTTP 502, 503, 504 (server errors) with exponential backoff — max 3 retries
- [x] Retry on connection errors (`ConnectError`) with exponential backoff — max 3 retries
- [x] Retry on timeout (`TimeoutException`) with exponential backoff — max 3 retries
- [x] Respect `Retry-After` header when present for rate-limit retry delay
- [x] Give up after max retries and raise the underlying error
- [x] Base delay starts at 1s and doubles per attempt (1s, 2s, 4s), capped at 30s
- [x] Retry is applied to all `_call_api` invocations — shared by both `query()` and `write()`
- [ ] No circuit breaker — retries are unconditional up to max attempts
- [ ] No `X-RateLimit-Remaining` header inspection before making requests
- [ ] No rate-limit budget-aware scheduling

### Pagination

- [x] List endpoints (`repos`, `pulls`, `issues`, `issue_comments`, `issue_events`, `labels`, `milestones`, `assignees`, `timeline`) parse Link header for pagination
- [x] `next_cursor` contains the full `next` URL from the Link header
- [ ] Cursor is opaque (full URL) — no page-number extraction helper
- [ ] No automatic pagination across pages — caller must follow `next_cursor`

## Edge Cases

### Missing/Invalid Inputs

- [x] Missing `repo` filter raises `ValueError` with descriptive message — all resources
- [x] Missing `path` filter raises `ValueError` for file operations — tested
- [x] Missing `issue_number` raises `ValueError` for issue-specific operations — tested
- [x] Missing `pull_number` raises `ValueError` for PR-specific operations — tested
- [x] Missing `title` raises `ValueError` for issue/PR creation — tested
- [x] Missing `head` or `base` raises `ValueError` for PR creation — tested
- [x] Missing `body` raises `ValueError` for issue/PR comments — tested
- [x] Missing `content` raises `ValueError` for file writes — tested
- [x] Missing `name` raises `ValueError` for label creation — tested
- [x] Unsupported resource string raises `ValueError` — tested for both query and write

### HTTP Error Mapping

- [x] HTTP 304 (Not Modified) raises `ValueError` with explanatory message
- [x] HTTP 4xx errors raise `ValueError` with status code and truncated response body (200 chars)
- [x] HTTP 5xx errors raise `ValueError` with status code — retried before raising
- [x] Connection-level failures (`ConnectError`) raise `ValueError` — retried before raising
- [x] Timeout failures raise `ValueError` — retried before raising
- [x] JSON decode failures raise `ValueError` with "invalid JSON" message and truncated body (200 chars)

## Known Gaps

- [ ] **No OAuth flow**: PAT-only auth; no OAuth 2.0 authorization code flow for user-context operations
- [ ] **Scope verification incomplete**: health check verifies `repo` and `read:org` classic PAT scopes; fine-grained PAT `pull_requests:write` not checked
- [ ] **PRD vs code scope mismatch**: PRD §7.11 specifies fine-grained PAT scopes (`contents:read`, `contents:write`, `pull_requests:write`) but code uses classic PAT scopes (`repo`, `read:org`) — different scope systems
- [ ] **No rate-limit budget awareness**: retry/backoff exists for 429, but `X-RateLimit-Remaining` header is not inspected before making requests; no rate-limit budget-aware scheduling
- [ ] **Token expiry not distinguished from other errors**: expired PAT, insufficient scopes, and network errors all raise `ValueError` — no distinct structured error types
- [ ] **Fine-grained PAT not supported**: code requires classic PAT `repo` scope; fine-grained PAT with `contents:read`, `contents:write`, `pull_requests:write` would fail `REQUIRED_SCOPES` check
- [ ] **`read:org` scope requirement unclear**: product map doesn't explain why `read:org` is required — may be unnecessary for most agent workflows
- [ ] **Merge PR not implemented**: no `write("pr_merge")` resource handler
- [ ] **Get PR diff not implemented**: no `query("pr_diff")` resource
- [ ] **Request PR review not implemented**: no `write("pr_review_request")` resource
- [ ] **Add PR labels not implemented**: no `write("pr_label")` or `query("pr_labels")` resource
- [ ] **Search issues not implemented**: no `query("search_issues")` resource — would use GitHub Search API
- [ ] **Assign issue not implemented**: no `write("issue_assign")` resource
- [ ] **File content encoding**: caller must base64-encode file content; no encode/decode helper in connector
- [ ] **Recursive file listing**: `GET /contents` only returns one level; no tree API integration
- [ ] **Path traversal protection**: relies on GitHub API server-side; no local validation before sending request
- [ ] **No machine-parseable error codes**: all errors raise `ValueError` with human-readable messages; no structured error type hierarchy
- [ ] **No circuit breaker**: retries are unconditional up to max attempts; no circuit breaker pattern for sustained failures
- [ ] **BDD coverage**: 8 scenarios exist covering basic CRUD + error paths; no BDD for PR operations, retry/backoff, pagination, or configurable base URL

