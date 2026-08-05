---
id: feat-connectors-jira
prd: 8.6
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/connector_health.feature
  - backend/tests/bdd/features/connectors/jira_connector.feature
unit-tests:
  - backend/tests/unit/connectors/test_jira.py
  - backend/tests/unit/connectors/test_jira_resilience.py
code:
  - backend/src/modulo/connectors/jira/__init__.py
  - backend/src/modulo/connectors/base.py
depends-on:
  - feat-connectors-hub
status: partial
---

# Jira Connector

Async Jira Cloud REST API v3 connector implementing `ConnectorBase`. Provides read/write access to Jira issues for agent pipelines. Supports both Personal Access Token and email+API token authentication. Belongs to the `issue-tracker` connector type.

## Behaviours

### Authentication — PAT and Basic Auth

- [x] Accept `instance` (domain) and credentials on construction
- [x] Support PAT authentication via `token` parameter
- [x] Support Basic Auth via `email` + `api_token` parameters
- [x] Set appropriate `Authorization` header based on auth method
- [x] Use `httpx.AsyncClient` with base URL `https://{instance}/rest/api/3`
- [x] `health_check()` calls `GET /myself` to validate credentials
- [x] Return authenticated user displayName on success
- [x] Return `HealthResult(ok=False)` with HTTP status on non-200
- [x] Return `HealthResult(ok=False)` with detail containing status code — Jira Cloud 401 vs 403 distinction is visible in the detail string
- [ ] Jira Data Center (self-hosted) API support — URL format differs

### Issue Operations — CRUD via Jira REST API

- [x] Get single issue via `query("issue")` with issue `key` filter
- [x] Return issue fields (summary, description, status, assignee, etc.)
- [x] Search issues via `query("search")` with JQL string and optional `maxResults`
- [x] Default `maxResults` to connector limit or Jira API default (50)
- [x] Create issue via `write("issue")` with `project`, `summary`, `issuetype`, optional `description`, `priority`, `assignee`
- [x] Update issue fields via `write("issue_update")` with `key` and fields to update
- [x] Raise `ValueError` for unsupported resources in `query()` and `write()`
- [x] Transition issue status via `write("transition")` with `transition_id`
- [x] List available transitions via `query("transitions")`
- [x] Add issue comment via `write("issue_comment")` with `body`
- [x] List issue comments via `query("issue_comments")` with pagination
- [x] Assign/reassign issue via dedicated `write("issue_assign")` operation — accepts `account_id` (direct), `email` or `display_name` (resolved via `GET /user/search`), or explicit `null`/`unassign` to clear the assignee
- [ ] Add issue attachment — not implemented
- [ ] List issue remote links — not implemented
- [x] Set issue labels via dedicated `write("issue_label")` operation — `add`/`remove` label names resolved against the issue's current `labels` set before PUT (true add/remove, not a blind replace)
- [x] Delete issue via dedicated `write("issue_delete")` operation — `DELETE /issue/{issue_key}` with success confirmation
- [x] JQL search supports pagination cursor via `startAt` parsing
- [x] JQL search returns total count
- [x] Discover create-issue field metadata via `query("field_metadata")` with required `project` filter — `GET /issue/createmeta?projectKeys={project}&expand=projects.issuetypes.fields`, returns the project's issue types with their create-issue field definitions (system + custom fields); `metadata["project"]` echoes the requested key
- [x] List all instance fields via `query("fields")` — `GET /field`, returns system + custom fields (`custom: true` flags custom fields)

### Project Operations — discovery and metadata

- [x] List accessible projects via `query("projects")` — returns key, name, lead, avatarUrls
- [x] Get project metadata (issue types, statuses, fields) — via `query("field_metadata")`, `query("fields")`, and `query("statuses")`
- [x] List issue-type statuses for a project via `query("statuses")` with required `project` filter — `GET /project/{project}/statuses`, returns each issue type with its statuses (incl. status category); `metadata["project"]` echoes the requested key
- [ ] Get project components and versions — not implemented

### Capability Declaration

- [x] `ConnectorType.JIRA` defined in `base.py` enum
- [x] `ConnectorType.JIRA.capabilities` returns `{ISSUE_READ, ISSUE_WRITE, ISSUE_SEARCH}` in `base.py`
- [x] `JiraConnector.connector_type` returns `ConnectorType.JIRA`

### Health Check — connectivity and credential validation

- [x] Validate credentials by calling `GET /myself` — fail if status != 200
- [x] Return authenticated user display name in `detail` on success
- [x] Return HTTP status and response body on failure
- [x] Catch `httpx.HTTPStatusError` — returns `HealthResult(ok=False)` with status code and response text
- [x] Catch `httpx.TimeoutException` and `httpx.ConnectError` — returns `HealthResult(ok=False)` with connection error detail
- [x] Catch any generic `Exception` — returns `HealthResult(ok=False)` with truncated message
- [ ] Detect expired tokens vs invalid instance URL vs network errors — partially covered: HTTP errors and network errors distinguished, but token expiry vs invalid URL both produce HTTP 401
- [ ] Per-operation permission check before write operations

