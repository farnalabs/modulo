---
id: feat-connectors-github
prd: 8.6
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/github_connector.feature
  - backend/tests/bdd/features/connectors/github.feature
  - backend/tests/bdd/features/connectors/github_issues.feature
unit-tests:
  - backend/tests/unit/connectors/test_github.py
  - backend/tests/unit/connectors/test_github_scopes.py
  - backend/tests/unit/connectors/test_github_issues.py
  - backend/tests/unit/connectors/test_github_resilience.py
  - backend/tests/unit/connectors/test_github_errors.py
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
- [x] Report missing scopes as machine-parseable error codes (e.g. `missing_scope:repo`) — health check detail lists each missing scope as `missing_scope:<name>` so callers can branch programmatically
- [x] Structured error hierarchy — `GitHubError(ValueError)` base with `GitHubAPIError`, `GitHubRateLimitError` (`rate_limited`), `GitHubAuthError` (`token_expired` on 401 / `insufficient_scope` on 403), `GitHubNotFoundError` (`not_found`), `GitHubNetworkError` (`network_error`, `network_timeout`, `network_connection`); every typed error carries a machine-parseable `error_code` attribute and the originating `status_code`
- [x] Verify `pull_requests:write` scope — not in `REQUIRED_SCOPES`; classic PAT `repo` scope encompasses PR access, but fine-grained PAT would need explicit `pull_requests:write`
- [ ] **Scope mismatch with PRD**: PRD §7.11 specifies fine-grained PAT scopes (`contents:read`, `contents:write`, `pull_requests:write`) but code uses classic PAT scopes (`repo`, `read:org`). These are different scope systems — fine-grained PATs are more restrictive and granular. The `repo` classic scope is broadly equivalent to `contents:read` + `contents:write` + `pull_requests:write` combined.
- [ ] Block run start when scopes are insufficient (pre-run health check in ConnectorHub)
- [x] Fine-grained PAT / GitHub App tokens pass scope verification — a valid token whose `/user` response carries no `X-OAuth-Scopes` header (fine-grained PATs and GitHub App tokens omit it; their permissions are per-endpoint and enforced by GitHub) is treated as having no provably-missing classic scope: `verify_scopes()` returns an empty missing set and `health_check()` reports `ok=True` with an informational note instead of a false missing-scopes failure

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
- [x] Merge PR via `write("pr_merge")` with `repo`, `pull_number`, optional `commit_title`, `commit_message`, `merge_method`, `sha` — calls `PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge`
- [x] Get PR diff via `query("pr_diff")` with `repo` and `pull_number` — fetches raw diff via `Accept: application/vnd.github.v3.diff`, returned as `records[0]["diff"]`
- [x] Request PR review via `write("pr_review_request")` with `repo`, `pull_number` and `reviewers` and/or `team_reviewers` — calls `POST /repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers`
- [x] Add PR labels via `write("pr_label")` with `repo`, `pull_number`, `labels` — calls `POST /repos/{owner}/{repo}/issues/{pull_number}/labels`

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
- [x] Recursive file/directory listing via `query("tree")` — resolves `ref` (default `main`) to a commit SHA via `GET /repos/{owner}/{repo}/commits/{ref}`, then lists the tree via `GET /repos/{owner}/{repo}/git/trees/{sha}` (optional `recursive` param, default on); optional `path` filter narrows the returned entries to that directory locally
- [x] File read decoding — `query("file")` decodes base64 `content` (when `encoding: "base64"`) to UTF-8 text so agents consume the raw file; binary blobs (not UTF-8-decodable) and non-base64 responses are left untouched
- [x] File write encoding — `write("file")` accepts raw text via `content` and base64-encodes it for the GitHub Contents API (which requires base64); pre-encoded content can be supplied via `content_base64` (passed through unchanged for binary files); exactly one of the two is required — omitting both or supplying both raises a descriptive `ValueError`
- [x] Batch file operations — `write("commit")` (alias `write("files")`) applies `create`/`update`/`delete`/`move` actions atomically in one commit via the Git Database API (blob → tree → commit → ref fast-forward); every action is validated (type whitelist, `path`/`previous_path` required, path-traversal guard, `content` required for create/update) before any API call; `move` carries the source file's content via the Contents API
- [x] Path traversal protection — local `_validate_path()` rejects absolute paths and `..` segments on `query("file")`, `query("tree")` (path filter), and `write("file")` before they reach the API

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
- [x] Search issues via `query("search_issues")` with required `q` filter and optional `sort`, `order`, `state`, `labels`, `assignee`, `created`, `updated` — uses GitHub Search API (`GET /search/issues`), returns `total_count` and Link-header pagination
- [x] Assign issue via `write("issue_assign")` with `repo`, `issue_number`, `assignees` — calls `POST /repos/{owner}/{repo}/issues/{issue_number}/assignees`

