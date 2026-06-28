---
id: feat-connectors-github
prd: 8.6
delivery-tasks: []
  - backend/tests/features/connectors/github.feature
  - backend/tests/bdd/features/connectors/github_connector.feature
unit-tests: []
code:
  - backend/src/modulo/connectors/github/__init__.py
  - backend/src/modulo/connectors/base.py

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
- [ ] Support token rotation via ConnectorHub without disrupting in-flight runs
- [ ] Rate-limit awareness — no 429 retry/backoff on `read()`/`write()` calls

### OAuth Scopes — capability verification

- [x] Declare required scopes: `repo:read`, `repo:write`, `pull_requests:write` (code constant)
- [x] Verify `repo:read` scope by probing `GET /user/repos` during health check
- [ ] Verify `repo:write` scope — not checked, only `repo:read` is probed
- [ ] Verify `pull_requests:write` scope — not checked during health check
- [ ] **Scope mismatch with PRD**: PRD §8.6 lists scopes as `contents:read`, `contents:write`, `pull_requests:write` — code uses `repo:read`, `repo:write`, `pull_requests:write`. These are different GitHub OAuth scope sets (`repo` is broader than `contents`). Needs resolution.
- [ ] Report missing scopes individually in health check detail (e.g. `missing_scope:repo:write`)
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

### Issue Operations — not yet implemented

- [ ] Create issue — BDD scenario exists in `github.feature` but no `query("issues")` or `write("issue")` implementation
- [ ] List issues — not implemented
- [ ] Search issues — not implemented
- [ ] Close issue — not implemented
- [ ] Add issue comment — not implemented
- [ ] Add issue labels — not implemented
- [ ] Assign issue — not implemented
- [ ] `Capability.ISSUE_READ` / `ISSUE_WRITE` / `ISSUE_SEARCH` are defined in `base.py` but not assigned to `ConnectorType.GITHUB` capabilities

### Health Check — connectivity and credential validation

- [x] Validate token by calling `GET /user` — fail if status != 200
- [x] Probe `repo:read` scope via `GET /user/repos` — fail on 401/403
- [x] Return authenticated user login in `detail` on success
- [x] Return HTTP status and truncated body on failure
- [ ] Full scope check — only `repo:read` is verified; `pull_requests:write` and implied `repo:write` are not probed
- [ ] Detect expired tokens vs insufficient scopes vs network errors — all return as "HTTP {code}: {body}"
- [ ] Per-operation scope verification — no granular check before `write()` calls

### ConnectorType Capability Declaration

- [x] `ConnectorType.GITHUB.capabilities` returns `{READ, WRITE, GIT_PUSH, CREATE_PR}` in `base.py`
- [x] `GitHubConnector.connector_type` returns `ConnectorType.GITHUB`
- [ ] `CREATE_PR` capability declared but `write("pr")` not implemented — capability mismatch
- [ ] `ISSUE_READ`/`ISSUE_WRITE`/`ISSUE_SEARCH` not assigned — no issue operations possible
- [ ] Capability-based graph validation — agent requirements vs connector capabilities not yet wired in ConnectorHub

## Known Gaps

- [ ] **No OAuth flow**: PAT-only auth; no OAuth 2.0 authorization code flow for user-context operations
- [ ] **PR creation unimplemented**: `CREATE_PR` capability declared, but `write("pr")` resource handler missing
- [ ] **Issue operations entirely absent**: no issue CRUD despite BDD scenario and base capability enums existing
- [ ] **PR commenting not wired**: BDD scenario exists but no `write("pr_comment")` in connector code
- [ ] **Scope verification incomplete**: health check only probes `repo:read`; `repo:write` and `pull_requests:write` unchecked
- [ ] **PRD vs code scope mismatch**: PRD §8.6 says `contents:read/write`, code uses `repo:read/write` — these are different GitHub OAuth scope sets
- [ ] **No pagination**: `query("pulls")` and `query("repos")` don't return `next_cursor`
- [ ] **BDD placeholder**: `backend/tests/bdd/features/connectors/github_connector.feature` is a 3-line placeholder with no real scenarios
- [ ] **No unit tests**: `unit-tests` field is empty
- [ ] **No GHES support**: API base URL is hard-coded to `https://api.github.com`
- [ ] **No rate-limit handling**: no 429 retry, no `X-RateLimit-Remaining` header inspection
