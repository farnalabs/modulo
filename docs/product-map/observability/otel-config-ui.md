---
id: feat-observability-otel-config-ui
prd: 6.6
delivery-tasks: [task-nv9-otel-config-ui]
bdd: backend/tests/bdd/features/observability/otel_traces.feature
code:
  - backend/src/modulo/api/routes/observability.py
  - backend/src/modulo/db/crud/observability.py
  - backend/src/modulo/db/models/organisation.py
  - backend/src/modulo/db/migrations/versions/0008_otel_config.py
  - backend/src/modulo/settings.py
  - frontend/src/views/SettingsObservabilityView.vue
  - frontend/src/lib/api/schema.ts
depends-on: []
unit-tests:
  - backend/tests/unit/api/test_observability_routes.py
  - backend/tests/bdd/steps/test_observability.py
status: partial
---

# OTel Config UI

Per-org OpenTelemetry exporter configuration: OTLP endpoint, dynamic headers, export
interval, and LangSmith toggle + API key. Stored in `organisations.otel_config_json`.
Settings page at `/settings/observability`.

## Behaviours

### Database — `organisations.otel_config_json`

- [x] JSON column stores otlp_endpoint, otlp_headers, export_interval_seconds, langsmith_enabled, langsmith_api_key_ciphertext
- [x] Column is NOT NULL with server_default `'{}'::json`
- [x] Migration 0008 adds column to organisations table
- [x] CRUD: get_otel_config() reads config for current org (RLS-scoped)
- [x] CRUD: update_otel_config() writes config for current org
- [x] Empty dict treated as default configuration (no OTLP, langsmith disabled, 10s interval)

### REST API — `/api/v1/settings/observability`

- [x] `GET` returns merged config (DB values + env var overrides) as OtelSettingsResponse
- [x] `PUT` accepts OtelSettingsUpdate, encrypts LangSmith key with Fernet, persists, returns merged config
- [x] `POST /test` sends a real OTLP span to the configured endpoint, returns success/failure with message
- [x] `GET /preview` generates a sample span config without exporting
- [x] All endpoints require authentication + RLS org scoping
- [x] LangSmith API key never returned in plaintext — has_langsmith_api_key boolean on read
- [x] Empty string LangSmith key on write clears stored key (sets null)
- [x] Sensitive OTLP header keys masked on read (authorization, x-api-key, api-key, x-otlp-token → ••••••)
- [x] Test endpoint distinguishes TimeoutException vs ConnectError vs generic errors with user-friendly messages
- [x] Test endpoint returns 200 with `success: false` if no endpoint configured (not a 400 error — returns user-friendly error in message field)
- [x] PUT returns 422 on invalid OTLP endpoint URL format
- [x] Env var override: OTEL_EXPORTER_OTLP_ENDPOINT env var shadows DB config — effective_otlp_endpoint returned in response

### UI — `/settings/observability`

- [x] OTLP endpoint URL text input
- [x] Dynamic OTLP headers: add/remove key-value rows
- [x] Sensitive header values masked with •••••• in the UI
- [x] Export interval number input (minimum 1 second)
- [x] LangSmith toggle switch
- [x] LangSmith API key password field with show/hide toggle
- [x] "Key already stored" indicator when LangSmith key exists
- [x] Test Connection button — POSTs test span, shows success/failure with auto-clear after 10s
- [x] Save button with loading state and success/error feedback
- [x] Reset button reverts to previously saved config
- [x] Env override warning banner when OTEL_EXPORTER_OTLP_ENDPOINT is set
- [x] Route registered at `/settings/observability`
- [x] TypeScript types (OtelSettingsResponse, OtelSettingsUpdate, TestOtelConfig, TestSpanResult) in schema.ts

### Startup — OTel Provider Configuration

- [x] App startup calls setup_otel(service_name, telemetry_enabled) with module-level settings
- [x] Telemetry disabled by default (opt-in via MODULO_TELEMETRY_ENABLED=true) — no-op TracerProvider
- [x] Stdout exporter (ConsoleSpanExporter) active when telemetry enabled
- [x] OTLP HTTP exporter conditionally added when OTEL_EXPORTER_OTLP_ENDPOINT env var set
- [x] OTLP exporter failure caught gracefully (log warning, continue without OTLP)
- [x] setup_otel is idempotent — replaces global TracerProvider on repeated calls
- [x] Shutdown flushes all buffered spans
- [x] Fernet encryption key for LangSmith secrets derived from MODULO_FERNET_KEY
- [x] MODULO_OTEL_SERVICE_NAME configurable (default "modulo")
- [x] Docker OTel Collector config provided (configs/otel-collector.yml) — OTLP gRPC :4317