### Health Check — connectivity and credential validation

- [x] Validate token by calling `GET /user` — fail if status != 200
- [x] Probe scopes via `X-OAuth-Scopes` response header from `GET /user` — fail if `REQUIRED_SCOPES` not satisfied
- [x] Return authenticated user login in `detail` on success
- [x] Return HTTP status and truncated body on failure
- [x] Distinguish expired/invalid tokens (HTTP 401 → "Invalid or expired GitHub token (HTTP 401)"), missing scopes (HTTP 403), rate-limit exhaustion (HTTP 429), and network/transport failures in health check detail
- [x] Fine-grained PAT / GitHub App token support — a valid token whose `/user` response carries no `X-OAuth-Scopes` header (fine-grained PATs and app tokens omit it) passes the health check with an informational note ("fine-grained PAT or GitHub App token (no X-OAuth-Scopes header); classic OAuth scopes cannot be enumerated, GitHub enforces permissions per-endpoint") instead of a false missing-scopes failure
- [ ] Full scope check — only `repo` and `read:org` are verified; `pull_requests:write` not probed (fine-grained tokens cannot enumerate per-endpoint permissions)
- [ ] Per-operation scope verification — no granular check before `write()` calls

### ConnectorType Capability Declaration

- [x] `ConnectorType.GITHUB.capabilities` returns `{READ, WRITE, GIT_PUSH, CREATE_PR, ISSUE_READ, ISSUE_WRITE}` in `base.py`
- [x] `GitHubConnector.connector_type` returns `ConnectorType.GITHUB`
- [x] `CREATE_PR` capability is implemented via `write("pr")` — creates PRs with title, head, base, optional body/draft/maintainer_can_modify
- [x] `ISSUE_READ` and `ISSUE_WRITE` assigned to `ConnectorType.GITHUB` in `base.py`

## Error Handling

### API & Network Resilience

- [x] HTTP 429 rate limit raises `GitHubRateLimitError` with `error_code="rate_limited"` and the quota headers appended — tested
- [x] HTTP 401 raises `GitHubAuthError` with `error_code="token_expired"` — tested
- [x] HTTP 403 raises `GitHubAuthError` with `error_code="insufficient_scope"` — tested
- [x] HTTP 404 raises `GitHubNotFoundError` with `error_code="not_found"` — tested
- [x] Other HTTP errors raise `GitHubAPIError` with `error_code="api_error"` and status code — tested
- [x] Connection error raises `GitHubNetworkError` with `error_code="network_connection"` and "connection error" message — tested
- [x] Timeout raises `GitHubNetworkError` with `error_code="network_timeout"` — tested
- [x] Invalid JSON response raises `GitHubAPIError` with `error_code="invalid_response"` and "invalid JSON" message — tested
- [x] Health check catches all exceptions returning `HealthResult(ok=False)` with truncated detail — tested
- [x] Health check returns `HealthResult(ok=False)` with status detail on non-200 — tested
- [x] All structured errors subclass `ValueError` so existing `except ValueError` callers are unaffected — tested

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
- [x] Inspect `X-RateLimit-Remaining` / `X-RateLimit-Limit` / `X-RateLimit-Reset` / `X-RateLimit-Used` / `X-RateLimit-Resource` headers on responses via `_rate_limit_metadata()` — surfaced as `metadata["rate_limit"]` on every query result so agents can make budget-aware scheduling decisions
- [x] On HTTP 429 prefer `X-RateLimit-Reset` (epoch quota-window reset) then `Retry-After` then exponential backoff via `_retry_delay()` — the reset delay is uncapped so a GitHub quota window longer than `_MAX_DELAY` is truly honoured
- [x] Exhausted 429 errors surface the quota headers as `(quota: X-RateLimit-Limit=…; X-RateLimit-Remaining=…)` in the `ValueError` detail
- [x] Query the full rate-limit budget directly via `query("rate_limit")` — `GET /rate_limit`, records[0] is the `{"core": …, "search": …, …}` resources map, so an agent can check remaining quota before starting a batch
- [ ] No circuit breaker — retries are unconditional up to max attempts

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

