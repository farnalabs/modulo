---
id: feat-core-prompt-optimization
prd: 8.2
delivery-tasks: [task-nv10-prompt-optimization]
bdd:
  - backend/tests/features/agents/prompt_versioning.feature
  - backend/tests/bdd/features/pipelines/run_variants.feature
code:
  - backend/src/modulo/core/prompt_optimizer/
  - backend/src/modulo/api/routes/agents.py
  - backend/src/modulo/db/crud/agent.py
  - backend/src/modulo/db/crud/variant_group.py
  - backend/src/modulo/db/crud/pipeline_snapshot.py
  - backend/src/modulo/db/models/agent.py
depends-on: [feat-evals-eval-engine, feat-variants-variant-groups]
unit-tests:
  - backend/tests/unit/api/test_agent_prompt_versioning.py
  - backend/tests/unit/api/test_agent_prompts.py
  - backend/tests/unit/core/prompt_optimizer/test_prompt_optimizer.py
status: partial
---

# Prompt Optimization

LLM-driven prompt improvement from eval failures, with full version history, rollback, diff, and pipeline snapshot pinning.

## Behaviours

### Prompt Versioning

- [x] Every prompt edit creates a new entry in `prompt_version_history`
- [x] Version entries track `version`, `template`, `created_at`, `notes`, `optimized_from`, `eval_result_ids`
- [x] Users can roll back to any prior version without creating a new pipeline version
- [x] Rollback appends a new history entry (not a mutation) — history is append-only
- [x] PipelineSnapshot captures the specific prompt version hash in use at run-start
- [x] Snapshots store `prompt_version_hash` (SHA-256 of template) and `prompt_version_at` per agent
- [x] New runs using a pinned snapshot execute against the snapshot's pinned prompt, not the current prompt
- [x] New runs without a pinned snapshot use the agent's latest prompt template
- [x] Listing prompt versions returns entries sorted newest-first
- [x] Listing returns empty array when an agent has no version history
- [x] Getting a specific version returns that entry's template, metadata, and timestamps
- [x] Getting a non-existent version returns 404
- [x] Diffing two versions returns structured line-level result with `added`/`removed`/`unchanged` annotations
- [x] Diff with a non-existent version returns 404
- [x] Diff on a non-existent agent returns 404
- [x] Listing versions on a non-existent agent returns 404

### Prompt Optimization

- [x] `POST /agents/{id}/prompts/{version}/optimize` accepts one or more `eval_result_ids`
- [x] Request with an empty `eval_result_ids` array returns 422
- [x] Request with non-existent agent returns 404
- [x] Request with eval result IDs that resolve to zero results returns 404
- [x] Optimization uses the agent's `model_backend_id` by default
- [x] Optimization can override the model backend via `model_backend_id` in the request body
- [x] Optimization requires encrypted credential retrieval for the model backend
- [x] Fails with 500 if model backend credentials cannot be decrypted
- [x] Response includes `suggested_prompt`, `rationale`, `analysis`, and `version` (next version label)
- [x] The optimizer builds a failure context from eval results and eval definitions
- [x] Evaluation results are annotated with eval definition name, type, config, score, and detail
- [x] Missing eval definitions are shown as `unknown` in the failure context
- [x] Empty eval results produce an empty `<failing_evals>[]</failing_evals>` section
- [x] The optimizer sends a system prompt (instructions) + human message (context) to the LLM
- [x] LLM response is parsed as plain JSON
- [x] LLM response wrapped in a markdown code fence (with or without `json` language tag) is parsed correctly
- [x] Malformed LLM response (bad JSON) raises a JSON decode error
- [x] LLM response missing required keys (`suggested_prompt`, `rationale`) raises a KeyError
- [x] LLM call failures (network, timeout) propagate to the caller

### Apply Optimized Prompt

- [x] `POST /agents/{id}/prompts/{version}/apply` accepts `suggested_prompt`, `rationale`, `optimize_version`, `eval_result_ids`
- [x] Creates a new version entry linking back to the optimized-from version
- [x] Returns the updated agent with the new prompt template
- [x] Non-existent agent returns 404
- [x] Empty `suggested_prompt` returns 422
- [x] Apply failure (DB error) returns 404

### Pipeline Integration

- [x] Variant groups compare `prompt_version_hash` between base and variant snapshots
- [x] Prompt version pinning is independent of pipeline snapshot versioning — prompts can be rolled back without a new snapshot
- [x] Model backend `model_id` is also pinned in the snapshot (`model_backend_pins_json`) — consistent with prompt pinning

### Error Handling

- [x] All 5 agent CRUD routes (list, create, get, update, delete) catch ProgrammingError → 501
- [x] All 6 prompt routes (optimize, apply, list versions, get version, rollback, diff) catch ProgrammingError → 501
- [x] Missing DB table on ModelBackend query in optimize endpoint returns 501
- [x] Missing DB table on get_eval_results_with_defs returns 501
- [x] 422 on empty eval_result_ids in optimize endpoint
- [x] 404 on non-existent agent for all prompt endpoints
- [x] 404 on non-existent version for get/rollback/diff endpoints
- [x] 404 on no eval results found for optimize endpoint
- [x] 404 on non-existent model backend for optimize endpoint
- [x] 500 on credential decryption failure for optimize endpoint
- [x] LLM call failures (network, timeout) propagate to caller for optimize endpoint
- [x] Malformed LLM response or missing required keys raises error in optimize endpoint

### Edge Cases

- [x] Version label in optimize response is computed from history length, not validated against `version` path param — non-existent version like "v99" accepted as source
- [x] Apply with duplicate version label creates entry with same label as existing
- [x] Rollback to current version creates a new history entry with same template (append-only)
- [x] Diff of same version (version_a == version_b) returns all lines as "unchanged"
- [x] Empty version history returns empty list for list endpoint
- [x] Empty template string accepted as valid prompt for version creation

## Known Gaps
- No unit test for get_prompt_diffs hash comparison logic (existing test is mock-based, not testing real diff computation)
- No performance or regression tests for large version histories (100+ entries)
- No unauthorized access scenarios for prompt history (non-member org, viewer role)

## QA History

### 2026-07-03 — Cross-cutting QA (improve-architecture index 106)
- Marked 3 stale Pipeline Integration checkboxes [ ]→[x] (prompt version hash comparison, independent pinning, model_backend_pins_json)
- Added Error Handling section (12 behaviour checkboxes)
- Added Edge Cases section (6 checkboxes)
- Refined Known Gaps (gap #1: corrected to "mock-based test, not real diff computation")
- Status: partial (3 known gaps remain) 