---
id: feat-connectors-linear
prd: 8.6
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/connector_health.feature
  - backend/tests/bdd/features/connectors/linear_connector.feature
unit-tests:
  - backend/tests/unit/connectors/test_linear.py
  - backend/tests/unit/connectors/test_linear_resilience.py
  - backend/tests/unit/connectors/test_linear_errors.py
code:
  - backend/src/modulo/connectors/linear/__init__.py
  - backend/src/modulo/connectors/base.py
depends-on:
  - feat-connectors-hub
status: partial
---

# Linear Connector

Async Linear GraphQL API connector implementing `ConnectorBase`. BDD coverage: 28 scenarios with step definitions in `backend/tests/bdd/features/connectors/linear_connector.feature` and `backend/tests/bdd/steps/test_connectors.py`. Provides read/write access to Linear issues for agent pipelines. Authenticated via Linear API key. Belongs to the `issue-tracker` connector type family alongside `JiraConnector`.

## Behaviours

### Authentication — API key

- [x] Authenticate all requests via `Authorization: {api_key}` header
- [x] Set `Content-Type: application/json` header on all requests
- [x] Use `httpx.AsyncClient` with base URL `https://api.linear.app`
- [x] `health_check()` executes viewer query to validate API key
- [x] Return authenticated user name on success
- [x] Return `HealthResult(ok=False)` with error detail on GraphQL errors

### GraphQL Operations — query and mutation execution

- [x] Execute GraphQL queries via `_graphql(query, variables)` helper
- [x] Raise on response containing `"errors"` key
- [x] Return data dict on success
- [x] Define shared issue field selection fragment (`_ISSUE_FIELDS`) for consistent responses
- [x] All GraphQL operations use the same endpoint `https://api.linear.app/graphql`
- [x] Implement retry/backoff for transient HTTP errors (429, 502, 503, 504) with exponential backoff
- [x] Handle 304 Not Modified responses gracefully
- [ ] Support GraphQL query complexity limits and cost-based rate limiting
- [x] Support request cancellation via `asyncio` timeout (httpx client has timeout=30s configured)

### Issue Operations — read, update, and search

- [x] Get single issue by ID via `query("issue")` with `issue_id` filter
- [x] Return issue fields: id, title, description, state, priority, assignee, labels (`{id, name, color}`), createdAt, updatedAt
- [x] Search issues via `query("search")` with text `query` and optional `limit`
- [x] Default search limit to 100
- [x] Support cursor-based pagination via `cursor` parameter in `ConnectorQuery`
- [x] Return `next_cursor` from `ConnectorResult` for continuation
- [x] Parse `pageInfo.hasNextPage` and `pageInfo.endCursor` from GraphQL response
- [x] Create issue via `write("issue")` with `team_id`, `title`, optional `description`, `priority`, `assignee_id`, `label_ids`
- [x] Update issue fields via `write("issue_update")` with `issue_id` and fields
- [x] Raise `ValueError` for unsupported resources in `query()` and `write()`
- [x] Read issue comments via `query("issue_comments")` with `issueId` filter
- [x] Create issue comments via `write("issue_comment")` with `issueId` and `body`
- [x] Support cursor-based pagination for `query("search")` — `next_cursor` populated from `pageInfo`
- [x] **State transition by name** — `write("issue_state")` accepts `{"id", "state": "<name>", "teamId"}` and resolves the workflow state name to an ID via the team's states before applying `issueUpdate`; name resolution is exact-first (case/punctuation-insensitive) then fuzzy, and raises a clear error on duplicate/ambiguous names
- [x] **State transition by raw state ID** — `write("issue_state")` accepts `{"id", "stateId"}` to set a workflow state ID directly without resolution
- [x] **Label creation** — `write("label")` creates a label via `labelCreate` with `name`, `teamId`, optional `color`/`description`
- [x] **Label update/rename** — `write("label_update")` renames/recolors a label via `labelUpdate` with `id` and optional `name`/`color`/`description`
- [x] **Label deletion** — `write("label_delete")` deletes a label via `labelDelete` with `id`
- [x] **Cycle assignment by name** — `write("issue_cycle")` accepts `{"id", "cycle": "<name>", "teamId"}` and resolves the cycle name to an ID via the team's cycles before applying `issueUpdate`; name resolution is exact-first (case/punctuation-insensitive) then fuzzy, and raises a clear error on duplicate/ambiguous names
- [x] **Cycle assignment by raw cycle ID** — `write("issue_cycle")` accepts `{"id", "cycleId"}` to set the cycle ID directly
- [x] **Cycle removal** — `write("issue_cycle")` accepts `{"id", "cycleId": null}` to unassign an issue from its cycle
- [x] **Label assignment (add/remove)** — `write("issue_label")` accepts `{"id", "addLabelIds": [...]}` and/or `{"id", "removeLabelIds": [...]}` (at least one required); current label IDs are fetched first and the target set computed so it is a true add/remove, applied via a single `issueUpdate`
- [x] **Assign/reassign issue** — `write("issue_assign")` accepts `{"id", "assigneeId": "<id>"}` (direct), `{"id", "email": "..."}` or `{"id", "name": "..."}` (resolved via Linear user search, `ValueError` when no user), or `{"assigneeId": null}` / `{"unassign": true}` to clear the assignee; applied via a single `issueUpdate`
- [x] **Unassign issue** — `write("issue_unassign")` with `{"id"}` clears the assignee via a single `issueUpdate`
- [x] **Cycle read-back on issues** — `_ISSUE_FIELDS` returns `cycle { id name }` on issue reads and writes
- [x] **Archive issue** — `write("issue_archive")` with `{"id"}` (optional `{"trash": bool}`) via `issueArchive`, returns `{"id", "archived": True, "trash": ...}`
- [x] **Delete issue** — `write("issue_delete")` with `{"id"}` via `issueDelete`, returns `{"id", "deleted": True}`