### BDD coverage

- [x] 4 scenarios in otel_traces.feature: chain span capture, tool child spans, no credentials in attributes, disabled produces no spans
- [ ] Step definitions exist but are mock-based — no DB-level or integration-level coverage

### Not yet implemented — gaps

- [ ] ExportPreview wired into frontend UI — GET /preview API exists but no frontend button or display
- [ ] Configurable trace sampling / rate-limiting (no head-based or tail-based sampling config)
- [ ] BatchSpanProcessor (currently uses SimpleSpanProcessor — synchronous, one-at-a-time export)
- [ ] Effective endpoint read-only display in normal mode (only shown in env-override banner)
- [ ] Per-org telemetry toggle in UI (currently controlled by global env var only — per-org is DB-stored but needs UI control)

### Error Handling

- [x] `GET` endpoint catches `ProgrammingError` → returns 501 Not Implemented
- [x] `PUT` endpoint catches `ProgrammingError` → returns 501 Not Implemented
- [x] `GET /preview` endpoint catches `ProgrammingError` → returns 501 Not Implemented
- [x] `POST /test` no DB access — no ProgrammingError risk
- [x] All ProgrammingError catches use `except ProgrammingError` (not broad `except SQLAlchemyError`)
- [x] `GET` endpoint catches `TimeoutError` → falls back to degraded response with cached/default config
- [x] `PUT` endpoint catches `TimeoutError` → re-raises as 500
- [x] `GET /preview` endpoint catches `TimeoutError` → falls back to cached/default config
- [x] `GET` endpoint catches generic `Exception` → falls back to degraded response
- [x] `PUT` endpoint catches generic `Exception` → re-raises as 500
- [x] Wait-for-DB timeout enforced via `asyncio.wait_for` with `_DB_TIMEOUT` (10s)
- [x] Timeout/error events logged with org_id context via `_log.warning` / `_log.exception`

### Resilience

- [x] In-memory cache (`_config_cache`) stores last successful DB read per org_id
- [x] Cache TTL of 60 seconds (`_CACHE_TTL`) — avoids serving stale data for too long
- [x] Cache returns defensive copy (`dict(entry)`) — callers cannot corrupt cached state
- [x] Cache invalidated on successful write (`_invalidate_cache`)
- [x] Degraded response (`_build_degraded_response`) returns cached config when DB unavailable
- [x] Degraded response falls through to `_DEFAULT_OTEL_CONFIG` when no cache exists
- [x] Default config provides safe fallback values for all fields (empty endpoint, 10s interval, disabled LangSmith)
- [x] Sensitive header values masked with `••••••` in API responses — never leaked in degraded mode
- [x] LangSmith API key never returned in plaintext — boolean `has_langsmith_api_key` only
- [x] Empty LangSmith key on write clears stored key (`langsmith_api_key_ciphertext = None`)
- [x] Fernet encryption for LangSmith API key at rest
- [x] Test endpoint distinguishes TimeoutException vs ConnectError vs generic errors with user-friendly messages
- [x] Test endpoint returns 200 with `success: false` if no endpoint configured

## Known Gaps

- **BDD step definitions are mock-only:** `otel_traces.feature` has 4 real scenarios and matching step definitions, but they use mock/patch rather than real DB or OTel exporter integration
- **ExportPreview not wired:** `GET /api/v1/settings/observability/preview` endpoint exists but frontend never calls it — no "preview config" button or display
- **SimpleSpanProcessor:** Uses synchronous per-span export instead of production-grade BatchSpanProcessor with buffering, batching, and backpressure
- **No sampling config:** No UI field or DB schema for trace sampling rate — every span is either exported or not
- **Per-org telemetry control:** LangSmith toggle exists but the core OTLP enable/disable is global env-var-only at startup, not per-org through the UI
- **Frontend i18n gap:** SettingsObservabilityView.vue has ~20+ hardcoded English strings not using `$t()` (page title, section headings, labels, button text, placeholders, status messages, aria-labels)
- **API error formatting:** `saveSettings()` and `loadSettings()` embed `${err}` directly in template literals instead of using `formatApiError(err)` — `openapi-fetch` returns error objects, causing `[object Object]` in user-facing error messages
- **No website docs:** No observability/otel page exists under `Website/modulo-website/src/docs/`
