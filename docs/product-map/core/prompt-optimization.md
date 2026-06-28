---
id: feat-core-prompt-optimization
prd: 8.2
delivery-tasks: [task-nv10-prompt-optimization]
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
status: partial
---
# Prompt Optimization LLM-driven prompt improvement from eval failures, with full version history, rollback, diff, and pipeline snapshot pinning. ## Behaviours ### Prompt Versioning
- [ ] Every prompt edit creates a new entry in `prompt_version_history`
- [ ] Version entries track `version`, `template`, `created_at`, `notes`, `optimized_from`, `eval_result_ids`
- [ ] Users can roll back to any prior version without creating a new pipeline version
- [ ] Rollback appends a new history entry (not a mutation) — history is append-only
- [ ] PipelineSnapshot captures the specific prompt version hash in use at run-start
- [ ] Snapshots store `prompt_version_hash` (SHA-256 of template) and `prompt_version_at` per agent
- [ ] New runs using a pinned snapshot execute against the snapshot's pinned prompt, not the current prompt
- [ ] New runs without a pinned snapshot use the agent's latest prompt template
- [ ] Listing prompt versions returns entries sorted newest-first
- [ ] Listing returns empty array when an agent has no version history
- [ ] Getting a specific version returns that entry's template, metadata, and timestamps
- [ ] Getting a non-existent version returns 404
- [ ] Diffing two versions returns structured line-level result with `added`/`removed`/`unchanged` annotations
- [ ] Diff with a non-existent version returns 404
- [ ] Diff on a non-existent agent returns 404
- [ ] Listing versions on a non-existent agent returns 404 ### Prompt Optimization
- [ ] `POST /agents/{id}/prompts/{version}/optimize` accepts one or more `eval_result_ids`
- [ ] Request with an empty `eval_result_ids` array returns 422
- [ ] Request with non-existent agent returns 404
- [ ] Request with eval result IDs that resolve to zero results returns 404
- [ ] Optimization uses the agent's `model_backend_id` by default
- [ ] Optimization can override the model backend via `model_backend_id` in the request body
- [ ] Optimization requires encrypted credential retrieval for the model backend
- [ ] Fails with 500 if model backend credentials cannot be decrypted
- [ ] Response includes `suggested_prompt`, `rationale`, `analysis`, and `version` (next version label)
- [ ] The optimizer builds a failure context from eval results and eval definitions
- [ ] Evaluation results are annotated with eval definition name, type, config, score, and detail
- [ ] Missing eval definitions are shown as `unknown` in the failure context
- [ ] Empty eval results produce an empty `<failing_evals>[]</failing_evals>` section
- [ ] The optimizer sends a system prompt (instructions) + human message (context) to the LLM
- [ ] LLM response is parsed as plain JSON
- [ ] LLM response wrapped in a markdown code fence (with or without `json` language tag) is parsed correctly
- [ ] Malformed LLM response (bad JSON) raises a JSON decode error
- [ ] LLM response missing required keys (`suggested_prompt`, `rationale`) raises a KeyError
- [ ] LLM call failures (network, timeout) propagate to the caller ### Apply Optimized Prompt
- [ ] `POST /agents/{id}/prompts/{version}/apply` accepts `suggested_prompt`, `rationale`, `optimize_version`, `eval_result_ids`
- [ ] Creates a new version entry linking back to the optimized-from version
- [ ] Returns the updated agent with the new prompt template
- [ ] Non-existent agent returns 404
- [ ] Empty `suggested_prompt` returns 422
- [ ] Apply failure (DB error) returns 404 ### Pipeline Integration
- [ ] Variant groups compare `prompt_version_hash` between base and variant snapshots
- [ ] Prompt version pinning is independent of pipeline snapshot versioning — prompts can be rolled back without a new snapshot
- [ ] Model backend `model_id` is also pinned in the snapshot (`model_backend_pins_json`) — consistent with prompt pinning ## Known Gaps - BDD test coverage exists for basic prompt versioning (CRUD, snapshot pinning) but not for the optimize/apply endpoints or diff
- No BDD coverage for variant group prompt hash comparison
- No BDD coverage for rollback
- The `prompt_versioning.feature` BDD uses mocked backends and does not exercise real DB state transitions
- No negative BDD scenarios (e.g. version not found, unauthorized access to prompt history)
- No performance or regression tests for large version histories (100+ entries) 