### Team and Project Operations

- [x] List teams via `query("teams")` — returns id, name, key, description
- [x] List projects for a team via `query("team_projects")` with `teamId` filter
- [x] Get workflow states for a team via `query("team_states")` with `teamId` filter
- [x] List issue labels for a team via `query("team_labels")` with `teamId` filter
- [x] List active/upcoming cycles for a team via `query("team_cycles")` with `teamId` filter

### Capability Declaration

- [x] `ConnectorType.LINEAR` defined in `base.py` enum
- [x] `ConnectorType.LINEAR.capabilities` returns `{ISSUE_READ, ISSUE_WRITE, ISSUE_SEARCH}` in `base.py`
- [x] `LinearConnector.connector_type` returns `ConnectorType.LINEAR`

### Health Check — connectivity and credential validation

- [x] Validate API key by executing viewer query — fail on GraphQL errors
- [x] Return authenticated user name in `detail` on success
- [x] Return error detail from GraphQL `"errors"` response on failure
- [x] **Distinguish expired API keys vs network errors vs insufficient permissions** — `health_check()` reports an invalid/expired key (`code: invalid_token`, HTTP 401 or GraphQL `AUTHENTICATION_REQUIRED`), a valid key lacking permission (`code: forbidden`, HTTP 403 or GraphQL `FORBIDDEN`), rate-limit exhaustion (`code: rate_limited`), and transport failures (`code: network_timeout`/`network_connection`) as distinct details — no more generic "error" for every failure mode
- [ ] Per-operation permission check before mutation calls

### Structured Error Handling — typed exceptions

- [x] `LinearError(ValueError)` base class carries a machine-parseable `error_code` + originating `status_code`
- [x] Typed hierarchy: `LinearAPIError` (`api_error`), `LinearRateLimitError` (`rate_limited`), `LinearAuthError` (`invalid_token` for bad credentials / `forbidden` for missing permission), `LinearNotFoundError` (`not_found`), `LinearNetworkError` (`network_timeout` / `network_connection` / `network_protocol`)
- [x] `_error_for_status()` maps HTTP statuses → typed errors in one place (429 → rate_limited, 401 → invalid_token, 403 → forbidden, 404 → not_found, other non-retryable → api_error)
- [x] `_classify_graphql_error()` classifies Linear GraphQL `errors` bodies by `extensions.type` (`AUTHENTICATION_REQUIRED` → invalid_token, `FORBIDDEN` → forbidden, unknown → api_error) — malformed payloads never raise
- [x] All `ValueError`-compatible so `except ValueError` callers are unaffected

### Error Handling & Resilience