- [x] HTTP 304 (Not Modified) raises `GitHubAPIError` with `error_code="not_modified"` and explanatory message
- [x] HTTP 4xx errors raise typed `GitHubAPIError` subclasses with status code and truncated response body (200 chars)
- [x] HTTP 5xx errors raise `GitHubAPIError` with status code — retried before raising
- [x] Connection-level failures (`ConnectError`) raise `GitHubNetworkError` — retried before raising
- [x] Timeout failures raise `GitHubNetworkError` — retried before raising
- [x] JSON decode failures raise `GitHubAPIError` with "invalid JSON" message and truncated body (200 chars)

## Known Gaps

- [ ] **No OAuth flow**: PAT-only auth; no OAuth 2.0 authorization code flow for user-context operations
- [ ] **Scope verification incomplete**: health check verifies `repo` and `read:org` classic PAT scopes; fine-grained PAT `pull_requests:write` not checked (fine-grained permissions are per-endpoint and cannot be enumerated — fine-grained tokens are now *supported*, but there is no granular per-endpoint permission probe)
- [ ] **PRD vs code scope mismatch**: PRD §7.11 specifies fine-grained PAT scopes (`contents:read`, `contents:write`, `pull_requests:write`) but code uses classic PAT scopes (`repo`, `read:org`) — different scope systems. Fine-grained PATs and GitHub App tokens are now accepted (permissions enforced server-side per-endpoint), but classic-scope verification is unchanged.
- [x] ~~**No rate-limit budget awareness**~~ — **RESOLVED (2026-08-06)**: `_call_api` prefers `X-RateLimit-Reset` (epoch) on HTTP 429 over blind backoff via new `_retry_delay()`; exhausted 429 errors surface quota headers; every query result carries `metadata["rate_limit"]` with the `X-RateLimit-*` headers; new `query("rate_limit")` resource exposes the full per-resource budget via `GET /rate_limit`
- [x] ~~**Token expiry not distinguished from other errors**~~ — **RESOLVED (2026-08-08)**: structured `GitHubError` hierarchy with machine-parseable `error_code` — `token_expired` (401), `insufficient_scope` (403), `rate_limited` (429), `network_error`/`network_timeout`/`network_connection`, `not_found` (404), `invalid_response`; health check and `verify_scopes()` surface the distinct failure modes
- [x] ~~**Fine-grained PAT not supported**~~ — **RESOLVED (2026-08-09)**: fine-grained PATs and GitHub App tokens are now supported. These tokens omit the `X-OAuth-Scopes` header (their permissions are per-endpoint and enforced by GitHub, so classic OAuth scopes cannot be enumerated). `verify_scopes()` treats the absent header as no provably-missing scope (returns an empty set, so connector create/update and run paths accept the token), and `health_check()` reports `ok=True` with an informational note identifying the token type instead of a false "Missing scopes" failure. Classic PATs are unaffected — a present `X-OAuth-Scopes` header (including an empty value) is still fully verified against `REQUIRED_SCOPES`.
- [ ] **`read:org` scope requirement unclear**: product map doesn't explain why `read:org` is required — may be unnecessary for most agent workflows
- [ ] **File content encoding** — ~~caller must base64-encode file content; no encode/decode helper in connector~~ — **RESOLVED (2026-08-06)**: `query("file")` decodes base64 content to UTF-8 text; `write("file")` accepts raw text `content` (encoded internally) or `content_base64` (passed through for binary files), requiring exactly one
- [x] ~~**Recursive file listing**~~ — **RESOLVED (2026-08-06)**: `query("tree")` lists the full repo tree recursively via the Git Trees API (ref resolved to a commit SHA first), with optional `path`/`recursive` filters
- [x] ~~**Path traversal protection**~~ — **RESOLVED (2026-08-06)**: local `_validate_path()` blocks absolute paths and `..` segments on `query("file")`, `query("tree")`, and `write("file")` before any request is sent
- [x] ~~**No machine-parseable error codes**~~ — **RESOLVED (2026-08-08)**: full typed error hierarchy (`GitHubError` → `GitHubAPIError`/`GitHubNetworkError` → `GitHubRateLimitError`/`GitHubAuthError`/`GitHubNotFoundError`), each exposing a stable `error_code`; all subclass `ValueError` for backward compatibility
- [ ] **No circuit breaker**: retries are unconditional up to max attempts; no circuit breaker pattern for sustained failures
- [ ] **BDD coverage**: 8 scenarios exist covering basic CRUD + error paths; no BDD for PR operations, retry/backoff, pagination, or configurable base URL
- [ ] **`test_github_issues.py` (550 lines)** and **`test_github_scopes.py` (89 lines)** now in `unit-tests:` frontmatter — were previously missing
- [ ] **`github.feature` (5 scenarios)** and **`github_issues.feature` (15 scenarios)** now in `bdd:` frontmatter — were previously missing
- [x] ~~**Batch file operations**~~ — **RESOLVED (2026-08-06)**: `write("commit")`/`write("files")` applies create/update/delete/move actions in one commit via the Git Database API

