---
id: feat-connectors
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/connectors.py
  - backend/src/modulo/core/connector_hub
  - backend/src/modulo/connectors/rest
  - frontend/src/views/AdminConnectorsView.vue
unit-tests:
  - backend/tests/unit/api/test_connectors_endpoint.py
  - backend/tests/unit/connectors/test_connector_base_seam.py
  - backend/tests/unit/connectors/test_connector_credential_redaction.py
  - backend/tests/unit/connectors/test_connector_egress_gate.py
  - backend/tests/unit/connector_hub/test_connector_hub.py
bdd:
  - backend/tests/bdd/features/connectors/connector_health.feature
  - backend/tests/bdd/features/connectors/github_connector.feature
  - backend/tests/bdd/features/connectors/jira_connector.feature
  - backend/tests/bdd/features/connectors/slack_connector.feature
  - backend/tests/bdd/features/connectors/schema_inference.feature
depends-on:
  - feat-model-backends
status: covered
---

# Connectors

External tool connectors providing a unified interface for querying and writing to
third-party services (GitHub, Jira, Slack, Linear, and 30+ others). Supports
REST integration with declarative endpoints, auth credential profiles, fan-out,
and per-destination rate limiting.

## Behaviours

- [x] A REST connector is creatable with a declarative endpoint (base_url + path
      + method) and auth credential profile (bearer / api_key / basic);
      credentials are Fernet-encrypted at rest and never exposed in responses
      (`backend/src/modulo/api/routes/connectors.py`,
      `backend/tests/unit/api/test_connectors_endpoint.py`)
- [x] query() is the read surface and write() is the write surface; mutating
      verbs retry only when an idempotency_header is declared
      (`backend/src/modulo/connectors/rest`)
- [x] Non-2xx/3xx responses surface typed RESTStatusError with status_code,
      location, and Retry-After metadata; response body capped at max_response_size
      (`backend/src/modulo/connectors/rest`)
- [x] Per-destination rate limiting (Redis-atomic when available, per-process
      fallback) enforces one budget per tenant:destination; shared limiter fails
      closed on Redis outage (`connectors/rest`)
- [x] Fan-out emits items sequentially with redacted outcome records; cardinality
      exceeds max_cardinality fails CLOSED before any request
- [x] Health check issues the configured request and reports OK only for sub-400
      status (`backend/tests/bdd/features/connectors/connector_health.feature`)
- [x] AdminConnectorsView renders schema-driven structured forms for REST
      connector config (base_url, method, credentials, advanced JSON)
      (`frontend/src/views/AdminConnectorsView.vue`)
- [x] ConnectorHub registers and manages 30+ native connector types with
      per-connector BDD features (`backend/src/modulo/connector_hub`,
      `backend/tests/unit/connector_hub/`)

## Known Gaps

- Per-item fan-out outcome trace spans are deferred to FAR-404 (operation-level
  OTel spans shipped in v1).
- No BDD for connector CRUD lifecycle (create/update/delete via admin API);
  coverage is via unit tests.

## QA History

- 2026-09-10: **improve-architecture (product-map walk)** — registered the
  `RestConnectorConfigForm.vue` static testids (`rest-connector-base-url`,
  `rest-connector-method`, `rest-connector-timeout`, `rest-connector-verify-tls`,
  `rest-connector-on-unknown`, `rest-connector-records-path`,
  `rest-connector-allowed-hosts`, `rest-connector-legacy-auth-hint`,
  `rest-connector-auth-mode`, `rest-connector-token`, `rest-connector-username`,
  `rest-connector-password`, `rest-connector-api-key`, `rest-connector-api-key-in`,
  `rest-connector-header-name`, `rest-connector-query-param`,
  `rest-connector-advanced-json`) in the `/admin/connectors` manifest `elements:`
  inventory and added `AdminConnectorsView.vue` to the reverse testid-coverage
  guard (`test_mapped_route_elements_cover_owning_view_testids`) — the structured
  Generic REST connector form shipped on the page was previously invisible to
  Remy's docs indexer / `/api/v1/manifest`.
- 2026-09-07: **improve-architecture (product-map walk)** — added this
  behaviour-tracker for `feat-connectors`, which previously had behaviours only
  in `manifest.yaml` inline. Behaviours verified against `routes/connectors.py`,
  `connector_hub/`, `connectors/rest/`, and the connector unit+BDD suites.
  Status: covered.