- [x] `health_check` catches generic `Exception` — returns `HealthResult(ok=False)` with truncated message (no redundant `HTTPStatusError` catch; `_graphql` wraps all HTTP errors as `ValueError`)
- [x] `_graphql` retries 429, 502, 503, 504 with exponential backoff (matching GitHub/Jira/Slack pattern)
- [x] `_graphql` respects `Retry-After` header for retry timing
- [x] `_graphql` retries `TimeoutException` with exponential backoff, raises `ValueError("Linear API timeout")` after exhaustion
- [x] `_graphql` retries `ConnectError` with exponential backoff, raises `ValueError("Linear API connection error")` after exhaustion
- [x] `_graphql` retries `ProtocolError` (e.g. server disconnect) with exponential backoff, raises `ValueError("Linear API protocol error")` after exhaustion
- [x] `_graphql` handles 304 Not Modified — raises `ValueError` with descriptive message
- [x] `_graphql` catches `httpx.HTTPStatusError` for non-retryable statuses — raises `ValueError` with status code and response text
- [x] `_graphql` catches `json.JSONDecodeError` — raises `ValueError` with parsing error detail
- [x] `_graphql` detects GraphQL `"errors"` key in response — raises `ValueError` with error details
- [x] `query("issue")` with missing `id` filter — raises `ValueError` with descriptive message
- [x] `write("issue_update")` with missing `id` — raises `ValueError` with descriptive message
- [x] `write("issue")` with `success: false` — raises `ValueError` with issue title
- [x] `write("issue_update")` with `success: false` — raises `ValueError` with issue id
- [x] `query()` with unsupported resource — raises `ValueError`
- [x] `write()` with unsupported resource — raises `ValueError`

### Prompt Portability — GraphQL query maintenance

- [ ] GraphQL queries are hard-coded in source — no query discovery
- [ ] Linear API schema changes (field deprecation, new fields) require source code update
- [ ] Prompt templates may use Linear-specific terminology ("issue", "team", "cycle")

## Known Gaps

- **Per-operation permission check before mutation calls** — write resources are not pre-verified against the key's declared permissions before a mutation is sent; Linear reports permission failures as `FORBIDDEN` GraphQL errors at execution time (now surfaced as `code: forbidden`).
- **GraphQL query-complexity limits and cost-based rate limiting** — the connector does not inspect Linear's query-complexity/point-budget reporting to avoid expensive queries before they run.
- **Prompt portability** — GraphQL queries are hard-coded in source (no query discovery), so Linear schema changes (field deprecation, new fields) require a source update; prompt templates may use Linear-specific terminology.

## QA History

### 2026-08-12 — improve-architecture: typed-error programme (invalid-key vs permission vs rate-limit vs network distinction)

**RESOLVED** the "Detect expired API keys vs network errors vs insufficient permissions" known gap (`connectors/linear/__init__.py`), mirroring the Slack/GitHub/GitLab typed-exception programme so Linear failures can be branched on programmatically instead of by parsing messages. (1) **Typed error hierarchy** — new `LinearError(ValueError)` base + `LinearAPIError`, `LinearRateLimitError`, `LinearAuthError` (`invalid_token` on HTTP 401 / GraphQL `AUTHENTICATION_REQUIRED`; `forbidden` on HTTP 403 / GraphQL `FORBIDDEN`), `LinearNotFoundError`, `LinearNetworkError` (`network_timeout`/`network_connection`/`network_protocol`); every typed error carries a stable machine-parseable `error_code` + originating `status_code`. (2) **`_graphql` raises the typed subclasses** — new `_error_for_status()` maps statuses → types in one place; the 304, invalid-JSON, non-object-data, and exhausted-429 paths now raise typed errors with codes (`not_modified`/`invalid_response`/`rate_limited`); timeouts/connection/protocol failures raise `LinearNetworkError`. (3) **GraphQL body classification** — new `_classify_graphql_error()` extracts `extensions.type` from the `errors` array (`AUTHENTICATION_REQUIRED` → `invalid_token`, `FORBIDDEN` → `forbidden`, malformed payloads fall back to `api_error` without raising). (4) **Health-check distinction** — `health_check()` now reports invalid/expired key, insufficient permission, rate-limit exhaustion, and network/transport failures as distinct details (e.g. `Linear authentication failed — invalid or expired API key (HTTP 401) (code: invalid_token)`, `Linear API rate limit exhausted (HTTP 429) (code: rate_limited)`). Added 23 unit tests in `test_linear_errors.py` (hierarchy + default `error_code` matrix, `_error_for_status` matrix, GraphQL classifier + malformed payload matrix, 401/403/404/500/exhausted-429/timeout/connect/protocol/invalid-JSON typed errors, GraphQL auth-classification, health-check invalid-key-http/invalid-key-graphql/forbidden/rate-limited/network-error/ok). Updated product map (`unit-tests:` + 6 behaviours `[ ]`→`[x]` incl. the health-distinction gap, Known Gaps rewritten). 114/114 linear unit tests pass (91 existing + 23 new), ruff check + format clean, mypy --strict clean. Status: partial (per-operation permission checks, GraphQL query-complexity limits, prompt portability remain).

