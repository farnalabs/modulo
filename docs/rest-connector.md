# Generic REST Connector

The generic REST connector (`ConnectorType.REST`, displayed as **rest** in the
connector list) lets you point Modulo at an arbitrary HTTP endpoint without
writing a vendor-specific client. You declare one endpoint (or a map of named
resources) and pipeline nodes call it with runtime variables rendered into the
request.

Because it is **verb-agnostic**, the connector does not infer meaning from the
HTTP verb. You declare the method; the node surface fixes the access-control
gate:

| Node surface | ACL operation | Declared method |
|---|---|---|
| `query()` — read | `read` | `GET` (default), `HEAD` |
| `write()` — write | `write` | `POST` (default), `PUT`, `PATCH`, `DELETE` |

A `PUT` / `PATCH` / `DELETE` mutates the remote system, so it belongs on the
**write** surface even though it is not a "create". The capability set is
`{read, write}`.

## Config shape (`config_json`)

All template fields render through a sandboxed Jinja environment against the
runtime variables supplied per call (`ConnectorQuery.filters` for `query()`,
`ConnectorPayload.data` for `write()`).

| Key | Type | Required | Meaning |
|---|---|---|---|
| `base_url` | string | yes | Scheme + host, e.g. `https://api.example.com` |
| `method` | string | no | Default verb (`GET` on read, `POST` on write) |
| `path` | string | yes (or per resource) | URL path/pattern, e.g. `/v1/users/{{ user_id }}` |
| `headers` | map | no | Header templates |
| `params` | map | no | Query params (URL-encoded by httpx) |
| `body` | map | no | JSON body template (write path) |
| `records_path` | string | no | JMESPath expression into the JSON response for records |
| `next_cursor_path` | string | no | JMESPath cursor for response-driven pagination (see below) |
| `passthrough` | bool | no | Force a single raw-body record wrap |
| `max_response_size` | int | no | Response body cap (default 10 MiB) |
| `idempotency_header` | string | no | Header name that makes a non-GET/HEAD request safe to retry |
| `allowed_hosts` | list | no | Scheme/host allowlist |

You can also declare **operations** — a map of named resources, each with its
own method/path/headers/params/body/records_path. When present, a node must
reference a declared resource; requesting an undeclared resource fails fast.

## Auth config (`credentials`)

Credentials are stored as an encrypted JSON dict, so multi-field auth round-trips.
`auth_mode` is one of `bearer`, `api_key`, or `basic`.

| mode | fields |
|---|---|
| `bearer` | `token` |
| `api_key` | `api_key` + `in` (`header` default / `query`) + `header_name` (default `X-API-Key`) or `query_param_name` (default `api_key`) |
| `basic` | `username`, `password` |

Credentials are never written to LangGraph state, checkpoint, OTel span, or log;
error detail redacts secret values. A query-mode `api_key` is injected after the
injection guard, so a secret containing characters a filter would reject still
round-trips.

## Egress allowlist (admin guide)

To keep the connector from issuing requests to arbitrary internet targets,
set `allowed_hosts` in `config_json`:

```json
{ "allowed_hosts": ["api.example.com", "internal.example.com"] }
```

The host must equal an entry or be a subdomain of an entry (`us.api.example.com`
matches `api.example.com`). The scheme is restricted to `http`/`https` and an
SSRF guard blocks private/loopback/metadata targets. If `allowed_hosts` is
unset, the connector still enforces the scheme restriction and SSRF guard — it
just accepts any public host.

## Result shape and UNKNOWN semantics

`query()` always returns a `ConnectorResult` whose `records` is a **list of
JSON-serializable dicts**, so a pipeline node can evaluate a JMESPath expression
against it:

- A JSON response with a configured `records_path` yields the list found there.
- A top-level JSON array is treated as the record list.
- A JSON object with no `records_path` is wrapped as a single record.
- **Raw / passthrough** content-types (CSV, XML, plain text) yield a single
  uniform record — `{body, content_type, status_code, headers}` — when **no
  `records_path` is configured** (or when `passthrough` is set). The connector
  does **not** parse XML/CSV itself — the record body is the raw string, which a
  downstream node (or Remy) can interpret. When a `records_path` IS configured
  against a non-JSON body, extraction cannot run, so the connector returns an
  empty records list rather than fabricating a record. This keeps the shape
  contract stable: the result is **never** a bare string that would break the
  JMESPath consumer.

A non-2xx/3xx response raises a typed error rather than silently passing a
redirect through — so a downstream node sees a failure, not an empty or
misleading result. That is the **UNKNOWN** you surface on failure: the connector
reports `HTTP <status>` plus `location`/`Retry-After` metadata, and the record
list is not fabricated.

## Determinism and ordering caveats

- **Pagination is response-driven and out of v1.** The connector reads a
  `next_cursor_path` in an **already-fetched** response body and returns it as
  `next_cursor`. It does **not** follow cursors or loop pages on its own — the
  pipeline node drives the loop by feeding the cursor back. A direct start
  cursor (`ConnectorQuery.cursor`) is rejected with an actionable error because
  REST pagination is not offset/token based up-front. This is deferred beyond
  v1.
- **HTTP response order is not guaranteed.** For an endpoint that returns a
  JSON array or an unordered map, the record order is whatever the server
  returns; the connector does not sort. If your pipeline depends on ordering
  (e.g. "latest first"), require it from the endpoint (a sort param) rather than
  relying on insertion order.
- **Retries can reorder side effects.** Idempotent verbs (`GET`/`HEAD`) retry on
  `429`/`5xx` with backoff. Mutating verbs retry **only** when `idempotency_header`
  is declared, and then the connector sends a fresh idempotency key per attempt.
  A retried `POST` without an idempotency header is a single attempt — treat
  indeterminate write outcomes as unknown and avoid assuming success from a
  single 200.
- **No redirect following.** HTTP 3xx is surfaced as an error (with
  `location`/`Retry-After` metadata), never silently followed, so a moved
  endpoint cannot silently read a different resource.
- **Response bodies are capped** at `max_response_size`. A body larger than the
  cap aborts the call rather than buffering unbounded data.
