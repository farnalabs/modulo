# Migration Guide: System Config Endpoint

## What Changed

The `GET/PUT/DELETE /api/v1/system-admin/config` endpoints have been deprecated
in favour of `/api/v1/admin/system-config` (or a similar standardised admin path).

The `/api/v1/system-admin/config` prefix was inconsistent with the rest of the admin
API (which uses `/api/v1/admin/...`). This migration consolidates config management
under the standard admin namespace.

## Timeline

| Event | Date |
|---|---|
| Deprecation announced | 2026-07 |
| Sunset date | 2027-01-01 |
| Grace period ends | 2027-02-01 |
| Old endpoint removed | 2027-02-01 (or later) |

## Migration Steps

1. Replace all requests from `/api/v1/system-admin/config` → `/api/v1/admin/system-config`
2. The request and response models are identical
3. Update any API client configurations, MCP tool references, or CI scripts

## Request/Response Comparison

### Before (deprecated)
```
GET /api/v1/system-admin/config
PUT /api/v1/system-admin/config/{key}
DELETE /api/v1/system-admin/config/{key}
```

### After
```
GET /api/v1/admin/system-config
PUT /api/v1/admin/system-config/{key}
DELETE /api/v1/admin/system-config/{key}
```