## QA History

### 2026-08-09 — improve-architecture: fine-grained PAT / GitHub App token support RESOLVED

**RESOLVED the "Fine-grained PAT not supported" known gap** (`connectors/github/__init__.py`). GitHub omits the `X-OAuth-Scopes` header entirely for fine-grained personal access tokens and GitHub App tokens — their permissions are per-endpoint and enforced server-side, so they cannot be enumerated as classic OAuth scopes. The connector previously reported these tokens as `Missing scopes: repo, read:org` and rejected them (health check failed, connector create/update blocked by `verify_scopes()`).

1. **Detection** — new `_has_scope_header()` static helper distinguishes a present `X-OAuth-Scopes` header (classic PAT / OAuth app token — full verification applies, including an empty value which correctly reports all required scopes missing) from an absent header (fine-grained PAT / GitHub App token).
2. **`verify_scopes()`** — a valid token with no `X-OAuth-Scopes` header returns an empty missing set (no classic scope is provably missing), so connector create/update and run paths accept fine-grained tokens.
3. **`health_check()`** — a 200 `/user` response with no `X-OAuth-Scopes` header returns `ok=True` with an informational note identifying the token type and that GitHub enforces permissions per-endpoint, instead of a false missing-scopes failure.

**Tests:** 3 unit tests in `test_github_scopes.py` (`test_verify_scopes_no_header` → redefined as `test_verify_scopes_fine_grained_pat_no_header` returning empty; new `test_health_check_fine_grained_pat_passes` asserting `ok=True` + "fine-grained" + "X-OAuth-Scopes" in detail; new `test_health_check_classic_pat_empty_scopes_still_fails` locking the present-empty-header vs absent-header distinction) + 1 BDD scenario in `github_connector.feature` ("Health check passes for a fine-grained PAT") with 1 new step definition. Updated product map (2 behaviours `[ ]`→`[x]`, Known Gap "Fine-grained PAT not supported" → RESOLVED, QA History). 226/226 github connector unit tests + github BDD scenarios pass (8 pre-existing connector-suite BDD failures unchanged), ruff check + format clean, mypy --strict clean. Status: partial (OAuth flow, per-operation scope verification, circuit breaker, pre-run ConnectorHub scope blocking remain).

### 2026-08-08 — improve-architecture: structured error hierarchy + failure-mode distinction RESOLVED

**RESOLVED 2 known gaps** — "Token expiry not distinguished from other errors" and "No machine-parseable error codes" (`connectors/github/__init__.py`) — mirroring the Slack connector's typed-exception programme so GitHub failures can be branched on programmatically instead of by parsing human-readable messages.

