# ADR 016 — Agent Log Observability

**Date**: 2026-07-19
**Status**: Accepted

---

## Context

The `sandbox_agent` node type (established in ADR 003) dispatches work to external agent runtimes running in E2B sandboxes. The E2B `commands.run()` API returns stdout and stderr from the agent process as part of the `cmd_result` object, but Modulo was discarding these — only extracting the structured JSON from the output file and the exit code.

This creates a visibility problem. Customers need to see what the agent did during a run, both for post-hoc debugging ("why did the agent modify those files?") and for audit ("what commands did the agent execute?"). The agent's stdout contains progress updates, shell command output, tool call traces, and error messages that are essential context for understanding the agent's behaviour and decisions.

Modulo already has infrastructure that makes solving this tractable:

- **OpenTelemetry bridge** (ADR 003 context): LangGraph's callback system emits span events through the OTel bridge, flowing through the configured OTel exporter to the customer's monitoring stack.
- **SSE EventBus**: Real-time frontend sync via WebSocket fan-out, providing live run updates without polling.
- **Run inspection API**: The existing run status and output API endpoints serve per-node state from LangGraph checkpoint snapshots.

The PRD establishes two related requirements. First, a table-stakes requirement at 6.6: "OpenTelemetry-native observability — plugs into existing monitoring without custom work." Second, a scope boundary at 6.20: "Modulo is the SDLC orchestration layer — dispatch, auth, audit, cost tracking, eval gates, HITL — not the agent loop itself." Agent log observability sits at the intersection of these: it's observability, but it's observability of the dispatched agent, not of Modulo's internal execution.

---

## Decision

Adopt a **two-channel approach** for agent stdout/stderr observability:

### Channel 1 — Run Context Artifact

stdout and stderr are captured from the `cmd_result` returned by `sandbox.commands.run()` and included in the `sandbox_agent` node's output dict. This flows into LangGraph checkpoint state alongside the structured output, serving two consumers:

1. **Run inspection API**: The per-node output endpoint surfaces `agent_stdout` and `agent_stderr` alongside the structured `agent_output` field.
2. **RunDetailView UI**: The run inspection view includes an expandable IO panel per node, showing stdout and stderr with basic formatting (monospace, scrollable, truncated to the first 100KB).

This channel is always active — no configuration required. It works offline, without OTel exporters, and is available immediately after a run completes.

### Channel 2 — OTel Span Events

stdout and stderr are also emitted as a single `sandbox.agent.output` span event on the current execution span via the OpenTelemetry API. The event carries:

| Attribute | Type | Description |
|---|---|---|
| `event.name` | string | `sandbox.agent.output` |
| `stdout` | string | Truncated stdout text (first 32KB) |
| `stderr` | string | Truncated stderr text (first 32KB) |
| `stdout_length` | integer | Total stdout length before truncation |
| `stderr_length` | integer | Total stderr length before truncation |

These events flow through the configured `SpanExporter` — the same OTel pipeline used for LangGraph execution spans — to the customer's monitoring stack (Loki, Grafana, DataDog, SigNoz, etc.).

This channel is opt-in: it activates automatically when OTel is configured (via `OTEL_EXPORTER_OTLP_ENDPOINT` or the standard OTel environment variables). Customers without OTel lose nothing — Channel 1 still works.

---

## What Was Deliberately NOT Built

### Live log streaming / terminal emulator

The natural next step would be switching from `commands.run()` to `commands.stream()` or the E2B process API, building a terminal-emulator Vue component, and relaying output through the SSE EventBus in real time. This is scope creep: real-time streaming, backpressure handling, terminal rendering, and reconnection logic belong in a dedicated feature. E2B's own web UI already provides terminal access, and Grafana can stream logs from OTel. Modulo should not replicate either.

### Log storage in Modulo's database

Agent stdout can be megabytes in size — a single opencode invocation running a PR review across a large codebase might emit hundreds of kilobytes of traces. Storing this in Postgres alongside pipeline entities is the wrong data model. The customer's existing OTel backend handles log retention, indexing, and search. Channel 1 stores a truncated snapshot in checkpoint state (for immediate display); Channel 2 delegates long-term storage to OTel.

### Structured log contract

Enforcing a structured logging format on external agents (levels, categories, timestamps, structured fields) would require rebuilding all E2B templates and adds no value for raw process output. If agents write structured logs (JSON lines, key=value pairs, etc.), they already flow through stdout. Modulo treats stdout/stderr as opaque text — structured parsing is the customer's concern, to be handled in their OTel pipeline.

### Log search UI in Modulo

Searching across agent logs (full-text search, filtering by time range or node type, faceted exploration) belongs in the customer's observability stack — Loki's LogQL, DataDog's log search, or Grafana's explore view. Building a log search UI in Modulo would duplicate functionality that every observability platform already provides.

---

## Consequences

- Customers can see agent stdout/stderr in the RunDetailView immediately after a run completes, without any configuration or infrastructure setup.
- Customers with OTel configured get agent logs in their existing monitoring stack automatically — no additional pipeline or exporter needed.
- No new infrastructure burden: Channel 1 leverages existing LangGraph checkpoint state; Channel 2 leverages the existing OTel bridge and exporter configuration.
- stdout/stderr are truncated to the first 100KB for state storage (Channel 1) and to the first 32KB per OTel span event (Channel 2). Truncation limits are documented in the `node_runner.py` source as named constants `_MAX_ARTIFACT_LOG` and `_MAX_OTEL_LOG_ATTR`.
- The output contract for `sandbox_agent` nodes is extended with optional `agent_stdout` and `agent_stderr` fields. This is backward compatible — existing pipelines that read the structured output from the well-known path continue to work unchanged. The new fields are absent when the sandbox agent produces no stdout/stderr.

---

## Future Work

- **Live log streaming**: Using `sandbox.commands.stream()` or the E2B process API, relayed through the SSE EventBus, could be added without changing the data model or breaking existing consumers. The two-channel approach leaves room for a third "streaming" channel that bypasses checkpoint state entirely.

---

## References

- ADR 003: Agent Dispatch Model — establishes Modulo's scope boundary as orchestration, not agent runtime
- ADR 005: Agent Architecture Two-Tier — E2B template and sandbox architecture
- PRD 6.6: OpenTelemetry — existing OTel strategy and table-stakes requirement
- PRD 6.20: Agent Dispatch Model — output contract and observability requirements
- PRD 8.22: SSE Event Bus — existing real-time infrastructure
- `node_runner.py:make_sandbox_agent_fn` — the implementation location for sandbox agent dispatch
