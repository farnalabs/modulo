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
- [ ] Support GraphQL query complexity limits and cost-based rate limiting
- [x] Support request cancellation via `asyncio` timeout (httpx client has timeout=30s configured)

### Issue Operations — read, update, and search

- [x] Get single issue by ID via `query("issue")` with `issue_id` filter
- [x] Return issue fields: id, title, description, state, priority, assignee, labels, createdAt, updatedAt
- [x] Search issues via `query("search")` with text `query` and optional `limit`
- [x] Default search limit to 100
- [x] Create issue via `write("issue")` with `team_id`, `title`, optional `description`, `priority`, `assignee_id`, `label_ids`
- [x] Update issue fields via `write("issue_update")` with `issue_id` and fields
- [x] Raise `ValueError` for unsupported resources in `query()` and `write()`
- [ ] Comment on issue — not implemented
- [ ] Add/remove issue labels — only available via full `issue_update`
- [ ] Change issue state/status — only available via `issue_update` with `stateId`
- [ ] Assign/unassign issue — only available via `issue_update`
- [ ] Archive issue — not implemented
- [ ] Delete issue — not implemented
- [ ] Search does not support pagination cursor — `next_cursor` always `None`

### Team and Project Operations

- [ ] List teams — not implemented
- [ ] List projects — not implemented
- [ ] Get team metadata (states, workflows) — not implemented
- [ ] List issue labels for a team — not implemented

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

### Error Handling

- [x] `health_check` catches `httpx.HTTPStatusError` — returns `HealthResult(ok=False)` with status code and response text
- [x] `health_check` catches generic `Exception` — returns `HealthResult(ok=False)` with truncated message
- [x] `_graphql` catches `httpx.HTTPStatusError` — raises `ValueError` with status code and response text
- [x] `_graphql` catches `httpx.TimeoutException` and `httpx.ConnectError` — raises `ValueError` with connection error detail
- [x] `_graphql` catches JSON decode errors — raises `ValueError` with parsing error detail
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

- [ ] **No comment operations**: cannot read or write issue comments
- [ ] **No team/project enumeration**: agents cannot discover teams, projects, or available workflows at runtime
- [ ] **State transitions require raw stateId**: no helper to map workflow state names to IDs
- [ ] **No label management**: cannot create, rename, or delete labels
- [ ] **No cycle/sprint awareness**: cannot read or set issue cycle assignment
- [ ] **No pagination**: `query("search")` results are limited by default with no cursor-based continuation
- [ ] **No rate-limit handling**: no GraphQL query cost measurement, no 429 handling

## QA History
- 2026-07-03: Cross-cutting QA (index 110): Fixed HTTP/JSON error handling in `_graphql` (wraps HTTPStatusError, TimeoutException, ConnectError, JSONDecodeError as ValueError). Added 5 resilience unit tests (test_linear_resilience.py). Fixed stale checkbox: timeout confirmed configured (30s). Fixed search default limit (50→100). Added Error Handling section (12 behaviour checkboxes). Status: partial (known gaps unchanged).
- 2026-07-01: Cross-cutting QA: fixed frontmatter (added unit-tests), removed outdated known gaps #7 (BDD placeholder → 5 real scenarios) and #8 (unit tests exist), added 3 BDD error-path scenarios + step definitions, added 4 unit tests (missing id, update failure, GraphQL error), fixed search to respect `q.limit` via `first:$limit`, consolidated gaps from 9→7

