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
  - backend/tests/unit/connectors/test_gitlab_resilience.py
code:
  - backend/src/modulo/connectors/gitlab/__init__.py
  - backend/src/modulo/connectors/base.py
depends-on:
  - feat-connectors-hub
status: partial
---
# GitLab Connector

Async GitLab REST API v4 connector implementing `ConnectorBase`. Provides read/write access to GitLab projects for agent pipelines. Authenticated via Personal Access Token. Part of the `git-host` connector type family alongside `FilesystemConnector` and `GitHubConnector`.

## Behaviours

### Authentication — PAT-based API access

- [x] Authenticate all requests via `Authorization: Bearer <token>` header
- [x] Set `Accept: application/json` header on all requests
- [x] Use `httpx.AsyncClient` with base URL `https://gitlab.com/api/v4`
- [x] Accept configurable base URL for self-hosted GitLab instances (`base_url` constructor arg, default `https://gitlab.com/api/v4`)
- [x] health_check() calls `GET /user` to validate token
- [x] Return `HealthResult(ok=False)` with HTTP status on non-200
- [x] Return `HealthResult(ok=True)` with authenticated user info on success
- [x] Report `X-Request-Id` on API errors for GitLab support debugging

### OAuth Scopes — capability verification

- [x] Declare required scopes: `read_api`, `write_repository`, `api` (code constant)
- [x] Verify `read_api` scope by probing `GET /projects` during health check
- [x] Distinguish expired token (401) from missing scopes (403) on `/user` and `/projects`
- [x] Verify `write_repository` scope during health check — read the token's declared scopes from the instance `GET /oauth/token/info` endpoint and report it when missing
- [x] Verify `api` scope during health check — same token-introspection probe; a token declaring `api` is treated as satisfying `read_api`/`write_repository` (GitLab superset scope)
- [x] Report missing scopes individually in health check detail
- [x] Per-operation write scope verification — `write()` fails fast with a descriptive `ValueError` when the token lacks the scope the operation requires (repository-file writes need `write_repository`; MR/issue/label/milestone/pipeline writes need `api`); declared scopes are read from the instance `/oauth/token/info` endpoint and cached per instance for `_SCOPE_CACHE_TTL` (5 min) so the probe runs at most once per window
- [x] Health check warms the write-scope cache — a successful `health_check()` caches the declared scopes so the first subsequent `write()` is verified without re-probing
- [x] Scope verification degrades to allow when scopes cannot be determined — an unavailable `GET /oauth/token/info` (older self-hosted returns 404), network error, or unparseable body skips the pre-write check and lets the GitLab API enforce scope on the call
- [ ] Block run start when scopes are insufficient (pre-run health check in ConnectorHub)

### Project Operations — listing and discovery

- [x] List projects via `query("projects")` with optional search filter
- [x] Return project ID, name, and web URL in results
- [x] Raise `ValueError` for unsupported resources in `query()`
- [x] Support pagination via GitLab `X-Next-Page` header — list queries return `next_cursor`
- [x] Accept `ConnectorQuery.cursor` as the GitLab `page` param to fetch the next page
- [x] Filter projects by membership, visibility, or ownership
- [x] Support `limit` parameter in project queries

### File Operations — read and write via Repository Files API

- [x] Read file via `query("file")` with `project_id`, `path`, `ref` filters
- [x] Decode base64 content from GitLab API response
- [x] Write file via `write("file")` with `project_id`, `path`, `content`, `commit_message`, optional `branch`
- [x] Call `PUT /projects/{id}/repository/files/{path}` with JSON body
- [x] Default branch to `"main"` when not specified
- [x] Return created/updated file info from API
- [x] `raise_for_status()` on HTTP errors
- [x] Delete file via `write("file_delete")` with `project_id`, `path`, optional `ref`/`branch` (default `main`), `sha`, and `message` — `DELETE /projects/{id}/repository/files/{path}`
- [x] Recursive directory listing via `query("tree")` — `GET /projects/{id}/repository/tree` with optional `path`, `ref`, and `recursive` filters; returns blob/tree entries
- [x] Batch file operations via `write("files")` / `write("commit")` — one atomic `POST /projects/{id}/repository/commits` with `actions` (`create`/`update`/`delete`/`move`/`chmod`), each action requiring a validated `file_path`; `move` requires `previous_path`
- [x] Path traversal protection — local validation rejects absolute paths and `..` segments on `query("file")`, `query("tree")`, `write("file")`, `write("files")`, and `write("file_delete")` before they reach the API