### 2026-08-03 — improve-architecture: 2 known gaps RESOLVED (dedicated assign/unassign + fuzzy/duplicate-name disambiguation)
- **RESOLVED** "Assign/unassign issue" — added `write("issue_assign")` (accepts `{"id", "assigneeId": "<id>"}` direct, `{"id", "email": "..."}` or `{"id", "name": "..."}` resolved via a new Linear `users(first: 1, filter: ...)` GraphQL query with `ValueError` on no-match, or `{"assigneeId": null}` / `{"unassign": true}` to clear) and `write("issue_unassign")` (`{"id"}`), both applied via a single `issueUpdate` and returning the updated issue.
- **RESOLVED** "State/cycle name resolution is case-insensitive exact-match only" — added `_normalize_name()` (case/punctuation-insensitive) and `_fuzzy_matches()` (token-containment) helpers plus a shared `_resolve_entity_by_name()`: exact (normalised) match first, fuzzy fallback second, clear `ValueError` when two or more entities match the same name (duplicate exact names or an ambiguous fuzzy prefix) so callers pass the raw ID. State lookup now paginates through all pages via new `_STATE_LOOKUP_QUERY` + `_team_states()`/`_team_named_entities()` (mirroring the existing paginated cycle lookup), so a name on page 2 can no longer be missed.
- Added 14 unit tests (`test_linear.py`: assign by id/email/name/unassign-flag/null-assignee, assign missing id + missing user reference + user-not-found + update failure, unassign + missing id, fuzzy state "progress"→"In Progress", punctuation-insensitive "in-progress", duplicate exact state names raise, ambiguous fuzzy cycle name raises, exact-beats-fuzzy) + 5 BDD scenarios in `linear_connector.feature` (assign by assignee id / by email / by name, unassign, missing-user-reference error) with 5 new step definitions in `test_connectors.py` and the mock connector extended to mirror the new resources.
- Updated product map (2 behaviours `[ ]`→`[x]`, both Known Gaps → RESOLVED, QA History). 91/91 linear unit tests + 28/28 linear BDD scenarios pass (pre-existing 18 connector-suite BDD failures unchanged), ruff clean. Status: partial (only cross-cutting gaps remain — expired-key vs network-error distinction, per-operation permission checks, GraphQL query-complexity limits).

### 2026-08-03 — improve-architecture: 3 known gaps RESOLVED (issue_label add/remove, cycle read-back, issue archive/delete)

**RESOLVED known gaps** "No label assignment on issues", "No cycle/sprint read-back on issues", "No issue archive/delete". Added to `connectors/linear/__init__.py`:
- `write("issue_label")` — add/remove labels on an issue via `{"id", "addLabelIds": [...]}` and/or `{"id", "removeLabelIds": [...]}`. Because Linear's `issueUpdate.labelIds` is a *set* (replaces the full list), the current label IDs are fetched first via a new `_ISSUE_LABEL_IDS_QUERY` and the target set computed in `_update_issue_labels()` so the operation is a true add/remove applied atomically in a single `issueUpdate` (not a blind replace). Missing issue → `ValueError`, missing both add/remove → `ValueError`.
- **Cycle read-back** — `cycle { id name }` added to the shared `_ISSUE_FIELDS` fragment, so every issue read/search/write now returns the issue's current cycle.
- `write("issue_archive")` — archives an issue via `issueArchive(id, trash)`, optional `{"trash": bool}`, returns `{"id", "archived", "trash"}`; success-flag check.
- `write("issue_delete")` — permanently deletes an issue via `issueDelete(id)`, returns `{"id", "deleted"}`; success-flag check.

Added 14 unit tests (`test_linear.py`: issue cycle read-back, issue_label add/remove/add+remove/missing-fields/issue-not-found/update-failure, issue_archive default/trash/missing-id/failure, issue_delete/missing-id/failure) + 6 BDD scenarios in `linear_connector.feature` (add label, remove label, no-label-ids error, archive, archive-to-trash, delete) with 7 new step definitions in `test_connectors.py` and the mock connector extended to mirror the new resources. Updated product map (5 behaviours `[ ]`→`[x]`, Known Gaps 4→2, BDD count 18→24, QA History). 78/78 `test_linear.py` + `test_linear_resilience.py` unit tests + 6/6 new linear BDD scenarios pass (pre-existing 19 connector-suite BDD failures unchanged), ruff clean. Status: partial (issue assign/unassign helper + fuzzy state/cycle name matching remain).

### 2026-08-03 — improve-architecture: review-fix hardening (PR #565)

