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
- [x] **Base64 content helpers** — `write("file")` accepts `content_encoding: "text"` to base64-encode plain text automatically (`_encode_content()`); `query("file")` adds a `decoded_content` field next to the raw base64 `content` for `encoding: "base64"` responses (`_b64decode()`); invalid base64 raises `ValueError`
- [x] **Recursive tree listing** — `query("tree")` lists repository tree entries via the Git Data API (`GET /repos/{owner}/{repo}/git/trees/{ref}`) with optional `recursive` (default on), `ref` (default `"main"`), and `path` prefix filters; returns `metadata["truncated"]` when the tree exceeds GitHub's limit
- [ ] Batch file operations — not implemented
- [x] **Path traversal protection** — `_validate_path()` rejects absolute paths and `..` segments on `query("file")`, `query("tree")` `path`, and `write("file")` before any request is sent

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
- [ ] Full scope check — only `repo` and `read:org` are verified; `pull_requests:write` not probed
- [ ] Detect expired tokens vs insufficient scopes vs network errors — all collapsed to generic "HTTP {code}: {body}"
- [ ] Per-operation scope verification — no granular check before `write()` calls

### ConnectorType Capability Declaration

- [x] `ConnectorType.GITHUB.capabilities` returns `{READ, WRITE, GIT_PUSH, CREATE_PR, ISSUE_READ, ISSUE_WRITE}` in `base.py`
- [x] `GitHubConnector.connector_type` returns `ConnectorType.GITHUB`
- [x] `CREATE_PR` capability is implemented via `write("pr")` — creates PRs with title, head, base, optional body/draft/maintainer_can_modify
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

### Resolved (2026-08-05)

- [x] ~~**File content encoding**: caller must base64-encode file content; no encode/decode helper in connector~~ — `write("file")` accepts `content_encoding: "text"` to auto-encode plain text (`_encode_content()`), `query("file")` exposes `decoded_content` (`_b64decode()`), invalid base64 → `ValueError`. Unit tests in `test_github.py` (encode/passthrough/invalid-encoding, decode/skip/non-base64) + 2 BDD scenarios.
- [x] ~~**Recursive file listing**: `GET /contents` only returns one level; no tree API integration~~ — `query("tree")` uses `GET /repos/{owner}/{repo}/git/trees/{ref}` with optional `recursive`/`ref`/`path` filters and `metadata["truncated"]`. Unit tests in `test_github.py` (entries/recursive-default/disabled/ref/path-filter/truncated/traversal/missing-repo) + 2 BDD scenarios.
- [x] ~~**Path traversal protection**: relies on GitHub API server-side; no local validation before sending request~~ — `_validate_path()` rejects absolute paths and `..` segments on `query("file")`, `query("tree")`, and `write("file")` before any HTTP request is sent. Unit tests (6 parametrized query rejections + write rejection with zero HTTP calls) + 2 BDD scenarios.

### Remaining Gaps

- [ ] **No OAuth flow**: PAT-only auth; no OAuth 2.0 authorization code flow for user-context operations
- [ ] **Scope verification incomplete**: health check verifies `repo` and `read:org` classic PAT scopes; fine-grained PAT `pull_requests:write` not checked
- [ ] **PRD vs code scope mismatch**: PRD §7.11 specifies fine-grained PAT scopes (`contents:read`, `contents:write`, `pull_requests:write`) but code uses classic PAT scopes (`repo`, `read:org`) — different scope systems
- [ ] **No rate-limit budget awareness**: retry/backoff exists for 429, but `X-RateLimit-Remaining` header is not inspected before making requests; no rate-limit budget-aware scheduling
- [ ] **Token expiry not distinguished from other errors**: expired PAT, insufficient scopes, and network errors all raise `ValueError` — no distinct structured error types
- [ ] **Fine-grained PAT not supported**: code requires classic PAT `repo` scope; fine-grained PAT with `contents:read`, `contents:write`, `pull_requests:write` would fail `REQUIRED_SCOPES` check
- [ ] **`read:org` scope requirement unclear**: product map doesn't explain why `read:org` is required — may be unnecessary for most agent workflows
- [ ] **No machine-parseable error codes**: all errors raise `ValueError` with human-readable messages; no structured error type hierarchy
- [ ] **No circuit breaker**: retries are unconditional up to max attempts; no circuit breaker pattern for sustained failures
- [ ] **BDD coverage**: 16 scenarios cover basic CRUD + PR ops + error paths + tree/path/encoding; no BDD for retry/backoff, pagination, or configurable base URL
- [ ] **`test_github_issues.py` (550 lines)** and **`test_github_scopes.py` (89 lines)** now in `unit-tests:` frontmatter — were previously missing
- [ ] **`github.feature` (5 scenarios)** and **`github_issues.feature` (15 scenarios)** now in `bdd:` frontmatter — were previously missing
- [ ] **Batch file operations**: no git-trees/blobs batch commit API integration

## QA History

### 2026-08-05 — improve-architecture: 3 known gaps RESOLVED (path traversal, tree listing, base64 helpers)

**RESOLVED known gaps** "File content encoding", "Recursive file listing", and "Path traversal protection" in `connectors/github/__init__.py`:
- (1) Path traversal protection — new `_validate_path()` helper rejects absolute paths and `..` segments on `query("file")`, `query("tree")` `path`, and `write("file")` before any HTTP request is sent (mirrors the GitLab connector guard).
- (2) Recursive file listing — new `query("tree")` (`GET /repos/{owner}/{repo}/git/trees/{ref}`) with optional `recursive` (default on), `ref` (default `"main"`), and `path` prefix filters; returns `metadata["truncated"]` when the tree exceeds GitHub's response limit.
- (3) File content encoding — new `_b64encode()`/`_b64decode()` helpers + `_encode_content()`; `write("file")` accepts `content_encoding: "text"` to auto-encode plain text (default `"base64"` passthrough stays backward compatible), `query("file")` adds a `decoded_content` field for `encoding: "base64"` responses, invalid base64 → `ValueError`.

Added 25 unit tests (`test_github.py`: path-traversal matrix ×6 + write-rejection-no-request + nested-relative-ok, decoded-content add/skip/invalid-base64, write text-encode/default-passthrough/explicit-base64/invalid-content_encoding, tree entries/recursive-default/recursive-disabled/ref/slash-ref-encoding/path-filter/truncated/traversal ×2/missing-repo). Added 6 BDD scenarios in `github_connector.feature` (recursive tree, tree path filter, path traversal on read, path traversal on write, text-content base64-encoded payload, decoded content on read) with 8 new step definitions in `test_connectors.py` (2 query tree steps, text-content write step, 4 assertion steps, `the result is an error with "{message}"`) and the mock connector extended to exercise the real `_validate_path`/`_b64decode`/`_encode_content` helpers. Updated product map (3 behaviours `[ ]`→`[x]` in File Operations, Known Gaps 16→13, BDD count 16→22, QA History). 86/86 `test_github.py` + 139/139 all-github unit tests + 22/22 github BDD scenarios pass (8 pre-existing connector-suite BDD failures unchanged), ruff clean, mypy strict clean. Status: partial (OAuth flow, fine-grained PAT, scope verification, rate-limit budget, machine-parseable error codes, circuit breaker, batch file ops remain).

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
