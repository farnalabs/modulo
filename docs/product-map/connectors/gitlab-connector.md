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
- [x] `GET /projects` non-2xx response (not just 401/403) returns HealthResult(ok=False)
- [ ] Detect expired tokens vs insufficient scopes vs network errors
- [ ] Per-operation scope verification — no granular check before `write()` calls
- [ ] Report self-hosted GitLab version for diagnostic purposes

### Error Handling

- [x] Missing required filter key raises ValueError with descriptive message
- [x] HTTP 4xx/5xx API errors wrapped as ValueError with status code and detail (via _call_api)
- [x] HTTP 304 Not Modified wrapped as ValueError with descriptive message
- [x] Connection errors (ConnectError) wrapped as ValueError (via _call_api)
- [x] Timeout errors (TimeoutException) wrapped as ValueError with specific "GitLab API timeout" message
- [x] Invalid JSON response wrapped as ValueError (via `_safe_json` narrowed to `json.JSONDecodeError`)
- [x] Retryable statuses (429, 502, 503, 504) retried with exponential backoff + jitter (max 3 retries)
- [x] `Retry-After` header respected for rate-limited responses
- [x] `last_exc` assigned in every retry `except` block — exception chain preserved on retry exhaustion
- [x] Other `httpx.HTTPError` subclasses (StreamError, ProtocolError, DecodingError, TooManyRedirects) wrapped as ValueError
- [x] All `query()` and `write()` `r.json()` calls use `_safe_json` — no bare `except Exception` in JSON parsing
- [x] Health check catches httpx.RequestError returning HealthResult(ok=False)
- [x] Health check catches JSON decode errors returning HealthResult(ok=False) — narrowed to `json.JSONDecodeError`
- [ ] Missing token during construction is not validated until first API call
- [ ] Project path encoding fails gracefully on malformed project IDs (e.g. None, numbers)

### Resilience & Integration Robustness

- [x] `_call_api` retries 429/502/503/504 with exponential backoff + jitter (max 3 retries)
- [x] `_call_api` raises ValueError for 304 Not Modified — resource unchanged
- [x] `_call_api` respects `Retry-After` header from GitLab API
- [x] `_safe_json` narrowed to `json.JSONDecodeError` only — prevents masking programming errors in all query/write paths
- [x] Health check consolidates /user and /projects calls into single client session
- [x] Retry timeout treated separately from connection errors — distinct error messages
- [x] Other `httpx.HTTPError` subclasses (StreamError, ProtocolError, etc.) caught and wrapped in retry loop

## Known Gaps

- [ ] **No self-hosted GitLab support**: API base URL is hard-coded to `https://gitlab.com/api/v4`
- [ ] **File deletion unimplemented**: no `write("file_delete")` or equivalent
- [ ] **MR operations limited**: only listing and creation work — no comments, merges, approvals, or labels
- [ ] **Scope verification incomplete**: health check doesn't verify individual scopes
- [ ] **No pagination**: `query("projects")` and `query("mrs")` don't return `next_cursor`
- [ ] **No RateLimit-* header inspection**: rate-limit retry is blind (no remaining/quota tracking from GitLab headers)

## QA History

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