### Merge Request Operations — listing and creation

- [x] List merge requests via `query("mrs")` with `project_id` and optional `state` filter
- [x] Default MR state filter to `"opened"`
- [x] Create merge request via `write("mr")` with `project_id`, `title`, `description`, `source_branch`, `target_branch`
- [x] Merge MR via `write("mr_merge")` with `project_id`, `iid`, optional `squash`, `merge_commit_message`, `should_remove_source_branch`, `merge_when_pipeline_succeeds`
- [x] Approve MR via `write("mr_approve")` with `project_id`, `iid`, optional `sha`
- [x] Add MR comment via `write("mr_comment")` / `write("mr_note")` with `project_id`, `iid`, `body`
- [x] Accept merge request with squash — `write("mr_merge")` with `squash: true`
- [x] List MR diff/changed files via `query("mr_changes")` with `project` and `iid` — `GET /projects/{id}/merge_requests/{iid}/changes`, returned as `records[0]["changes"]`
- [x] Set MR labels via `write("mr_labels")` with `project`, `iid`, `labels`
- [x] Request MR approval via `write("mr_approval_request")` — creates an approval rule (`POST /projects/{id}/merge_requests/{iid}/approval_rules`, `rule_type: "approval"`) requesting approval from specific `user_ids` and/or `user_emails`; requires at least one user; optional `name`/`approvals_required`
- [x] `query("mrs")` supports pagination — `next_cursor` from `X-Next-Page`, cursor forwarded as `page`

### Capability Declaration

- [x] `ConnectorType.GITLAB` defined in `base.py` enum
- [x] `GitLabConnector.connector_type` returns `ConnectorType.GITLAB`
- [x] `ConnectorType.GITLAB.capabilities` returns `{read, write, git_push, create_pr, issue_read, issue_write, issue_search, trigger_run}` in `base.py`

### Health Check — connectivity and credential validation

- [x] Validate token by calling `GET /user` — fail if status != 200
- [x] Probe API access by calling `GET /projects` during health check
- [x] Return authenticated user info in `detail` on success
- [x] Skip per-project repository-level checks
- [x] `GET /projects` non-2xx response (not just 401/403) returns HealthResult(ok=False)
- [x] Detect expired tokens (401) vs insufficient scopes (403) vs network errors
- [x] Report which scope/endpoint is denied in health check detail
- [x] Report the GitLab instance version on self-hosted health checks (best-effort `GET /version` probe, appended as `(GitLab <version>)`; a missing/forbidden/failed probe never fails the health check)
- [ ] Per-operation scope verification — no granular check before `write()` calls

### Error Handling

