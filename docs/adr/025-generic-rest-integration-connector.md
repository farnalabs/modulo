# ADR 025 — Generic REST Integration Connector

**Date**: 2026-08-25
**Status**: Accepted

---

## Context

Modulo's connector surface shipped as a family of vendor-specific connectors
(GitHub, Jira, Linear, Slack, Datadog, …). Every new integration meant a new
client, new auth handling, and new unit fixtures — all specialised for a single
vendor's API. The FAR-401 "point Modulo at any external system" goal needs a way
to bind the product to a system that has no first-party connector.

`RestConnector` (FAR-408) implements the **generic** surface: a declarative,
verb-agnostic HTTP connector. A pipeline can declare an arbitrary endpoint
(base_url, method, path, headers, params, body, records extraction) and a node
calls it with runtime variables rendered into the request. This ADR records the
design decisions and the deliberate deferrals that keep the connector in scope
for v1.

## Decision

### A declarative, verb-agnostic connector

A single connector instance describes one endpoint (or a map of named resources).
All template fields render through the same sandboxed Jinja environment the node
runner uses, so no new templating machinery is introduced. The connector adds
`httpx` (transport), `jmespath` (records extraction) and `jinja2` (templating)
only.

### Verbatim read/write mapping

The connector does **not** infer semantics from the HTTP verb. The two surfaces
fix the ACL gate:

| Surface | ACL operation | Declared verb |
|---|---|---|
| `query()` | `read` | `GET` (default), `HEAD` |
| `write()` | `write` | `POST` (default), `PUT`, `PATCH`, `DELETE` |

`PUT`/`PATCH`/`DELETE` mutate the remote system, so they belong on the write
surface regardless of whether they are a "create". Capabilities are `{read,
write}`.

### REST-specific conformance shape

`query()` returns a `ConnectorResult` whose `records` is always a **list of
JSON-serializable dicts**, so the downstream JMESPath consumer and the JSON API
formatter stay contract-stable. Raw / passthrough content-types (CSV, XML, plain
text) wrap the raw body in a single
`{body, content_type, status_code, headers}` record when **no `records_path` is
configured** (or when `passthrough` is set). When a `records_path` IS configured
against a non-JSON body, extraction cannot run and the connector returns an empty
records list. This is the REST-specific conformance shape: the connector never
returns a bare string or a list of scalars that would break the downstream
evaluator.

### Layered guards

SSRF / output-injection guards live in `modulo.core` and are injected at the
composition root (`connector_hub`) via a `SecurityGuard` port — the connector
does not reach into `modulo.core` directly. The connector additionally:
- rejects header control characters (injection),
- forbids rendered headers from overriding auth/transport headers,
- enforces a scheme/host allowlist (`allowed_hosts`) + SSRF validation,
- caps the response body at `max_response_size`,
- does not follow redirects (HTTP 3xx is a typed error with location/Retry-After
  metadata),
- retries idempotent verbs (`GET`/`HEAD`) with backoff, and mutating verbs only
  when `idempotency_header` is declared.

## Consequences

### Positive

- No per-vendor client for a generic endpoint — one template covers a long tail.
- The capability contract is preserved: read vs write maps onto `query`/`write`.
- Conformance stays uniform (list-of-dicts) even for non-JSON bodies, so
  downstream nodes and the JSON API behave predictably.
- Existing SSRF/injection machinery is reused via a single port rather than
  duplicated inside the connector.

### Negative

- The operator must declare the endpoint (records_path, records shape, auth) —
  a generic connector cannot infer them.
- The connector introduces a small dependency set (`httpx`, `jmespath`).
- Raw-body passthrough is opaque: XML/CSV are not parsed, so a node wanting
  structured fields must parse further downstream.

## Deferrals

- **Request→response ingestion classification** — to FAR-404.
- **DISTINCT classification layer** — a separate deduplicate/classify layer is
  deferred to FAR-404 alongside the ingestion classification;
  request→response ingestion runs as one opaque hop today.
- **Per-tenant weighted concurrency semaphore** — a per-tenant (weighted,
  per-credential) concurrency semaphore is deferred; today the connector shares
  a single connection-pooled client and far-out fan-out relies on the pipeline's
  global concurrency limits.
- **Fan-out cardinality guard** — a connector-level guard on the number of
  parallel requests a single fan-out may open is deferred (see the pipeline
  concurrency references in the loop/parallel node docs); rely on pipeline-level
  concurrency limits.
- **OAuth 2.0** — the connector supports bearer / api_key (header+query) / basic
  only; registry/refresh flows are deferred.
- **Async mode** — response-driven polling and async webhook-style triggers are
  deferred; the connector is synchronous request/response.
- **Pagination** — read is response-driven via `next_cursor_path`, but the
  connector does not loop cursors itself. A direct start cursor is rejected with
  an actionable error. Out of v1.

## References

- FAR-401: "point Modulo at any external system" design.
- FAR-408: generic REST connector core.
- ADR 010: Integration Tier Classification (Native / Preview / In-Dev).
- `docs/rest-connector.md`: user-facing config, auth, egress-allowlist and
  UNKNOWN/determinism guidance.