Addressed reviewer nits on the connector feature-gap delivery: renamed the cycle lookup query to `_CYCLE_LOOKUP_QUERY` and made `_resolve_cycle_id()` paginate through all cycles (a team with >100 cycles can no longer miss a name match); hardened `_resolve_state_id()`/`_resolve_cycle_id()` to raise a clean `ValueError` when a nonexistent `teamId` returns a null `team`; and whitelisted `LabelCreateInput` keys in `write("label")` instead of passing the raw payload. Added 4 unit tests (label input whitelisting, state/cycle team-not-found, cross-page cycle name match). 56/56 `test_linear.py` unit tests pass, ruff clean.

### 2026-08-03 — improve-architecture: 3 known gaps RESOLVED (state transition by name, label management, cycle assignment)

**RESOLVED known gaps** "State transitions require raw stateId", "No label management", "No cycle/sprint assignment". Added 3 new write resources to `connectors/linear/__init__.py`:
- `write("issue_state")` — transitions an issue's workflow state. Accepts either `{"id", "stateId"}` (raw ID) or `{"id", "state": "<name>", "teamId"}` which resolves the state name to an ID via the team's states (`_resolve_state_id()`, case-insensitive, error if not found). Applies via the shared `_update_issue()` (`issueUpdate`).
- `write("issue_cycle")` — assigns/unassigns an issue's cycle. Accepts `{"id", "cycleId"}` (direct, including `null` to remove) or `{"id", "cycle": "<name>", "teamId"}` which resolves the cycle name to an ID via the team's cycles (`_resolve_cycle_id()`, case-insensitive).
- `write("label")` / `write("label_update")` / `write("label_delete")` — full label CRUD via `labelCreate`/`labelUpdate`/`labelDelete` GraphQL mutations, with required-field validation and success-flag checks.

Added 15 unit tests (`test_linear.py`: issue_state by name/by ID/missing-fields/not-found, issue_cycle by name/by ID/remove/missing-fields, label create/update/delete + missing-field + failure paths) and 10 BDD scenarios in `linear_connector.feature` (state by name + raw ID + missing team error, cycle assign by name + raw ID + remove, label create/rename/delete) with 10 new step definitions in `test_connectors.py` and the mock connector extended to mirror the new resources. Updated product map (9 behaviours `[ ]`→`[x]`, 3 Known Gaps → RESOLVED + 4 new documented, QA History). 52/52 `test_linear.py` unit tests + 10/10 new linear BDD scenarios pass (pre-existing 19 connector-suite BDD failures unchanged), ruff clean. Status: partial (issue archive/delete, label assignment helper, cycle read-back, fuzzy name matching remain).

### 2026-07-07: Cross-cutting QA (index 318). Added `labels` field to `_ISSUE_FIELDS` GraphQL fragment. Added `httpx.ProtocolError` retry/backoff handling to `_graphql` (previously unhandled — raised raw exception). Added 2 new resilience tests (protocol error failure + protocol error retry-then-success). Updated test mocks to verify label data in issue responses. Fixed stale checkbox in connector-hub.md (BDD scenarios exist since July 1). Status: partial.
- 2026-07-05: Cross-cutting QA (improve-architecture): Added retry/backoff to `_graphql` (429/502/503/504 + TimeoutException + ConnectError) matching GitHub/Jira/Slack patterns. Simplified `health_check` (removed dead `httpx.HTTPStatusError` catch). Added cursor-based pagination for `query("search")`. Added comment operations (`issue_comments` read, `issue_comment` write). Added team/project discovery (teams, projects, states, labels, cycles). Added 15 new unit tests covering all new code paths (37 total). Updated product map with new sections. Status: partial (cycle assignment, label management, state helper still gaps).
- 2026-07-03: Cross-cutting QA (index 110): Fixed HTTP/JSON error handling in `_graphql` (wraps HTTPStatusError, TimeoutException, ConnectError, JSONDecodeError as ValueError). Added 5 resilience unit tests (test_linear_resilience.py). Fixed stale checkbox: timeout confirmed configured (30s). Fixed search default limit (50→100). Added Error Handling section (12 behaviour checkboxes). Status: partial (known gaps unchanged).
- 2026-07-01: Cross-cutting QA: fixed frontmatter (added unit-tests), removed outdated known gaps #7 (BDD placeholder → 5 real scenarios) and #8 (unit tests exist), added 3 BDD error-path scenarios + step definitions, added 4 unit tests (missing id, update failure, GraphQL error), fixed search to respect `q.limit` via `first:$limit`, consolidated gaps from 9→7