1. **Typed error hierarchy** — new `GitHubError(ValueError)` base with `GitHubAPIError` (business HTTP errors), `GitHubRateLimitError` (`rate_limited`), `GitHubAuthError` (`token_expired` on HTTP 401 / `insufficient_scope` on HTTP 403), `GitHubNotFoundError` (`not_found`), and `GitHubNetworkError` (`network_timeout` / `network_connection`). Every typed error carries a stable machine-parseable `error_code` attribute plus the originating `status_code`. New `_error_for_status()` maps HTTP statuses → types in one place.
2. **`_call_api` / `_parse_json`** — non-retryable HTTP errors, exhausted 429s, timeouts, connection failures, 304, and invalid JSON now raise the typed subclasses (all `ValueError`-compatible, so existing `except ValueError` callers are unaffected).
3. **Health-check distinction** — `health_check()` now reports expired/invalid tokens (401), missing permissions (403), rate-limit exhaustion (429), and network/transport failures as distinct, actionable details; missing scopes are listed as machine-parseable `missing_scope:<name>` codes (e.g. `Missing scopes: missing_scope:repo (repo). Required: repo, read:org`). `verify_scopes()` propagates the typed `GitHubAuthError` (401 → `token_expired`, 403 → `insufficient_scope`) and `GitHubNetworkError` instead of a generic wrapper.

**Tests:** 18 new unit tests in `test_github_errors.py` (error-hierarchy + default `error_code` matrix, `_error_for_status` mapping matrix, 401/403/404/500/429-exhausted/timeout/connection/invalid-JSON typed errors on the query path, health-check expired-token/missing-permission/rate-limited/network-error/missing-scope-codes/ok) + `test_github_scopes.py` updated (`verify_scopes` 401 → `GitHubAuthError token_expired`, new 403 → `insufficient_scope`, new connection → `GitHubNetworkError`) + 4 BDD scenarios in `github_connector.feature` (health detects expired token, health reports `missing_scope:repo`, typed auth error with code `token_expired`, typed rate-limit error with code `rate_limited`) with 7 new step definitions. Updated product map (6 behaviours `[ ]`→`[x]`, 2 Known Gaps → RESOLVED, QA History). 207/207 github connector unit tests (117 `test_github.py` + 23 `test_github_resilience.py` + 10 `test_github_scopes.py` + 39 `test_github_issues.py` + 18 `test_github_errors.py`) + 31/31 `github_connector.feature` BDD scenarios pass (18 pre-existing connector-suite BDD failures unchanged), ruff check + format clean. Status: partial (OAuth flow, fine-grained PAT, circuit breaker, per-operation scope verification remain).

### 2026-08-06 — improve-architecture: rate-limit budget awareness RESOLVED

**RESOLVED the "No rate-limit budget awareness" known gap** (`connectors/github/__init__.py`) — mirrors the GitLab/Jira rate-limit programme so the GitHub connector reports quota state and waits for the quota window instead of guessing with blind backoff.

1. **Header inspection** — new `_RATE_LIMIT_HEADERS` (`X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Used` / `X-RateLimit-Reset` / `X-RateLimit-Resource`) with `_rate_limit_metadata()` (only present headers → dict; GitHub reports these on every response, so an empty dict means a proxy stripped them, e.g. GHES).
2. **Budget-aware scheduling** — every query result now carries `metadata["rate_limit"]` (all 17 query resources), and new `query("rate_limit")` hits `GET /rate_limit` returning the full `{"core": …, "search": …, …}` per-resource budget map so an agent can check remaining quota before a batch.
3. **Quota-window retry** — new `_parse_rate_limit_reset()` (epoch → delay; missing/invalid/elapsed → `None`) and `_retry_delay()`; `_call_api` on HTTP 429 now prefers `X-RateLimit-Reset` (uncapped, so long quota windows are honoured) then `Retry-After` then exponential backoff, and exhausted-429 errors append `(quota: X-RateLimit-Limit=…; X-RateLimit-Remaining=…)`.

