# API Versioning — Modulo

## Approach

URL-path versioning (`/api/v1/`, `/api/v2/`, etc.) — chosen for simplicity and broad tooling compatibility. The version is part of the URL path, making it immediately visible in logs, curl commands, proxies, and API client configs.

```
GET /api/v1/pipelines
POST /api/v2/runs
```

## When to Version

A new API version is required when making **breaking changes** to an existing endpoint:

- **Field removals** — deleting a field from a response or request body
- **Type changes** — changing a field's type (e.g. `string` → `integer`, `number` → `string`)
- **Auth changes** — changing authentication requirements or mechanisms for an existing endpoint
- **Semantic changes** — altering endpoint behaviour in a way that breaks existing clients (e.g. changing pagination semantics, changing sort order defaults)
- **Endpoint removal** — removing an endpoint that clients depend on
- **Required field additions** — adding a new required field to an existing request body

## When NOT to Version

The following changes do not require a new API version:

- **Adding new fields** to existing response bodies (clients that ignore unknown fields will not break)
- **Adding new endpoints** — new functionality at a new URL path
- **Bug fixes** — correcting behaviour that was clearly broken or unintended
- **Performance improvements** — response time changes, caching behaviour
- **Documentation changes** — clarifications, examples, typo fixes
- **Adding optional fields** to request bodies

## Deprecation Policy

When an API version or endpoint is deprecated:

1. **Announcement:** Deprecation is announced in the changelog (see `/api/v1/changelog`) and, for major versions, via a notification in the Modulo admin UI
2. **Deprecation headers:** All deprecated endpoints return:
   - `Deprecation: true` — signals the endpoint is deprecated
   - `Sunset: <ISO-date>` — the date after which the endpoint will be removed
   - `Link: <migration-url>; rel="deprecation"` — link to the migration guide
3. **Minimum deprecation period:** 90 days from announcement to sunset, except for security fixes (which may be shorter at the discretion of the Modulo team)
4. **Grace period:** After the sunset date, the endpoint returns `410 Gone` for a further 30 days before being removed entirely
5. **Breaking security fixes** may be deployed with a shorter deprecation window; this is documented in the changelog entry

## Migration Guides

Each new API version gets a migration guide at `docs/operations/migrations/v1-to-v2.md` (or similar). The guide covers:

- What changed and why
- Before/after request/response examples
- Common migration pitfalls
- Automated migration tooling, if available

## Changelog

All API changes are published at `/api/v1/changelog`. The changelog endpoint returns entries sorted by date descending. Each entry includes:

| Field | Type | Description |
|---|---|---|
| `version` | `string` | API version identifier (e.g. `"1.0"`, `"1.1"`, `"2.0"`) |
| `date` | `string` | ISO 8601 date of the change |
| `summary` | `string` | One-line summary of the change |
| `changes` | `list[string]` | Individual change descriptions |
| `deprecations` | `list[string]` or null | Features being deprecated in this release |
| `migration_url` | `string` or null | Link to the migration guide, if applicable |

## API Lifecycle

```
v1.0 ──┬── v1.1 (backward-compatible) ──┬── v1.2 (backward-compatible)
       │                                 │
       └── v2.0 (breaking) ──────────────┘
```

- Minor version bumps (`v1.0` → `v1.1`) are backward-compatible additions
- Major version bumps (`v1.x` → `v2.0`) signal breaking changes
- At most two major versions are supported simultaneously
- When a new major version ships, the previous major version enters deprecation with a minimum 90-day sunset window
