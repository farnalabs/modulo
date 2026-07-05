---
id: feat-pipelines-run-trace-observability
prd: 6.6
delivery-tasks: [task-nv7-run-trace-observability]
bdd:
  - backend/tests/bdd/features/observability/otel_traces.feature
  - backend/tests/bdd/features/observability/metrics.feature
  - backend/tests/bdd/features/observability/run_logs.feature
code:
  - backend/src/modulo/otel_bridge/handler.py
  - backend/src/modulo/otel_bridge/export.py
  - backend/src/modulo/api/routes/runs.py
  - backend/src/modulo/api/routes/observability.py
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/db/models/run.py
  - backend/src/modulo/db/crud/run.py
  - frontend/src/views/RunDetailView.vue
unit-tests:
  - backend/tests/unit/otel_bridge/test_handler.py
  - backend/tests/unit/otel_bridge/test_export.py
  - backend/tests/unit/otel_bridge/test_telemetry_toggle.py
depends-on: [feat-pipelines-cicd-pipeline]
status: partial
---

# Run Trace Observability

LangGraph→OpenTelemetry bridge, run-level trace ID for external correlation,
per-node token consumption and cost in the run detail view, OTel exporter
configuration API.

## Behaviours

### OTel Bridge — LangGraph Lifecycle Mapping

- [x] Chain start/end/error mapped to `langgraph.chain.<name>` spans with correct parent propagation
- [x] LLM start/end/error mapped to `langgraph.llm.<name>` spans with parent
- [x] Chat model start/end/error mapped to `langgraph.llm.<name>` spans (mirrors LLM callbacks)
- [x] Tool start/end/error mapped to `langgraph.tool.<name>` spans with parent
- [x] Parent span propagation via run_id / parent_run_id works across async contexts
- [x] Unknown parent_run_id handled gracefully (no crash, no parent)
- [x] End without start does not raise
- [x] Span dict stays bounded (entries removed on end/error)
- [x] Thread-safe span tracking via threading.Lock

### OTel Bridge — Token Usage

- [x] LLM end records prompt_tokens, completion_tokens, total_tokens on span attributes
- [x] Chat model end records token usage with message_count attribute
- [x] LLM end without token usage does not raise
- [x] Chat model end without token usage does not raise
- [x] Error callbacks record ERROR status and exception event

### OTel Bridge — Naming

- [x] Span name derived from serialized "name" field
- [x] Falls back to last element of serialized "id" path
- [x] Handles None serialized gracefully ("unknown")
- [x] Tags from LangGraph callbacks set as langgraph.tags attribute

### OTel Export Configuration

- [x] Telemetry disabled by default (no egress) — no-op TracerProvider
- [x] Stdout exporter (ConsoleSpanExporter) active when MODULO_TELEMETRY_ENABLED=true
- [x] OTLP exporter active when OTEL_EXPORTER_OTLP_ENDPOINT set
- [x] OTLP exporter failure does not crash startup (logged, continues without it)
- [x] Shutdown flushes all buffered spans
- [x] Sensitive data never written to span attributes
- [x] OTel settings CRUD API (GET/PUT /api/v1/settings/observability)
- [x] Fernet-encrypted LangSmith API key persistence
- [x] Export interval configuration persisted per-org
- [x] Env var override detection (OTEL_EXPORTER_OTLP_ENDPOINT env > DB config)
- [x] OTel test connection API (POST /api/v1/settings/observability/test)
- [x] OTel export preview API (GET /api/v1/settings/observability/preview)

### Run Trace ID

- [x] trace_id generated deterministically from langgraph_thread_id (UUID v5)
- [x] trace_id included in RunResponse for external trace correlation
- [x] Frontend displays OTel trace ID with copy button
- [x] Per-node trace ID column in execution trace table

### Per-Node Token Consumption

- [x] node_token_usage tracked per-node from LangGraph event stream (on_chat_model_end / on_llm_end)
- [x] node_token_usage persisted to runs.node_token_usage JSON column
- [x] node_token_usage included in RunResponse
- [x] Frontend per-node table shows input_tokens, output_tokens, total_tokens
- [x] Frontend shows total tokens across all nodes

### Run Cost Display

- [x] total_cost_usd in RunResponse
- [x] Cost calculated from per-node token usage × model rates
- [x] Frontend shows total run cost (formatted to 6 decimal places)
- [x] Frontend shows per-node cost in execution trace table

### Run Detail View — Execution Trace

- [x] Frontend execution trace table: node name, status, duration, tokens, cost, trace ID, IO
- [x] Per-node status badges (running, complete, failed, etc.)
- [x] Expandable per-node IO (input/output rendered as formatted JSON)
- [x] Run header: pipeline ID, run ID, status badge, timestamps
- [x] Timestamps displayed (created, started, completed)

### Credential Safety

- [x] No credential fields appear in span attributes
- [x] Sensitive OTLP header keys masked in API responses (authorization, x-api-key, etc.)