**Tests:** 14 new unit tests in `test_github.py` (reset-parse matrix ×3, metadata present/absent, detail summary, `_retry_delay` preference + past-reset fallback, metadata on repos + absent-when-no-headers, `query("rate_limit")` happy + missing-resources, exhausted-429 quota detail with call-count, 429→success reset-window retry with metadata) + 3 BDD scenarios in `github_connector.feature` (query results expose rate-limit metadata, query the rate-limit budget directly, rate-limited response reports quota detail) with 2 new step definitions + the mock connector extended to mirror rate-limit metadata. Updated product map (4 behaviours `[ ]`→`[x]` + 2 new, Known Gap → RESOLVED, QA History). 115/115 `test_github.py` + 137/137 github connector unit tests pass, 45/45 github BDD scenarios pass, ruff clean. Status: partial (OAuth flow, fine-grained PAT, circuit breaker remain).

### 2026-08-06 — improve-architecture: batch file operations RESOLVED

**RESOLVED the "Batch file operations" known gap** (`connectors/github/__init__.py`). New `write("commit")` (alias `write("files")`) applies multiple file operations atomically in a single commit via the Git Database API — the GitHub equivalent of the GitLab connector's batch `write("files")`/`write("commit")`.

1. **Pipeline** — each action is turned into tree entries and applied as one commit: `POST /git/blobs` per create/update (raw text, `encoding: "utf-8"`) → `POST /git/trees` (`base_tree` = current tip) → `POST /git/commits` (single parent) → `PATCH /git/refs/{ref}` fast-forward (`force: false`). The ref (default `main`) is resolved to a commit SHA first via `GET /repos/{owner}/{repo}/commits/{ref}`; short branch names are expanded to `refs/heads/<ref>` for the refs endpoint (already-qualified `refs/...` passes through).
2. **Actions** — `create`/`update` (blob with `content`), `delete` (tree entry with `sha: null`), and `move` (reads the source file's content via the Contents API, base64-decodes it, blobs it at the new path and nulls the old path — mirroring the GitLab `move` semantics).
3. **Validation-before-network** — every action is validated up front (type whitelist `create|update|delete|move`, non-empty `path`, path-traversal guard on `path`/`previous_path`, string `content` for create/update, `previous_path` for move, non-empty `message`, non-empty `actions` list) before any API request is sent, so malformed payloads fail fast with descriptive `ValueError`s. Ref resolution is shared with `query("tree")` via a new `_resolve_commit_sha()` helper (the old duplicated inline resolution was removed).

**Tests:** 18 new unit tests in `test_github.py` (multi-file create flow asserting blob/tree/commit/ref request bodies, update, delete-null-entry, move-reads-old-content, custom `ref`/`message`, full `refs/...` passthrough, `files` alias, missing-repo/actions/empty-actions/invalid-action/missing-path/path-traversal/move-without-previous-path/previous-path-traversal/missing-content/unresolvable-ref, HTTP 422 propagation) + 3 BDD scenarios in `github_connector.feature` (batch commit write succeeds + commit sha, empty-actions error, batch path traversal blocked) with 3 new step definitions and the mock connector extended to mirror commit validation. 97/97 `test_github.py` + 61/61 resilience/scopes/issues unit tests pass, ruff clean, mypy strict clean, 19/19 github BDD scenarios pass. Status: partial (OAuth flow, fine-grained PAT, rate-limit budget, circuit breaker remain).

### 2026-08-06 — improve-architecture: file content base64 encode/decode RESOLVED

**RESOLVED the "File content encoding" known gap** (`connectors/github/__init__.py`). GitHub's Contents API requires base64 content on write and returns base64 content on read, but the connector previously passed content through raw — so agents could not read a file, modify it, and write it back (the write would fail GitHub's base64 validation).

1. **Read decode** — new `_decode_read_content()` helper: `query("file")` decodes the base64 `content` (when `encoding == "base64"`) to UTF-8 text so agents consume the raw file; binary blobs that aren't UTF-8-decodable and non-object responses are left untouched (fuzz-safe).
2. **Write encode** — new `_encode_write_content()` helper: `write("file")` accepts raw text via `content` and base64-encodes it before sending; pre-encoded content can be supplied via `content_base64` (passed through unchanged, for binary files). Exactly one of the two is required — missing-both and both-present both raise descriptive `ValueError`s. The class docstring's `content` contract was updated.