### Prompt Portability — issue-tracker terminology

- [ ] Connector type abstraction handles API operations
- [ ] Agent prompt templates may use Jira-specific terminology ("issue", "story", "epic", "sprint")
- [ ] Prompt portability is user's responsibility — documented limitation

### Error Handling

- [x] `health_check` catches `ValueError` (from `_call_api`) — returns `HealthResult(ok=False)` with truncated message
- [x] `health_check` catches generic `Exception` — returns `HealthResult(ok=False)` with truncated message
- [x] Query/write methods catch `httpx.HTTPStatusError` — raises `ValueError` with status code and response text
- [x] Query/write methods catch `httpx.TimeoutException` — raises `ValueError` after retries exhausted
- [x] Query/write methods catch `httpx.ConnectError` — raises `ValueError` after retries exhausted
- [x] Query/write methods catch JSON decode errors — raises `ValueError` with response text snippet
- [x] `query("issue")` with missing `issue_key` — raises `ValueError` with descriptive message
- [x] `write("issue_update")` with missing `issue_key` — raises `ValueError` with descriptive message
- [x] `query()` with unsupported resource — raises `ValueError`
- [x] `write()` with unsupported resource — raises `ValueError`
- [x] `_call_api` retries 429/502/503/504 with exponential backoff + jitter (max 3 retries)
- [x] `_call_api` raises `ValueError` for 304 Not Modified — resource unchanged
- [x] `_call_api` respects `Retry-After` header from Jira API
- [x] `_call_api` prefers Jira Cloud `X-RateLimit-Reset` (quota window) on 429 instead of blind backoff
- [x] `_call_api` surfaces `X-RateLimit-*` quota headers in the final 429 error message
- [x] `_parse_json` narrowed to `json.JSONDecodeError` only — prevents masking programming errors

### Resilience & Integration Robustness

- [x] `_compute_delay` includes random jitter — prevents thundering herd on retry
- [x] `_compute_delay` respects `Retry-After` header when present
- [x] `_compute_delay` capped at `_MAX_DELAY` (30s)
- [x] `_compute_delay` extracted as shared helper — eliminates duplication across 3 retry paths
- [x] `_parse_retry_after` simplified to single case-insensitive header lookup
- [x] Retry delay formula deduplicated via `_compute_delay` helper
- [x] Required field validation uses `key not in` pattern — rejects empty/None without falsy ambiguity
- [x] `test_jira_resilience.py` covers jitter, exponential backoff, Retry-After, retry 502/503/504 → success, 429 exhaustion
- [x] Required field validation edge cases tested (missing issue_key, missing body, missing transition_id)
- [x] Query results expose `metadata["rate_limit"]` mirroring Jira Cloud `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset` response headers when present (empty dict when absent)
- [x] `_parse_rate_limit_reset` derives the wait from the `X-RateLimit-Reset` epoch header — missing/invalid/elapsed header → `None`
- [x] `_sleep_delay` applies tight jitter around the quota-reset wait so the window is honoured
- [x] `_rate_limit_detail` summarises `X-RateLimit-*` quota headers for error/health detail strings

## Known Gaps

- [ ] **Jira Data Center not supported**: URL format is `https://{instance}/rest/api/3` — Jira Server/Data Center uses a different path structure
- [ ] **No attachment support**: cannot upload or download issue attachments
- [x] ~~**No field metadata**~~ — **RESOLVED (2026-08-05)**: agents can now discover create-issue fields + custom fields (`query("field_metadata")`), all instance fields (`query("fields")`), and per-project issue-type statuses (`query("statuses")`)

---

## QA History

### 2026-08-05 — improve-architecture: field metadata RESOLVED

**RESOLVED known gap** "No field metadata". Agents can now discover custom fields, available issue types, and statuses for a given project via three new `query()` resources in `connectors/jira/__init__.py`:

- **`query("field_metadata")`** — requires a `project` filter; calls `GET /issue/createmeta?projectKeys={project}&expand=projects.issuetypes.fields` and returns the project's issue types with their create-issue field definitions (system fields such as `summary` plus custom fields such as `customfield_*`). `ValueError` when the `project` filter is missing; empty `projects` list (unknown project) yields empty records. `metadata["project"]` echoes the requested key.
- **`query("fields")`** — calls `GET /field` and lists every field across the instance (system + custom, `custom: true` flags custom fields). No filter required.
- **`query("statuses")`** — requires a `project` filter; calls `GET /project/{project}/statuses` and returns each issue type for the project with its available statuses (incl. status category). `ValueError` when the `project` filter is missing; `metadata["project"]` echoes the requested key.

All three return `ConnectorResult` with `metadata["rate_limit"]` (empty dict when headers absent).