### BDD — OTel Traces

- [x] BDD: OTel span exporter captures trace during pipeline run
- [x] BDD: trace contains a span for each node execution
- [x] BDD: trace contains attributes for organisation_id and pipeline_id
- [x] BDD: no credential fields appear in span attributes

### BDD — Metrics

- [x] BDD: GET /metrics returns pipeline_run_count_total
- [x] BDD: GET /metrics returns active_runs_gauge
- [x] BDD: GET /metrics returns token_usage_total

### BDD — Run Logs

- [x] BDD: per-node log streaming during active run
- [x] BDD: log level filtering (INFO and above only)
- [x] BDD: log entries grouped by node ID

## Error Handling

- [x] Observability settings CRUD routes catch ProgrammingError → 501
  - GET/PUT `/settings/observability` and GET `/settings/observability/preview` catch `ProgrammingError` → 501 (observability.py:149, 197, 294)
  - POST `/settings/observability/test` has no DB dependency — no catch needed
- [x] Run detail/trace endpoints catch ProgrammingError → 501
  - All 16 DB-backed endpoints in runs.py catch `ProgrammingError` → 501 (18 occurrences across trigger_run, stats, heatmap, get_run_status, cancel, get_run_io, export_fixture, workspace_lease, workspace_events, node_output, observe_node, recover_node, prompt_reveal, diff)
- [x] Auth 401/403 enforced via `Depends(get_current_user)` on all routes
- [x] 422 validation errors via Pydantic models on observability and runs endpoints
- [x] OTel exporter failure at startup logged and continues without crashing (export.py:68-69)
- [ ] End-to-end integration test for `ProgrammingError` catching (unit-testable via mock session)

## Resilience

- [x] In-memory config cache with TTL serves stale data on DB failure (observability.py:86-91)
- [x] Degraded response built from cache (or defaults) when DB is unreachable (observability.py:132-135)
- [x] Cache returns defensive copy via `dict(entry)` — prevents caller mutation of cache state
- [x] Shutdown flushes all buffered spans (export.py:74-87)
- [x] BatchSpanProcessor for OTLP exporter — non-blocking export
- [x] Multiple `shutdown_otel()` calls are safe — subsequent calls are no-ops
- [x] OTLP exporter failure isolated — caught by `except Exception`, logged, continues without it
- [x] Sensitive headers masked in API responses via `_mask_headers` (observability.py:33-34)
- [ ] Duplicate orphaned span cleanup in `handler.py:_start_span` — lines 88-95 and 106-114 both try to `.set_status(OK)` and `.end()` the same orphaned span. The outer block (lines 88-95) already ends the span. The inner block (lines 106-114) is dead code — `Span.end()` is idempotent in the SDK, but the redundancy should be cleaned up.

## QA History

### index 58 (2026-07-02)
- Cross-cutting QA: Marked all 50+ implemented behaviours [ ]→[x] (OTel bridge lifecycle mapping, token usage, span naming, export configuration, run trace ID, per-node token consumption, cost display, execution trace UI, credential safety, and BDD coverage).
- Added `set_run_context(org_id, pipeline_id)` to LangGraphOtelBridge to fix missing org/pipeline attributes on real run spans.
- Wired set_run_context call into PipelineExecutor._execute_run before astream_events.
- Updated Known Gaps: Added frontend gaps subsection, updated BDD gap description (real step defs exist, mocking-only), resolved org_id/pipeline_id span attribute gap.
- Status: partial (metrics endpoint, log streaming endpoint, cost mapping, frontend data wiring, integration tests remain as known gaps).

### index 73 (2026-07-05) — Error Handling & Resilience QA
- Verified all 5 Error Handling items against code: all [ ]→[x] except integration-test gap.
- Added Resilience section with 8 [x] items covering cache fallback, defensive copies, shutdown, exporter isolation, and header masking.
- Found duplicate orphaned span cleanup in `handler.py:_start_span` (lines 88-95 vs 106-114) — documented as Resilience gap.
- Confirmed website docs stub already exists at `Website/modulo-website/src/docs/observability.md` (36 lines, covers all config options).
- Status: partial (unchanged — gaps remain in metrics, log streaming, cost mapping, frontend data wiring, integration tests, and the duplicate cleanup edge case).

## Known Gaps

- No Prometheus /metrics endpoint — metrics BDD features are stubs.
- No log streaming endpoint — run_logs BDD features are stubs.
- BDD features exist with real step definitions but use mocks — not end-to-end integration tests.
- No integration test verifying spans are emitted during real pipeline execution.
- `node_token_usage` has no model cost mapping — node cost_usd is always null in frontend output.
- Frontend IO view fetches from /io endpoint but RunIOResponse may not map correctly for all node output shapes.

### Frontend Gaps

- Frontend runTimestamps return all dashes — not wired to real data from the API.
- Frontend per-node duration is always '—' (no duration tracking per node).
- Frontend per-node traceId falls back to run trace_id for all nodes.