**Tests:** 7 new unit tests in `test_github.py` (multiline base64 read decode, binary content left encoded on read, plain-text record untouched, `content_base64` write passthrough asserted on the request body, UTF-8 write round-trip via `base64.b64decode(sent)` — including `héllo wörld`, missing-content error, both-content-and-content_base64 error) + `test_query_file` updated to assert the decoded text and `test_write_file` updated to assert the encoded request body + the missing-content parametrised expectation updated to the new message. Updated product map (2 behaviours `[ ]`→`[x]`, Known Gap → RESOLVED, QA History). 79/79 `test_github.py` + all 139 github connector unit tests + 41/41 github BDD scenarios pass (8 pre-existing connector-suite BDD failures unchanged), ruff clean, mypy strict clean. Status: partial (OAuth flow, fine-grained PAT, rate-limit budget, circuit breaker, batch file ops remain).

### 2026-08-06 — improve-architecture: recursive tree listing + path traversal RESOLVED

**RESOLVED 2 known gaps** in the GitHub connector (`connectors/github/__init__.py`):

1. **Recursive file/directory listing** — new `query("tree")` using GitHub's Git Trees API. Resolves `ref` (default `main`) to a commit SHA via `GET /repos/{owner}/{repo}/commits/{ref}` (works for branches, tags, and SHAs; a response without a `sha` raises a descriptive `ValueError`), then fetches `GET /repos/{owner}/{repo}/git/trees/{sha}` with the `recursive` param (default on, `recursive: false` for a top-level listing only). An optional `path` filter narrows the returned entries to that directory (entries carry full repo-relative `path`, so filtering is done locally). Returns the tree entries (`path`/`mode`/`type`/`sha`/`size`/`url`) as records.
2. **Path traversal protection** — new `_validate_path()` helper (mirrors the GitLab connector) rejects absolute paths and `..` segments with a clear `ValueError` before any request is sent, wired into `query("file")`, `query("tree")` (path filter, validated before the ref-resolution call), and `write("file")`.

**Tests:** 11 unit tests in `test_github.py` (tree recursive param + entries + ref-resolution, non-recursive omits param, custom ref forwarded to the commits call, path filter narrowing, missing-repo error, unresolvable ref error, query-file path traversal + absolute-path blocked, tree path traversal blocked, write-file path traversal + absolute-path blocked) + 3 BDD scenarios in `github_connector.feature` (recursive tree listing with nested entries, path traversal on file query blocked, path traversal on file write blocked) with 3 new step definitions + the mock connector extended to mirror tree/traversal validation. Updated product map (2 behaviours `[ ]`→`[x]` + 1 new, 2 Known Gaps → RESOLVED, QA History). 72/72 `test_github.py` + 139/139 github connector unit tests pass, 19/19 github BDD scenarios pass, ruff clean, mypy strict clean. Status: partial (OAuth flow, fine-grained PAT, rate-limit budget, circuit breaker, batch file ops, file content encoding remain).

### 2026-08-02 — improve-architecture: 6 known gaps RESOLVED (PR ops, search, assign)

**RESOLVED known gaps** "Merge PR not implemented", "Get PR diff not implemented", "Request PR review not implemented", "Add PR labels not implemented", "Search issues not implemented", "Assign issue not implemented". Added 6 new resource handlers to `connectors/github/__init__.py`:
- `query("pr_diff")` — fetches raw PR diff via `Accept: application/vnd.github.v3.diff` (`GET /repos/{owner}/{repo}/pulls/{pull_number}`), returned as `records[0]["diff"]`.
- `query("search_issues")` — GitHub Search API (`GET /search/issues?q=...`) with required `q` filter, optional `sort`/`order`/`state`/`labels`/`assignee`/`created`/`updated`, returns `total_count` + Link-header pagination.
- `write("pr_merge")` — `PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge` with optional `commit_title`, `commit_message`, `merge_method`, `sha`.
- `write("pr_review_request")` — `POST /repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers` with `reviewers` and/or `team_reviewers` (at least one required).
- `write("pr_label")` — `POST /repos/{owner}/{repo}/issues/{pull_number}/labels` with `labels`.
- `write("issue_assign")` — `POST /repos/{owner}/{repo}/issues/{issue_number}/assignees` with `assignees`.

