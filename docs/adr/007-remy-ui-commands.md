# ADR 007 — Remy UI Commands: Frontend-Mediated Browser Automation

**Date**: 2026-07-03  
**Status**: Active

---

## Context

Remy, the in-app AI assistant, can call MCP tools for backend operations (list pipelines, trigger runs, review HITL gates). But many user workflows require multi-page UI configuration — navigating to Settings, filling forms, clicking buttons — which MCP tools cannot do.

Two broad approaches were considered:

1. **Server-side browser automation** — Run Playwright on the backend, authenticated as the user. The LLM calls a `browser_navigate` MCP tool, the backend controls a headless browser.
2. **Frontend-mediated UI commands** — Register new tools (`navigate`, `click`, `fill`, etc.) alongside MCP tools. When the LLM calls them, the SSE stream forwards them to the frontend Vue app, which executes them in the user's own browser session with full authentication.

## Decision

Use **frontend-mediated UI commands** (option 2). Remy emits native LLM tool calls that are routed to the frontend over the existing SSE stream, executed in the user's browser, with results fed back to the LLM for continuation.

Concretely:

- New tools (`navigate`, `click`, `fill`, `extract`, `get_page_interactables`, `go_back`, `wait`, `press`) are defined alongside MCP tools and injected into the LLM's `tools` parameter.
- The existing SSE stream (`POST /sessions/{id}/stream`) becomes an **agentic loop**: multiple LLM turns within a single stream, separated by frontend execution pauses.
- When the LLM emits UI tool calls, the backend yields `event: ui_command_batch`, then awaits execution results via `asyncio.Event` + HTTP POST from the frontend.
- The frontend `UiCommandExecutor` executes commands sequentially using `data-testid` first, with DOM stability detection (`waitForDomStable`) and shadcn/vue component awareness.
- A permission system gates destructive actions: mode presets (safe/full_auto/locked_down), destructive selector pattern detection, and per-(tool, page) session approvals with 30min TTL.

## Why Not Server-Side Playwright

1. **Auth complexity** — The backend would need to authenticate as the user (forward cookies/JWT to a headless browser). Any auth failure or session expiry breaks automation silently. Frontend-mediated runs in the user's real session with their real auth.
2. **Infrastructure burden** — Playwright browser binaries add ~400MB to the deployment. Headless Chrome in Docker requires additional dependencies, memory, and process management. Frontend-mediated needs zero server-side changes.
3. **User sees nothing** — Server-side automation happens invisibly. Users can't see what Remy is doing, damaging trust. Frontend-mediated runs in the user's visible browser tab with highlighting, toasts, and an execution overlay.
4. **Stale state** — The server-side browser may not reflect the user's current view (different navigation state, open modals, unsaved changes). Frontend-mediated always operates on the user's actual DOM.
5. **Multi-tab conflicts** — Server-side browser shares a session with the user's real browser. Concurrent actions cause race conditions. Frontend-mediated has a visibility guard and per-session execution.

## What This Means for Code

| Concern | Approach |
|---|---|
| Communication channel | SSE (`event: ui_command_batch`) + HTTP POST (`/sessions/{id}/ui-command-results`) |
| LLM tool injection | Combined MCP + UI tools as `tools` parameter (tool-capable providers only) |
| Agentic loop | `while True` in stream generator — LLM call → tools → results → next LLM call |
| Permission gating | `_resolve_tool_permission()` — per-tool override → mode preset → safe defaults + destructive pattern matching |
| Session approvals | In-memory dict keyed by `(session_id, tool_name, page_path)` with 30min TTL, cleaned on lookup and logout |
| Selector strategy | `data-testid` first, CSS selector fallback, `get_page_interactables()` for discovery |
| DOM readiness | `waitForDomStable()` — MutationObserver scoped to `<main>`, 200ms quiet, spinner detection |
| Component handling | shadcn/vue-aware dispatch — combobox, switch, select, native input detection |
| Visual feedback | Element highlighting (outline flash), navigation toast, execution overlay with pointer-events |
| Stop/abort | Per-call `AbortController`, `cancelled_by_user` results, `abort_summary` event skips LLM turn |
| Non-tool models | Text-only description of UI tools via `_build_tool_definitions_for_text()` |
| SSE keepalive | Background task yields `event: ping` every 15s to prevent proxy timeout |
| Event registration | Registered before `yield` to prevent POST race |

## When to Revisit

- A user reports that the DOM stability heuristic (`waitForDomStable`) reliably fails for their workflow
- The app scales to multiple uvicorn workers — the in-process `asyncio.Event` registry must be swapped to Redis pub/sub
- A new permission model emerges (e.g., role-based tool defaults, team-scoped tool overrides)
- Browser-side automation proves insufficient for complex workflows (e.g., file downloads, cross-origin interactions)

At that point, server-side Playwright remains an option, but the frontend-mediated architecture would still be the primary path — Playwright would only supplement capabilities the frontend cannot provide.

## Related Documents

- PRD §8.27 — Remy UI Commands feature specification
- ADR 001 — Agent Execution Environment as a V1 Primitive
- `backend/src/modulo/api/routes/remy.py` — SSE stream handler (agentic loop entry point)
- `backend/src/modulo/api/ui_tools.py` — UI tool definitions
- `frontend/src/composables/useUiCommandExecutor.ts` — frontend command execution
- `frontend/src/composables/useRemyStream.ts` — event handler (permission_request, ui_command_batch)
