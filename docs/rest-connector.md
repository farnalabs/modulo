# REST Connector

The generic REST connector (`ConnectorType.REST`) lets a Modulo pipeline make
an arbitrary HTTP request to an external system configured by the operator,
with no per-vendor client. It is the FAR-401 "point Modulo at any external
system" implementation: a declarative, templated HTTP call.

Because it is **verb-agnostic**, the connector does not infer meaning from the
HTTP verb. You declare the method; the node surface fixes the access-control
gate:

| Node surface | ACL (access control) operation | Declared method |
|---|---|---|
| `query()` – read | `read` | `GET` (default), `HEAD` |
| `write()` – write | `write` | `POST` (default), `PUT`, `PATCH`, `DELETE` |

`OPTIONS` is also accepted when explicitly declared on either surface; the
surface (not the verb) still fixes the ACL gate, and the per-surface
default (`GET` / `POST`) applies when no method is declared.

A `PUT` / `PATCH` / `DELETE` mutates the remote system, so it belongs on the
**write** surface even though it is not a "create". The capability set is
`{read, write}`.

## Configuration (`config_json`)

A single connector instance describes one endpoint (or a map of named
resources, each with its own endpoint). All template fields are rendered with
Jinja2 against the runtime variables supplied per call
(`ConnectorQuery.filters` for `query()`, `ConnectorPayload.data` for
`write()`).

| Field | Type | Default | Description |
|---|---|---|---|
| `base_url` | `str` | – (required) | Scheme + host of the endpoint. `http://`/`https://` only. |
| `method` | `str` | `GET` (query) / `POST` (write) | HTTP verb. One of `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`. |
| `path` | `str` | – (required) | Path template appended to `base_url` (e.g. `/v1/users/{{ user_id }}`). |
| `headers` | `dict` | `{}` | Header templates. May not override auth/transport headers (`authorization`, `host`, `content-length`, …). |
| `params` | `dict` | `{}` | Query parameter templates (URL-encoded by httpx). |
| `body` | `dict` | `{}` | JSON body template (write path). |
| `operations` | `dict` | `{}` | Per-resource override map: `{ "<resource>": { "method", "path", "headers", "params", "body", "records_path", "next_cursor_path", "passthrough", "idempotency_header" } }`. |
| `records_path` | `str` | `null` | JMESPath expression into the JSON response for records (e.g. `data.items`). |
| `next_cursor_path` | `str` | `null` | Optional pagination cursor (JMESPath). |
| `allowed_hosts` | `list[str]` | `[]` | Optional scheme/host allowlist. |
| `passthrough` | `bool` | `false` | Force a single-record `{"body", "content_type", "status_code", "headers"}` wrap of the raw body. |
| `max_response_size` | `int` | `10485760` | Max response body bytes before the read aborts (10 MiB default). |
| `timeout_seconds` | `float` | `30.0` | Per-request timeout (connect + read/write) for the pooled client. |
| `verify_tls` | `bool` | `true` | Whether the client verifies the server certificate. Disable only for a self-hosted registry with a self-signed cert – the SSRF (Server-Side Request Forgery) guard still blocks loopback/metadata targets regardless. |
| `idempotency_header` | `str` | `null` | Header that makes a non-`GET`/`HEAD` request safe to retry; a fresh UUID is injected per attempt. |
| `on_unknown` | `str` | `fail_open` | Per-op idempotency gate mode for the UNKNOWN (couldn't-confirm-delivery) case (FAR-458): `fail_open` (re-fire on ambiguity, possible duplicate), `fail_closed` (suppress on ambiguity, possible silent miss), or `off` (never deduplicated, write always fires). A confirmed-delivered write is suppressed in every mode except `off`. |
| `fan_out` | `dict` | `null` | Fan-out / iterator mode (FAR-411). When `enabled` is true and `items_path` resolves to a sequence, `write()` fans out one request per item. Sub-fields: `enabled` (bool), `items_path` (JMESPath into `payload.data`), `max_cardinality` (fail-closed cap, default 1000), `per_item_timeout` (per-item HTTP timeout), `max_retries` (per-item retries). |
| `rate_limit` | `dict` | `null` | Per-destination token bucket (FAR-411). Sub-fields: `requests_per_second` (refill rate), `burst` (burst capacity). Shared across fleet workers when Redis is configured; per-process when Redis is absent. |

You can also declare **operations** – a map of named resources, each with its
own method/path/headers/params/body/records_path. When present, a node must
reference a declared resource; requesting an undeclared resource fails fast.

## Authentication (`credentials_ciphertext` / `creds`)

Credentials are stored as an encrypted JSON dict so multi-field creds
round-trip. Read `auth_mode` + named fields from that dict:

| `auth_mode` | Required fields |
|---|---|
| `bearer` | `token` |
| `api_key` | `api_key`, and `in` (`header` [default] or `query`), `header_name` (default `X-API-Key`) for header mode, `query_param_name` (default `api_key`) for query mode |
| `basic` | `username`, `password` |

Credentials are never written to LangGraph state, checkpoint, OTel span, or log;
error detail redacts secret values.

## Behaviour

### Verb-agnostic read/write mapping

The capability contract (`read` / `write`) maps onto the two public surfaces,
not onto any single verb:

- `query()` is the **read** surface (ACL `read`). It performs the operation's
  verb (default `GET`).
- `write()` is the **write** surface (ACL `write`). It performs the operation's
  verb (default `POST`). A `PUT`/`DELETE`/`PATCH` mutates the remote, so it
  belongs on the **write** surface. The connector never infers semantics from
  the verb.

### Transport

A single lazily-created, connection-pooled `httpx.AsyncClient` is reused across
calls and closed via `close()`. The client never follows redirects, so HTTP 3xx
responses surface as errors (with `location`/`Retry-After` metadata) rather
than silently passing through.

### Retry

Idempotent verbs (`GET`/`HEAD`) are retried up to 2 times (3 total attempts)
with exponential backoff + jitter, honouring `Retry-After` and the retryable
status set (`429`, `500`, `502`, `503`, `504` — a narrow set, not every `5xx`;
other server errors fail immediately). Mutating verbs are retried only when the
operation declares an `idempotency_header`. Transport failures are retried for
idempotent verbs and surface as a typed `RESTConnectError`. The retry sleep
uses the injected `sleep` seam (defaulting to `asyncio.sleep`), so timing is
deterministic under test; jitter comes from a separate injected `random_uniform`
seam, and the injected `clock` seam is used only for latency instrumentation.

### Security guards

- **SSRF**: the target URL is validated twice, in two distinct places. The
  connector itself checks the `allowed_hosts` allowlist first
  (`connectors/rest/__init__.py`, `_validate_target_url`), then delegates to
  the injected SSRF guard, which is bound at the composition root and resolves
  DNS before blocking private/loopback/link-local/cloud-metadata/CGNAT ranges.
  Private and loopback ranges can be re-enabled for a self-hosted deployment
  via the `SSRF_ALLOW_PRIVATE_RANGES` CIDR allowlist, but the link-local,
  metadata, benchmarking and CGNAT blocks are a non-negotiable floor. Disabling
  `verify_tls` never re-enables those blocked targets.
- **Header injection**: rendered header names/values may not contain CR/LF or
  control characters, and may not override auth/transport headers.
- **Redaction**: credential values are stripped from error detail so a secret
  never echoes into logs or spans.