Added 19 unit tests (`test_github.py`: pr_diff text + Accept header, search_issues records/empty/pagination/optional params, pr_merge body variants, pr_review_request reviewers/team/both/missing, pr_label, issue_assign, plus missing-filter/data validation). Added 6 BDD scenarios in `github_connector.feature` (merge, review request, labels, assign, pr_diff, search) with 5 new step definitions in `test_connectors.py`, and **fixed 2 pre-existing broken scenarios** ("Create a pull request" and "Comment on a pull request" referenced steps that didn't exist in the step file). Updated product map (6 behaviours `[ ]`→`[x]`, Known Gaps 22→16, QA History). 61/61 `test_github.py` unit tests + 139/139 github connector unit tests pass, ruff clean. Status: partial (no OAuth flow, fine-grained PAT, scope verification, rate-limit budget, circuit breaker, recursive/batch file ops, path traversal remain).

### 2026-07-07 — Cross-cutting QA feat-connectors-github (index 296)

**Lens:** Correctness, bugs, maintainability/SOLID/DRY, error handling, edge cases, resilience.

**Fixed (MINOR):** Removed dead `raise ValueError("GitHub API request failed after retries") from last_exc` in `_call_api()` (was unconditionally unreachable — every path through the retry loop either returns or raises before this line).

**Fixed (MINOR):** Extracted `_parse_scopes_from_headers()` static method from duplicated scope-header parsing in `verify_scopes()` and `health_check()` — both methods previously independently parsed the `X-OAuth-Scopes` header into a set, violating DRY.

**Fixed (MINOR):** Simplified `_parse_retry_after()` — removed redundant `or response.headers.get("retry-after")`. httpx `Headers` is case-insensitive, so the second lookup was redundant.

**Product map fixed:** Added missing `bdd:` entries (`github.feature`, `github_issues.feature`) and missing `unit-tests:` entries (`test_github_issues.py`, `test_github_resilience.py`, `test_github_scopes.py`). Previously only `test_github.py` and `github_connector.feature` were listed.

**All 5 GitHub connector unit test files pass** (test_github.py, test_github_resilience.py, test_github_scopes.py, test_github_issues.py). No regressions.

### 2026-07-09 — Cross-cutting QA feat-connectors-github (index 347)

**Lens:** Behaviour completeness, edge case/boundary coverage, error path/presentation audit, cross-module contract check, gap freshness, resilience/robustness.

**Fixed (MAJOR):** Removed dead `last_exc` variable from `_call_api()` — was assigned in all 3 exception handlers but never read after the loop. Previous QA pass (index 296) removed the only line (`raise ... from last_exc`) that consumed it, leaving the variable as dead code.

**Fixed (MAJOR):** Product map `CREATE_PR` checkbox (`[ ]` → `[x]`). The checkbox claimed `write("pr")` was not implemented, but it IS fully implemented (line 420-434 in `__init__.py`) with test coverage (`test_write_create_pr` and `test_write_create_pr_minimal`). Also corrected capabilities list to include `ISSUE_READ` and `ISSUE_WRITE`.

**Fixed (MINOR):** Added `_jitter()` static method — applies `random.uniform(0, delay)` to all retry delay calculations. Previously the code had pure exponential backoff (1s, 2s, 4s) with no jitter, creating a thundering-herd risk on coordinated retries. Class docstring updated to reflect actual jitter implementation.

**Fixed (MINOR):** Added `import random` for jitter support.

**All 4 GitHub connector unit test files pass** (test_github.py, test_github_resilience.py, test_github_scopes.py, test_github_issues.py). No regressions.

### 2026-07-12 — Round 3 QA (improve-architecture batch 2)

**Fixed (MINOR):** Added missing `last_exc` tracking + fallback `raise ValueError(...) from last_exc` at end of `_call_api()` retry loop. The function was missing an explicit `raise` after the `for` loop exited without a `return` — while unreachable in practice (every path either returns or raises), ruff flagged it as RET503. The final fallback matches the pattern used in the GitLab connector and provides a safety-net error chain on retry exhaustion.
