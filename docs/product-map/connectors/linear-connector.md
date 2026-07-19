---
id: feat-connectors-linear
prd: 8.6
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/connector_health.feature
  - backend/tests/bdd/features/connectors/linear_connector.feature
unit-tests:
  - backend/tests/unit/connectors/test_linear.py
code:
  - backend/src/modulo/connectors/linear/__init__.py
  - backend/src/modulo/connectors/base.py
depends-on:
  - feat-connectors-hub
status: partial
---

# Linear Connector

Async Linear GraphQL API connector implementing `ConnectorBase`. BDD coverage: 8 scenarios (5 happy-path + 3 error-path) with step definitions in `backend/tests/bdd/features/connectors/linear_connector.feature` and `backend/tests/bdd/steps/test_connectors.py`. Provides read/write access to Linear issues for agent pipelines. Authenticated via Linear API key. Belongs to the `issue-tracker` connector type family alongside `JiraConnector`.

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
- [ ] Add/remove issue labels — only available via full `issue_update`
- [ ] Change issue state/status — only available via `issue_update` with `stateId`
- [ ] Assign/unassign issue — only available via `issue_update`
- [ ] Archive issue — not implemented
- [ ] Delete issue — not implemented

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
- [ ] Detect expired API keys vs network errors vs insufficient permissions
- [ ] Per-operation permission check before mutation calls

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

- [ ] **State transitions require raw stateId**: no helper to map workflow state names to IDs
- [ ] **No label management**: cannot create, rename, or delete labels
- [ ] **No cycle/sprint assignment**: can read cycles but cannot assign an issue to a cycle

## QA History
- 2026-07-07: Cross-cutting QA (index 318). Added `labels` field to `_ISSUE_FIELDS` GraphQL fragment. Added `httpx.ProtocolError` retry/backoff handling to `_graphql` (previously unhandled — raised raw exception). Added 2 new resilience tests (protocol error failure + protocol error retry-then-success). Updated test mocks to verify label data in issue responses. Fixed stale checkbox in connector-hub.md (BDD scenarios exist since July 1). Status: partial.
- 2026-07-05: Cross-cutting QA (improve-architecture): Added retry/backoff to `_graphql` (429/502/503/504 + TimeoutException + ConnectError) matching GitHub/Jira/Slack patterns. Simplified `health_check` (removed dead `httpx.HTTPStatusError` catch). Added cursor-based pagination for `query("search")`. Added comment operations (`issue_comments` read, `issue_comment` write). Added team/project discovery (teams, projects, states, labels, cycles). Added 15 new unit tests covering all new code paths (37 total). Updated product map with new sections. Status: partial (cycle assignment, label management, state helper still gaps).
- 2026-07-03: Cross-cutting QA (index 110): Fixed HTTP/JSON error handling in `_graphql` (wraps HTTPStatusError, TimeoutException, ConnectError, JSONDecodeError as ValueError). Added 5 resilience unit tests (test_linear_resilience.py). Fixed stale checkbox: timeout confirmed configured (30s). Fixed search default limit (50→100). Added Error Handling section (12 behaviour checkboxes). Status: partial (known gaps unchanged).
- 2026-07-01: Cross-cutting QA: fixed frontmatter (added unit-tests), removed outdated known gaps #7 (BDD placeholder → 5 real scenarios) and #8 (unit tests exist), added 3 BDD error-path scenarios + step definitions, added 4 unit tests (missing id, update failure, GraphQL error), fixed search to respect `q.limit` via `first:$limit`, consolidated gaps from 9→7

