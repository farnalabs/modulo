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
- [x] ~~**No rate-limit budget awareness**~~ — **RESOLVED (2026-08-06)**: `_call_api` prefers `X-RateLimit-Reset` (epoch) on HTTP 429 over blind backoff via new `_retry_delay()`; exhausted 429 errors surface quota headers; every query result carries `metadata["rate_limit"]` with the `X-RateLimit-*` headers; new `query("rate_limit")` resource exposes the full per-resource budget via `GET /rate_limit`
- [ ] **Token expiry not distinguished from other errors**: expired PAT, insufficient scopes, and network errors all raise `ValueError` — no distinct structured error types
- [ ] **Fine-grained PAT not supported**: code requires classic PAT `repo` scope; fine-grained PAT with `contents:read`, `contents:write`, `pull_requests:write` would fail `REQUIRED_SCOPES` check
- [ ] **`read:org` scope requirement unclear**: product map doesn't explain why `read:org` is required — may be unnecessary for most agent workflows
- [ ] **File content encoding** — ~~caller must base64-encode file content; no encode/decode helper in connector~~ — **RESOLVED (2026-08-06)**: `query("file")` decodes base64 content to UTF-8 text; `write("file")` accepts raw text `content` (encoded internally) or `content_base64` (passed through for binary files), requiring exactly one
- [x] ~~**Recursive file listing**~~ — **RESOLVED (2026-08-06)**: `query("tree")` lists the full repo tree recursively via the Git Trees API (ref resolved to a commit SHA first), with optional `path`/`recursive` filters
- [x] ~~**Path traversal protection**~~ — **RESOLVED (2026-08-06)**: local `_validate_path()` blocks absolute paths and `..` segments on `query("file")`, `query("tree")`, and `write("file")` before any request is sent
- [ ] **No machine-parseable error codes**: all errors raise `ValueError` with human-readable messages; no structured error type hierarchy
- [ ] **No circuit breaker**: retries are unconditional up to max attempts; no circuit breaker pattern for sustained failures
- [ ] **BDD coverage**: 8 scenarios exist covering basic CRUD + error paths; no BDD for PR operations, retry/backoff, pagination, or configurable base URL
- [ ] **`test_github_issues.py` (550 lines)** and **`test_github_scopes.py` (89 lines)** now in `unit-tests:` frontmatter — were previously missing
- [ ] **`github.feature` (5 scenarios)** and **`github_issues.feature` (15 scenarios)** now in `bdd:` frontmatter — were previously missing
- [x] ~~**Batch file operations**~~ — **RESOLVED (2026-08-06)**: `write("commit")`/`write("files")` applies create/update/delete/move actions in one commit via the Git Database API

## QA History

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
