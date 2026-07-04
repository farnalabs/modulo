---
id: feat-pipelines-prompt-reveal
prd: 8.9
delivery-tasks: [task-api-prompt-reveal, task-prd-prompt-reveal-api]
bdd:
  - backend/tests/bdd/features/ui/run_detail.feature
unit-tests:
  - backend/tests/unit/api/test_prompt_reveal.py
  - backend/tests/unit/api/test_prompt_reveal_programming_error.py
code:
  - backend/src/modulo/api/routes/runs.py
  - backend/src/modulo/db/models/agent.py
  - frontend/src/views/RunDetailView.vue
  - frontend/src/lib/api/schema.ts
depends-on: [feat-pipelines-run-trace-observability, feat-core-agent-model]
status: partial
---

# Prompt Reveal API

Server-authenticated reveal of the rendered prompt sent to an LLM for a specific
run node. Prompts are masked by default (`[Prompt hidden — click to reveal]`)
and revealed per-node on click. Sensitive credential-like values (api_key, secret,
token, password) are always masked in the response. The `prompt_always_visible`
agent flag disables frontend masking for pipelines whose prompts contain no
sensitive data.

## Behaviours

### Backend — Masking
- [x] Credential-like values (api_key, secret, token, password) masked with bullet characters
- [x] Non-sensitive text left untouched
- [x] Multiple sensitive values in a single prompt all masked
- [x] Masking applied to all message roles (system, user, assistant)

### Backend — Prompt Reconstruction
- [x] System message from agent prompt_template
- [x] User message from input_payload or checkpoint state
- [x] Assistant messages from previous node outputs
- [x] Own node output excluded from assistant messages
- [x] Checkpoint state preferred over input_payload for current input
- [x] Encrypted checkpoint data decrypted with Fernet key

### Backend — Token Estimation
- [x] Token count estimated via 4-char-per-token heuristic
- [x] Empty string returns 1

### Backend — Agent Lookup
- [x] Agent found by node_id in graph_json
- [x] Node without agent returns no system message
- [x] Nonexistent node returns 404

### Backend — Endpoint
- [x] Auth required (401/403 if no token)
- [x] Run not found returns 404
- [x] Node not found returns 404
- [x] Returns prompt, messages, token_count, prompt_always_visible
- [x] Consistent token count across repeated calls
- [x] Agent not found returns 404
- [x] Non-agent node returns prompt without system message

### Backend — Error Handling
- [x] Missing DB table (ProgrammingError) returns 501 Not Implemented
- [x] Run not found returns 404
- [x] Snapshot not found returns 404
- [x] Node not found returns 404
- [x] Agent not found returns 404
- [x] Auth required (401/403)

### Backend — prompt_always_visible
- [x] Agent model has prompt_always_visible field (defaults to false)
- [x] Response includes prompt_always_visible flag
- [x] Server always masks credential values regardless of flag

### Backend — prompt_always_visible (Agent Config)
- [ ] Agent editor UI includes prompt_always_visible toggle
- [x] Agent API CRUD supports prompt_always_visible field

### Frontend — Run Detail View
- [x] "Prompt" column in execution trace table
- [x] Masked by default: shows "[Prompt hidden — click to reveal]"
- [x] Click-to-reveal calls POST /api/v1/runs/{run_id}/nodes/{node_id}/prompt/reveal
- [x] Revealed prompt displayed in a dialog with token count
- [x] Copy prompt button in dialog
- [x] Per-node reveal isolation (revealing one node does not reveal others)
- [x] 30-second TTL on revealed prompt DOM value
- [x] Auto-reveal dialog when prompt_always_visible is true (after API call)

## Error Handling

- [x] Missing DB table (ProgrammingError) returns 501 Not Implemented
- [x] Run not found returns 404
- [x] Snapshot not found returns 404
- [x] Node not found returns 404
- [x] Agent not found returns 404
- [x] Auth required (401/403)
- [ ] 422 validation errors for invalid input parameters

## Known Gaps
- No Agent editor UI toggle for prompt_always_visible
- No website docs page for prompt-reveal API
- 30-second TTL on revealed prompt DOM value is client-side only
  (PRD §8.9 calls for Redis-backed token mechanism — only the DOM timer exists)

## QA History

### 2026-07-03 — Cross-cutting QA (feat-pipelines-prompt-reveal-104)
- **Finding 1 (CRITICAL)**: `reveal_node_prompt` endpoint didn't catch `ProgrammingError`
  — Fixed: wrapped function body in try/except ProgrammingError returning 501.
- **Finding 2 (MAJOR)**: Agent API schemas didn't expose `prompt_always_visible`
  — Fixed: added to AgentCreate, AgentUpdate, AgentResponse, and create_agent endpoint call.
- **Finding 3 (MAJOR)**: No BDD scenarios for prompt reveal
  — Added 2 scenarios to `run_detail.feature` with Playwright step definitions in test_ui.py.
- **Finding 4 (MAJOR)**: Missing unit test for `ProgrammingError`→501 on prompt reveal
  — Fixed: created `test_prompt_reveal_programming_error.py`.
- **Finding 5 (MAJOR)**: Missing error path tests (agent/snapshot 404)
  — Fixed: added `test_reveal_snapshot_not_found_returns_404` and `test_reveal_agent_not_found_returns_404` to test_prompt_reveal.py.
- **Finding 6 (MAJOR)**: Frontend doesn't auto-reveal when `prompt_always_visible` is true
  — Fixed: added `showPrompt(nodeName)` call in `revealPrompt()` after successful API response when flag is true.
- **Finding 7 (MINOR)**: No website docs stub for prompt-reveal
  — Fixed: created stub at Website/modulo-website/src/docs/pipelines/prompt-reveal.md.
