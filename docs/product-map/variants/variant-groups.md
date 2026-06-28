---
id: feat-variants-variant-groups
prd: 8.19
delivery-tasks: [task-nv3-variant-group]
  - backend/tests/bdd/features/pipelines/run_variants.feature
code:
  - backend/src/modulo/api/routes/variants.py
  - backend/src/modulo/db/models/variant_group.py
  - backend/src/modulo/db/crud/variant_group.py
  - backend/src/modulo/db/migrations/versions/0012_variant_groups.py
unit-tests:
  - backend/tests/unit/api/test_variants.py
  - backend/tests/unit/db/crud/test_variant_group.py
  - backend/tests/integration/crud/test_variant_group.py
depends-on: [feat-core-run-context, feat-evals-eval-engine]
status: partial
---
# Variant Groups A/B test variant management — named sets of runs against the same pipeline that differ only in `run_context_overrides`. ## Behaviours ### CRUD operations
- [ ] Create a variant group with name, description, pipeline_id, and 2+ variant definitions (each with snapshot_id, name, weight, run_context_overrides, eval_definition_ids)
- [ ] Create a variant group with optional `selection_strategy` (default "weighted"), `max_concurrent_runs` (default 5), `degraded_evals` (default false)
- [ ] List variant groups with pagination (page, page_size, max 100), ordered by created_at desc
- [ ] List variant groups filtered by pipeline_id
- [ ] Get a variant group by ID
- [ ] Update a variant group — change name, description, variants, selection_strategy, max_concurrent_runs, degraded_evals
- [ ] Delete a variant group by ID
- [ ] RLS org context set on every CRUD endpoint via `set_rls_org` ### Variant selection
- [ ] Weighted random selection picks a variant proportionally to each variant's weight
- [ ] Single-variant group short-circuits — returns the only variant directly (no random call)
- [ ] Empty variants list returns None
- [ ] All-zero weights fall back to uniform random selection
- [ ] Missing weight key defaults to 1.0
- [ ] Selection strategy constrained to `'weighted'` or `'single'` (DB CHECK constraint) ### Running variants
- [ ] `POST /{group_id}/run` selects a variant via weighted random, merges `run_context_overrides` into `input_payload`, creates a run with the variant's `snapshot_id`, and returns `{run_id, variant_name, merged_payload}`
- [ ] `snapshot_id` accepted as both `str` and `uuid.UUID` in variant definitions
- [ ] `degraded_evals=true` injects `_degraded_evals: True` into merged payload
- [ ] Run count incremented on the variant group after each successful run
- [ ] `trigger_type` defaults to `"manual"` for variant-triggered runs
- [ ] 404 if variant group not found when running
- [ ] 429 if pipeline concurrent run quota exceeded (`check_pipeline_run_quota`)
- [ ] 429 if no variant selected or quota exceeded in `run_variant_weighted` ### Coverage gap detection
- [ ] `GET /{group_id}/coverage-gaps` returns variants whose `eval_definition_ids` don't cover all eval definitions for the pipeline
- [ ] Eval definitions loaded from `EvalDefinition` table filtered by pipeline_id
- [ ] Empty eval definitions list for pipeline → no gaps reported
- [ ] All evals present in variant's `eval_definition_ids` → no gap reported
- [ ] 404 if variant group not found
- [ ] RLS enforced ### Prompt diff comparison
- [ ] `GET /{group_id}/prompt-diffs` compares `prompt_pins_json` across variant snapshots
- [ ] Returns agent-level diffs: `{agent_id, base_hash, variant_hash}` when hashes differ
- [ ] Handles `base_snapshot_ids` parameter for explicitly marking base vs comparison variants
- [ ] Missing snapshots are skipped (not a hard error)
- [ ] No snapshots or no variants → returns empty list
- [ ] 404 if variant group not found
- [ ] RLS enforced ### Error handling
- [ ] 404 on GET/PUT/DELETE for unknown group_id
- [ ] 204 No Content on successful DELETE
- [ ] 404 on run_variant when group not found
- [ ] 429 on run_variant when quota exceeded
- [ ] ForeignKey `RESTRICT` on pipeline deletion (pipeline with variant groups cannot be deleted)
- [ ] Check constraint enforces `selection_strategy IN ('weighted', 'single')` ### Pipeline limits
- [ ] Concurrent run limit enforced per pipeline per variant group via `check_pipeline_run_quota` (`active < max_concurrent_runs`) ## Missing implementations (gaps relative to PRD 8.19)
- [ ] Batch run: PRD specifies "fires one run per variant" — current code fires only one run per API call, not N variants
- [ ] Comparison view: no frontend, no endpoint for side-by-side eval scores / token cost / HITL outcomes / per-node output diff
- [ ] Eval coverage signal warning: `coverage-gaps` endpoint exists but no UI surfaces the "Variants diverged but evals did not differentiate" warning
- [ ] HITL partial completion: no handling for one variant reaching `awaiting_human` while others complete
- [ ] Cancel/abandon variant: no endpoint or status for marking a variant run as abandoned and excluding from aggregates
- [ ] All-or-nothing pre-flight quota: PRD says check all N variants before firing any — current code checks per-run only
- [ ] Prompt versioning library guide: `get_prompt_diffs` exists but documented library pattern for prompt versioning does not
- [ ] BDD feature file: placeholders only — no real Gherkin scenarios ## Test coverage gaps
- [ ] No unit tests for `run_variant_weighted`
- [ ] No unit tests for `get_prompt_diffs`
- [ ] No unit tests for `increment_run_count` ## Known Gaps
- No frontend exists for variant group creation, comparison view, or coverage signal
- BDD scenarios not yet written
- No all-or-nothing N-variant quota pre-flight
- No HITL partial completion handling 