**Tests:** 8 unit tests in `test_jira.py` (field_metadata records + createmeta params + project echo + unknown-project empty + missing-project error, fields records + custom flag + rate-limit metadata, statuses records + status categories + project echo + missing-project error + 404 api error) + 5 BDD scenarios in `jira_connector.feature` (discover field metadata, missing-project filter error, query all fields incl. custom, query project statuses, missing-project statuses error) with 6 new step definitions and the mock connector extended. Updated product map (3 behaviours `[ ]`→`[x]`, 1 Known Gap → RESOLVED, BDD count 13→18, QA History). 57/57 `test_jira.py` + 27/27 `test_jira_resilience.py` unit tests pass, 18/18 jira BDD scenarios pass (19 pre-existing connector-suite BDD failures unchanged), ruff clean, mypy strict clean. Status: partial (Jira Data Center, attachments remain).

### 2026-08-03 — improve-architecture: assign / labels / delete RESOLVED
- **RESOLVED 3 known gaps** in the Jira connector. (1) Dedicated assignment — `write("issue_assign")` accepts `{"issue_key", "account_id"}` (direct `accountId`), `{"issue_key", "email"}` or `{"issue_key", "display_name"}` (resolved via `GET /user/search?query=...&maxResults=1`, `ValueError` when no user / no `accountId` returned), or explicit `{"account_id": null}` / `{"unassign": true}` to clear the assignee (PUT `{"fields": {"assignee": null}}`). (2) Label management — `write("issue_label")` accepts `{"issue_key", "add": [...], "remove": [...]}`; because Jira's `labels` field is a set (PUT replaces the whole list), new `_compute_target_labels()` fetches the issue's current labels and computes the add/remove target atomically in one PUT. (3) Issue delete — `write("issue_delete")` (`DELETE /issue/{issue_key}`, success confirmation). Added 14 unit tests (`test_jira.py`: assign by account_id / by email / by display_name / unassign via null / unassign via flag / missing key / no-identifier error / user-not-found, labels add+remove / add-dedupe / missing add/remove / missing key, delete + missing key) + 4 BDD scenarios in `jira_connector.feature` (assign, unassign, add/remove labels, delete) with 8 new step definitions in `test_connectors.py` and the mock connector extended. Updated product map (4 behaviours `[ ]`→`[x]`, Known Gaps 6→3, QA History). 49/49 `test_jira.py` + 27/27 `test_jira_resilience.py` unit tests pass, 13/13 jira BDD scenarios pass, ruff clean. Status: partial (Jira Data Center, attachments, field metadata remain).


### 2026-08-02 — improve-architecture: X-RateLimit-* header inspection RESOLVED
- **RESOLVED** known gap "No X-RateLimit-* header inspection: rate-limit retry is blind (no remaining/quota tracking)". Jira Cloud reports quota state via `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset` (epoch) headers on every response. Added `_RATE_LIMIT_HEADERS`, `_parse_rate_limit_reset()` (epoch → wait delay; missing/invalid/elapsed → `None`), `_rate_limit_detail()` and `_rate_limit_metadata()` to `connectors/jira/__init__.py`. `_call_api` now prefers `X-RateLimit-Reset` on HTTP 429 via new `_sleep_delay()` (tight jitter around the quota window) instead of blind backoff, and the final exhausted-429 error surfaces the quota headers. Every query result now carries `metadata["rate_limit"]` (absent → `{}`). Timeout/ConnectError retry paths now use `_jitter()`.
- Added 16 unit tests (`test_jira.py`: rate-limit metadata on issue/search/projects, empty fallback; `test_jira_resilience.py`: reset parse ×4, quota detail summary ×2, metadata-only-present-headers, sleep-delay reset vs backoff, 429+reset retry-to-success, exhausted-429 quota detail) + 2 BDD scenarios in `jira_connector.feature` with step definitions in `test_connectors.py` (metadata exposed on results, quota detail in error).
- Updated product map (behaviours `[ ]`→`[x]`, Known Gaps 7→6, QA History). 62/62 jira unit tests pass, new BDD scenarios pass, ruff clean. Pre-existing connector-suite failures unchanged (10). Status: partial.

### Index 304 — 2026-07-10: Cross-cutting architecture QA
- **Fixed CRITICAL** — added `random` import and `_compute_delay()` helper with jitter (`random.uniform(0, 1)`) to all 3 retry delay paths (normal response, HTTPStatusError, TimeoutException/ConnectError). Previous code used pure exponential backoff without jitter despite product map claiming "exponential backoff + jitter". All 3 retry paths now produce varied delays, preventing thundering herd on retry.
- **Fixed MAJOR** — extracted `_compute_delay(attempt, response=None)` helper, consolidating 3 duplicated delay computation formulas into one shared function. The helper handles Retry-After parsing, exponential backoff, jitter, and capping at `_MAX_DELAY` (30s).
- **Fixed MAJOR** — simplified `_parse_retry_after` (removed redundant `or response.headers.get("retry-after")` — httpx headers are case-insensitive, single lookup suffices).
- **Fixed MAJOR** — changed all 5 falsy-check validations (`if not issue_key:`, `if not body:`, `if not transition_id:`) to `key not in filters/data` pattern, matching the established project convention and eliminating falsy-value ambiguity.
- **Added** 9 new resilience tests in `test_jira_resilience.py`: `_compute_delay` jitter verification, exponential backoff, capping, Retry-After parsing and capping, retry 502/503/504 → success, 429 exhaustion, required field validation edge cases (missing issue_key, missing body).