- [x] Missing required filter key raises ValueError with descriptive message
- [x] Invalid pagination cursor (non-numeric) raises ValueError with descriptive message
- [x] HTTP 4xx/5xx API errors wrapped as ValueError with status code and detail (via _call_api)
- [x] HTTP 304 Not Modified wrapped as ValueError with descriptive message
- [x] Connection errors (ConnectError) wrapped as ValueError (via _call_api)
- [x] Timeout errors (TimeoutException) wrapped as ValueError with specific "GitLab API timeout" message
- [x] Invalid JSON response wrapped as ValueError (via `_safe_json` narrowed to `json.JSONDecodeError`)
- [x] Retryable statuses (429, 502, 503, 504) retried with exponential backoff + jitter (max 3 retries)
- [x] `Retry-After` header respected for rate-limited responses
- [x] `RateLimit-ResetTime` header respected on 429 (waits for quota window reset instead of blind backoff)
- [x] `RateLimit-*` quota headers surfaced in final 429 error detail
- [x] `last_exc` assigned in every retry `except` block — exception chain preserved on retry exhaustion
- [x] Other `httpx.HTTPError` subclasses (StreamError, ProtocolError, DecodingError, TooManyRedirects) wrapped as ValueError
- [x] All `query()` and `write()` `r.json()` calls use `_safe_json` — no bare `except Exception` in JSON parsing
- [x] Health check catches httpx.RequestError returning HealthResult(ok=False)
- [x] Health check catches JSON decode errors returning HealthResult(ok=False) — narrowed to `json.JSONDecodeError`
- [x] API error detail includes GitLab's `X-Request-Id` header when present (via `_error_detail`)
- [x] Health check failure details include GitLab's `X-Request-Id` header when present (via `_id_suffix`)
- [x] Path traversal blocked locally — absolute paths and `..` segments raise ValueError on `query("file")`, `write("file")`, `write("file_delete")` before any request is sent
- [ ] Missing token during construction is not validated until first API call
- [ ] Project path encoding fails gracefully on malformed project IDs (e.g. None, numbers)

### Resilience & Integration Robustness

- [x] `_call_api` retries 429/502/503/504 with exponential backoff + jitter (max 3 retries)
- [x] `_call_api` raises ValueError for 304 Not Modified — resource unchanged
- [x] `_call_api` respects `Retry-After` header from GitLab API
- [x] `_call_api` prefers `RateLimit-ResetTime` on 429 for precise quota-window waits
- [x] `_safe_json` narrowed to `json.JSONDecodeError` only — prevents masking programming errors in all query/write paths
- [x] Health check consolidates /user and /projects calls into single client session
- [x] Retry timeout treated separately from connection errors — distinct error messages
- [x] Other `httpx.HTTPError` subclasses (StreamError, ProtocolError, etc.) caught and wrapped in retry loop
- [x] Report GitLab `RateLimit-*` headers (`Limit`/`Remaining`/`Observed`/`Reset`/`ResetTime`) as `metadata["rate_limit"]` on query results
- [x] Absent rate-limit headers produce an empty `metadata["rate_limit"]` dict (no crash, no phantom data)
- [x] Missing write scope raises ValueError before the API call — a token lacking the scope an operation requires is blocked with a descriptive message (e.g. `requires scope 'write_repository'`, `token declares: read_api`) and the API is never reached
- [x] `verify_write_scopes(resource)` exposes the missing-scope set programmatically — returns `frozenset()` when satisfied or when scopes cannot be determined

## Known Gaps

- **Scope verification incomplete** — largely RESOLVED: `write_repository`/`api` are verified during health check via a best-effort `GET /oauth/token/info` probe, and every `write()` now fails fast when the token lacks the scope the operation requires (see QA History 2026-08-09). Pre-run ConnectorHub blocking of insufficient scopes remains unimplemented.
- [ ] **Pre-run ConnectorHub scope blocking**: a connector instance with insufficient scopes still permits run start — the health check flags it and writes fail fast, but the ConnectorHub does not refuse to build/execute the connector.
- [ ] **Self-hosted discovery**: `base_url` is configurable per connector instance, but there is no instance-discovery/onboarding flow for self-hosted GitLab

## QA History

### 2026-08-09 — improve-architecture: per-operation scope verification RESOLVED

**RESOLVED the last GitLab scope gap** — "Per-operation scope verification — no granular check before `write()` calls". Before dispatching any write, `GitLabConnector.write()` now verifies the token actually has the scope the operation requires and fails fast with a descriptive `ValueError` instead of letting the API reject with an opaque 403.

- New module map `_WRITE_SCOPE_REQUIREMENTS` — repository-file writes (`file`, `files`/`commit`, `file_delete`) require `write_repository`; every other write (`mr`/`merge_request`, `mr_comment`/`mr_note`, `mr_merge`, `mr_approve`, `mr_approval_request`, `mr_labels`, `issue`, `issue_update`, `issue_note`, `issue_label`, `label`, `milestone`, `pipeline_run`) requires `api`. The `api` superset satisfies all of them via the existing `_effective_scopes()` relation.
- New `_ensure_write_scope(resource)` runs at the top of `write()` and raises `ValueError` (e.g. `GitLab write resource 'file' requires scope 'write_repository'; token declares: read_api`) before any request is sent. It is strictly best-effort: when declared scopes can't be determined (404 on older self-hosted GitLab, network error, unparseable body, empty scope list) the check is skipped and the API enforces scope as before.
- Scope probe is cached per instance for `_SCOPE_CACHE_TTL` (5 min) via new `_scope_cache` + `_declared_scopes_cached()`/`_probe_declared_scopes()`, so the token-info round-trip happens at most once per window rather than on every write. A successful `health_check()` now warms the cache (refactored to `_declared_effective_scopes(client)` + cache write), so connectors health-checked at init verify the first write without re-probing.
- New public `verify_write_scopes(resource)` returns the missing-scope set programmatically (empty when satisfied or when scopes cannot be determined) for callers that want to probe before dispatching.

**Tests:** 11 new unit tests in `test_gitlab.py` — file write blocked without `write_repository` (write endpoint never called), scope error lists declared scopes, MR write blocked without `api`, `write_repository`-only token can write files, `api`-only token satisfies MR + file writes, writes proceed when token-info is 404/network-error (fail-open), `verify_write_scopes` returns exact missing sets / empty-when-unknown, scope cache avoids re-probing across consecutive writes, health check warms the write-scope cache. **171/171 gitlab unit tests pass (160 + 11 new), ruff check + format clean, mypy --strict clean.** Status: partial (pre-run ConnectorHub scope blocking + self-hosted discovery remain).

### 2026-08-05 — improve-architecture (index 145+)

**RESOLVED the individual-scope-probing gap** (`write_repository`/`api` were declared in `REQUIRED_SCOPES` but never verified; only `read_api` was inferred from `/projects` HTTP status).

- New best-effort `_missing_scopes(client)` reads the token's declared scopes from the instance `GET /oauth/token/info` endpoint (Doorkeeper token introspection, works for PATs on GitLab 11.6+ and self-hosted). The probe reuses the open health-check client and hits the *instance root* (`https://gitlab.com/oauth/token/info`), not the versioned `/api/v4` path — new `_instance_root()` helper strips the `/api/vN` segment and preserves reverse-proxy mount paths.
- New `_effective_scopes()` treats `api` as a superset scope: a token declaring `api` satisfies `read_api` + `write_repository` (GitLab's full-access scope), so `api`-only tokens keep passing health.
- `health_check()` now returns `ok=False` with `Missing scopes: <individually listed>` when the token's declared scopes fall short of `REQUIRED_SCOPES` (e.g. `read_api`-only → `api, write_repository` missing; `read_api`+`write_repository` → `api` missing). The probe is strictly non-fatal: a 404 (older self-hosted versions), network error, invalid/non-object body, empty scope list, or the deprecated `scopes` alias all degrade gracefully to the existing `/user`+`/projects` endpoint probes.

**Tests:** 12 new unit tests — `test_gitlab.py` (missing write_repository reported, missing api only, `api` superset passes, token-info 404/network-error/invalid-body/empty-scopes all non-fatal, self-hosted probe hits the instance root) + `test_gitlab_resilience.py` (deprecated `scopes` alias, space-separated string scopes, `_instance_root()` derivation matrix, `_effective_scopes()` matrix). 8 existing health-check tests updated to mock the token-info route. **160/160 gitlab unit tests pass (148 + 12 new), ruff clean, mypy --strict clean.** Status: partial (pre-run ConnectorHub scope blocking + self-hosted discovery remain).

### 2026-08-04 — improve-architecture: 3 known gaps RESOLVED (recursive tree listing, batch file ops, MR approval request)

**RESOLVED known gaps** "Recursive directory listing & batch file ops", "MR approval requests", and the "Request MR approval" behaviour. Added to `connectors/gitlab/__init__.py`:

- **Recursive directory listing** — new `query("tree")` (`GET /projects/{id}/repository/tree`) with optional `path`, `ref`, and `recursive` filters (forwarded verbatim) plus pagination via the existing `X-Next-Page` cursor contract. Returns blob/tree entries with `name`/`type`/`path`.
- **Batch file operations** — new `write("files")` / `write("commit")` using GitLab's Commits API (`POST /projects/{id}/repository/commits`) to apply multiple file changes atomically in a single commit. Accepts `branch`/`ref` (default `main`) and `message`, plus an `actions` list where each action is `{action, file_path, content, previous_path}`. `action` is whitelisted to GitLab's `create`/`update`/`delete`/`move`/`chmod`; empty actions, non-object actions, missing `file_path`, and `move` without `previous_path` all raise clear `ValueError`s. Every `file_path` (and `previous_path`) is run through the existing `_validate_path()` traversal guard.
- **MR approval request** — new `write("mr_approval_request")` creates an approval rule (`POST /projects/{id}/merge_requests/{iid}/approval_rules`, `rule_type: "approval"`) requesting approval from specific users via `user_ids` and/or `user_emails`; at least one user reference is required (`ValueError` otherwise), with optional `name` and `approvals_required`.

**Tests:** 18 unit tests in `test_gitlab.py` (tree entries + recursive/path/ref params + next_cursor + path-traversal + missing-project, batch commit body + custom branch/message + move action + empty-actions + invalid-action + missing-file_path + move-missing-previous_path + traversal on file_path and previous_path, mr_approval_request by ids / by emails with name+approvals_required / no-users error / missing project) + 5 BDD scenarios in `gitlab_issues.feature` (recursive tree listing with nested entries, batch write reports commit id, batch path traversal blocked, approval request reports requested approvers, approval request without users errors) with 7 new step definitions and the mock connector extended. Updated product map `connectors/gitlab-connector.md` (3 behaviours `[ ]`→`[x]`, Known Gaps 4→2, QA History). 148/148 gitlab unit tests + 5/5 new gitlab BDD scenarios pass, ruff clean, mypy strict clean (19 pre-existing connector-suite BDD failures unchanged). Status: partial (individual scope probing + pre-run ConnectorHub blocking, self-hosted discovery remain).

### 2026-08-03 — improve-architecture: 4 known gaps RESOLVED (MR changes, X-Request-Id, self-hosted version, path traversal)

**RESOLVED known gaps** "No X-Request-Id reporting", "MR diff/changed-files listing", "No self-hosted version reporting", and "Path traversal protection". Added to `connectors/gitlab/__init__.py`:

- **MR diff/changed files** — new `query("mr_changes")` (`GET /projects/{id}/merge_requests/{iid}/changes`) returning the full MR response as `records[0]`, so `records[0]["changes"]` is the list of changed files (`old_path`/`new_path`/`new_file`/`renamed_file`/`deleted_file`/`diff`) alongside the MR title/source/target.
- **X-Request-Id reporting** — new `_request_id()` / `_error_detail()` / `_id_suffix()` helpers. The final `_call_api` ValueError now appends `(request_id: <id>)` from GitLab's `X-Request-Id` header (on 4xx/5xx and exhausted retries), and every health-check failure detail (401 expired token, 403 missing scope, non-200, `/projects` failure) carries the request id when present, for GitLab support debugging.
- **Self-hosted version reporting** — `health_check()` now does a best-effort `GET /version` probe on self-hosted instances (`base_url != https://gitlab.com/api/v4`), appending `(GitLab <version>)` to the success detail. The probe is strictly diagnostic: a 403/404 or network error is swallowed and never fails the health check.
- **Path traversal protection** — new `_validate_path()` helper rejects absolute paths and `..` segments on `query("file")`, `write("file")`, and `write("file_delete")` with a clear `ValueError` before any request is sent.

**Tests:** 10 unit tests (`test_gitlab.py`: mr_changes records + missing-iid, path-traversal on query/write/file_delete, absolute-path rejection, nested relative path still allowed; `test_gitlab_resilience.py`: X-Request-Id on 500 / exhausted-429 / health-401, self-hosted version in detail, version-probe 403 and network-error both non-fatal, hosted instance does not probe `/version`) + 3 BDD scenarios in `gitlab_issues.feature` (query MR changes, health check reports instance version, path traversal on file write blocked) with 2 new step definitions and the mock connector extended. Updated product map (6 behaviours `[ ]`→`[x]`, Known Gaps 6→4, QA History). 146/146 gitlab unit tests pass, ruff clean. Status: partial (MR approval requests, individual scope probing + pre-run ConnectorHub blocking, recursive/batch file ops, self-hosted discovery remain).

### 2026-08-02 — improve-architecture (index 144)

**Review-fix pass (PR #544, non-blocking nits):**

- **Quota reset window no longer capped at `_MAX_DELAY` (30s)**: `_retry_delay()` now returns the `RateLimit-ResetTime`/`RateLimit-Reset` wait uncapped on 429, so a GitLab quota window longer than 30s is truly honoured instead of firing the retry early into another 429. `Retry-After` and exponential backoff remain capped at `_MAX_DELAY`.
- **`RateLimit-Reset` gated to HTTP 429 in `_has_server_delay()`**: GitLab reports `RateLimit-*` headers on every response while rate limiting is active; they previously switched 502/503/504 backoff retries to tight jitter around the exponential backoff. Now only 429 treats the reset header as a server-provided wait, keeping full-jitter thundering-herd protection on other retryable statuses. `Retry-After` remains honoured on any status.
- **Cosmetic**: removed a stray leading space on the `Return HealthResult(ok=True)` behaviour line.

**Tests:** added 4 unit tests in `test_gitlab_resilience.py` (uncapped reset-window delay, Retry-After/backoff still capped, `_has_server_delay` gated to 429 for `RateLimit-Reset`, `Retry-After` honoured on any status).

### 2026-08-02 — improve-architecture (index 143)

**RESOLVED 4 known gaps in one pass** (self-hosted support, file deletion, MR comment/merge/labels, rate-limit header inspection):

- **Self-hosted GitLab**: `GitLabConnector.__init__` now accepts an optional `base_url` (default `https://gitlab.com/api/v4`, trailing-slash-normalised); `_client()` uses it for every request. Wired through `connector_hub._build_connector()` and `polling._build_polling_connector()` via `config.get("base_url", ...)` — mirroring the existing `gitlab_ci` pattern.
- **File deletion**: new `write("file_delete")` — `DELETE /projects/{id}/repository/files/{path}` with `ref`/`branch` (default `main`), optional `sha` and `commit_message`.
- **MR operations**: new `write("mr_comment")`/`write("mr_note")` (`POST …/merge_requests/{iid}/notes`), `write("mr_merge")` (`PUT …/merge_requests/{iid}/merge` with optional `squash`/`merge_commit_message`/`should_remove_source_branch`/`merge_when_pipeline_succeeds`), `write("mr_approve")` (`POST …/merge_requests/{iid}/approve`), and `write("mr_labels")` (`PUT …/merge_requests/{iid}` with `labels`).
- **RateLimit-* inspection**: added `_rate_limit_metadata()` + `_RATE_LIMIT_HEADERS` and a `metadata: dict` field on the shared `ConnectorResult` dataclass (`base.py`, additive + defaulted). Every query result now carries `metadata["rate_limit"]` mirroring GitLab's `RateLimit-Limit`/`Remaining`/`Observed`/`Reset`/`ResetTime` headers when present (absent → `{}`). On 429, `_retry_delay()` prefers `RateLimit-ResetTime` (quota-window wait) over blind backoff, and `_rate_limit_detail()` surfaces the `RateLimit-*` quota headers in the final 429 error.

**Scope verification → partial**: health check now distinguishes expired tokens (401 on `/user` → "Invalid or expired GitLab token") from missing scopes (403 on `/user` → needs read_user/api; 403 on `/projects` → read_api/api not granted), and reports which endpoint/scope is denied. Individual `write_repository`/`api` probing and pre-run ConnectorHub scope blocking remain.

**Tests:** 13+ unit tests in `test_gitlab.py` (rate-limit metadata on list + single-resource, empty-metadata fallback, self-hosted base URL routing for health-check + query + trailing-slash normalisation, default base URL, `file_delete` success/default-ref/sha/missing-filters, `mr_comment`, `mr_note`, `mr_merge`+squash, `mr_approve`, `mr_labels`) + resilience tests (RateLimit-ResetTime retry, RateLimit-* 429 detail, health 403 scope detail, health 401 expired token, health ok with quota headers). 99/99 gitlab unit tests + 14/14 resilience + 47/47 gitlab-issues unit tests pass, ruff clean.

**Status:** partial (scope verification, MR approval-request/MR-diff, recursive/batch file ops, path traversal protection, `X-Request-Id` reporting, self-hosted version/discovery flow remain).

### 2026-08-02 — improve-architecture (index 142)

**RESOLVED known gap "No pagination"**: `query("projects")` and `query("mrs")` didn't return `next_cursor`.

- Added `_parse_next_page()` — reads GitLab's `X-Next-Page` header, returning `None` when absent or `"0"` (last page), and `_paginate_params()` — forwards `ConnectorQuery.cursor` as the GitLab `page` query param, raising `ValueError` for non-numeric cursors.
- Wired both into all list resources: `projects`, `mrs`/`merge_requests`, `issues`, `labels`, `milestones`, `issue_notes`, `issue_discussions`, `branches`, `tags`, `pipelines`, `jobs`. Single-record resources (`file`, `issue`, `merge_request`, `label`, `branch`) still return `next_cursor=None`.
- Updated connector docstring to document the `next_cursor` contract.

**Product map updates:** behaviours `[ ]`→`[x]` (Project Operations pagination, MR pagination), Known Gaps removed the pagination entry.

**Tests:** Added 8 unit tests in `test_gitlab.py` (projects/mrs/issues/pipelines `next_cursor` echo, `"0"`→`None` boundary, cursor→`page` param forwarding, invalid cursor → `ValueError`, single-resource no cursor) + 2 BDD scenarios in `gitlab_issues.feature` (paginated query returns next page cursor, last page has no cursor) with `When … on page "{page}"` step + `Then` next-cursor steps. Also fixed 2 pre-existing broken BDD scenarios (error-on-write/query — steps referenced didn't exist).

**Status:** partial (5 known gaps remain). 23/23 gitlab unit tests + 6/6 new/fixed BDD scenarios pass, ruff clean.

### 2026-07-08 — Cross-cutting QA (improve-architecture index 266)

**CRITICAL fixes applied:**
- Narrowed `_parse_json` from `except Exception` to `except json.JSONDecodeError` — previously masked programming errors (TypeError, AttributeError, etc.) as "invalid response" instead of propagating them.
- Added retry/backoff for 429/502/503/504 with exponential backoff + jitter (max 3 retries, matching GitHub/Slack/Jira/Linear connector pattern) — previously no retry on rate limits or server errors, causing transient failures to propagate as hard failures.
- Added 304 Not Modified handling — raises ValueError with descriptive message ("resource unchanged").
- `last_exc` assigned in every `except` block in retry loop — preserves exception chain on retry exhaustion (per AGENTS.md Lessons Learned).

**MAJOR fixes applied:**
- Consolidated health_check from two separate `httpx.AsyncClient` sessions into one — fewer TCP connections per health check.
- Health check catches `json.JSONDecodeError` (narrowed from bare Exception) on `/user` response.

**Product map updates:**
- Added Resilience & Integration Robustness section (6 checkboxes: 6 [x]) covering retry/backoff, 304 handling, narrowed `_parse_json`, Retry-After support, single client session, and distinct timeout/connection error messages.
- Updated Error Handling section (12 checkboxes — added 304, retry/backoff, narrowed parse, Retry-After, last_exc: 5 new [x]).
- Removed stale `qa-history` frontmatter field (migrated to proper QA History section).
- Moved inline `except Exception`→`except json.JSONDecodeError` finding from gap to fixed.
- Updated Known Gaps: corrected "No rate-limit handling" (429s now retried) → now tracks only RateLimit-* header inspection gap.

**Tests:**
- Added 8 new tests in test_gitlab_resilience.py: 429 retry-then-success, 502 retry-then-success, 429 retry exhaustion still returns ValueError, 304 Not Modified → ValueError, write 429 retry-then-success, health check single client session, narrowed `_parse_json` with list response, timeout message match.
- Fixed 1 test (test_query_timeout_returns_value_error) to assert "GitLab API timeout" instead of "GitLab API connection error" following retry refactor.

**Status:** partial (6 known gaps unchanged). All 76 connector unit tests pass (69 existing + 7 new).

### 2026-07-07 — Cross-cutting QA (improve-architecture index 320)

**CRITICAL fixes applied:**
- Narrowed all 21 inline `r.json()` calls in `query()` and `write()` from `except Exception` to `_safe_json()` (catches only `json.JSONDecodeError`). The previous fix (index 266) only narrowed `_parse_json` but the same bare `except Exception` pattern persisted in every query and write handler — systematic gap that masked programming errors (TypeError, AttributeError) as "invalid response".
- Added `except httpx.HTTPError` catch-all to `_call_api` retry loop. Previously only `HTTPStatusError`, `TimeoutException`, and `ConnectError` were caught — `StreamError`, `ProtocolError`, `DecodingError`, `TooManyRedirects` propagated uncaught as 500s.

**MAJOR fixes applied:**
- Health check `/projects` response now validates all non-2xx status codes (not just 401/403). Previously a 500 from `/projects` silently passed the health check as `ok=True`.
- `query("projects")` now forwards `search`, `membership`, `visibility`, `owned` filter params to the GitLab API.

**Product map updates:**
- Error Handling section: +2 [x] for `httpx.HTTPError` catch-all and `_safe_json` coverage of all query/write paths.
- Health Check section: +1 [x] for `/projects` non-2xx validation.
- Project Operations: `search` filter now [x]; `membership/visibility/ownership` filter now [x].
- Resilience section: updated `_safe_json` description; added `httpx.HTTPError` catch-all [x].
- Added `_safe_json` helper as the single JSON parsing path for all query/write responses.

**Tests:** 76 passed (unchanged — all existing tests still pass with narrowed exception handlers).

### 2026-07-12 — Round 3 QA (improve-architecture batch 2)

**Fixed (MAJOR):** Added `_jitter()` static method and applied random jitter to all 5 retry sleep calls in `_call_api()`. The product map claimed "exponential backoff + jitter" but the code had pure exponential backoff without jitter, creating a thundering-herd risk on coordinated retries (e.g. webhook-triggered deployments). This was fixed in GitHub/Linear/Jira/Slack connectors in previous Round 3 passes but was missed for GitLab.

**Fixed (MINOR):** Removed redundant `or response.headers.get("retry-after")` from `_parse_retry_after()` — httpx response headers are case-insensitive so the second lookup was redundant. (GitHub connector already had this pattern fixed.)

**Fixed (MINOR):** Fixed B904 — added `from None` to `raise ValueError(...)` in `_require_filter()` `except KeyError` handler.

**Fixed (MINOR):** Inlined 9 `_safe_json(r)` assignments in `write()` method — replaced `X = _safe_json(r); return X` with `return _safe_json(r)` to fix RET504 flags.

**Product map fixed:** Added `# noqa: S311` to `_jitter()` — same suppression as GitHub connector.

**Status:** partial (6 known gaps unchanged).
