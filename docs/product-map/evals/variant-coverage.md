---
id: feat-evals-variant-coverage
prd: 8.19
delivery-tasks: [task-nv3-eval-coverage-signal]
bdd:
  - backend/tests/bdd/features/pipelines/run_variants.feature
  - backend/tests/bdd/features/variants/variant_groups.feature
code:
  - backend/src/modulo/api/routes/variants.py
  - backend/src/modulo/db/crud/variant_group.py
  - backend/src/modulo/db/models/variant_group.py
  - backend/src/modulo/api/routes/evals.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/core/feedback_manager/__init__.py
depends-on: [feat-evals-eval-engine, feat-variants-variant-groups]
status: partial
---
# Run Variants — Coverage Gap Signal Discovered from 1 completed delivery tasks. ## Behaviours ### Variant Group Lifecycle
- [ ] Create variant group with 2+ variants, each with `run_context_overrides` and `eval_definition_ids`
- [ ] List variant groups for a pipeline
- [ ] Get single variant group by ID
- [ ] Update variant group (name, variants, strategy, limits)
- [ ] Delete variant group
- [ ] Variants store `snapshot_id`, `name`, `weight`, `run_context_overrides`, `eval_definition_ids`
- [ ] `selection_strategy`: `weighted` — weighted random pick when running a single variant
- [ ] Weighted selection short-circuits when only one variant or total weight is zero
- [ ] `degraded_evals` flag on group skips eval execution for all variant runs in the group
- [ ] Group is complete when all variants reach terminal state (success, failed, abandoned) ### Running Variants
- [ ] Fire one run per variant — same input payload, same pipeline snapshot
- [ ] `run_variant_weighted` merges `run_context_overrides` into the run payload
- [ ] Runs are counted individually against org/team/trigger run limits
- [ ] Pre-flight quota check: breach by any variant rejects the entire group (`variant_group_quota_exceeded`)
- [ ] Prompt version comparison via `run_context_overrides` containing `prompt_version`
- [ ] Agents read `run_context.prompt_version` to select prompt template
- [ ] Abandon a variant run — marked `abandoned`, excluded from aggregate scores ### Comparison View
- [ ] Eval scores per node, per variant shown side-by-side
- [ ] Token cost per run shown in comparison
- [ ] HITL outcomes shown per variant (if any gates were reached)
- [ ] Per-node output diff side-by-side (artifact comparison)
- [ ] HITL variant blocked: partial results for completed variants + "pending HITL" indicator
- [ ] Pre-eval degraded mode: no evals configured → cost + output diffs only + config banner ### Eval Coverage Signal
- [ ] Warning surfaces when variants produce different outputs but identical eval scores
- [ ] Warning copy: "Variants diverged but evals did not differentiate — your eval suite may have a coverage gap."
- [ ] `GET /api/v1/variant-groups/{group_id}/coverage-gaps` — returns missing eval def IDs per variant
- [ ] Coverage gap detection: compares variant's `eval_definition_ids` against pipeline's full eval def set
- [ ] `GET /api/v1/evals/coverage` — pipeline-level coverage map (nodes with/without evals)
- [ ] Coverage map returns per-node: `node_id`, `name`, `has_evals`, `eval_count`
- [ ] Coverage map summary: `total_nodes`, `covered_nodes`, `uncovered_nodes`, `coverage_pct`
- [ ] Admin eval dashboard identifies pipeline nodes with no eval definitions as coverage gaps
- [x] Feedback system: `detect_eval_gap()` flags when human rejection is not caught by existing evals
- [ ] `POST /api/v1/feedback/{record_id}/detect-gap` endpoint triggers eval gap check on a feedback record
- [ ] FeedbackRecords with `eval_gap = True` feed into eval proposal generation
- [ ] `GET /api/v1/variant-groups/{group_id}/prompt-diffs` — compares prompt hash diffs between variants ### Prompt Version Comparison
- [ ] Variant groups support model backend differences and prompt version differences
- [ ] Prompt diffs endpoint compares `prompt_pins_json` hashes between variant snapshots
- [ ] Prompt diffs returns agent-level diff entries per variant pair
- [ ] Agents declare multiple prompt template versions and select by `run_context.prompt_version` ## Known Gaps
- BDD feature file `run_variants.feature` is a placeholder — no scenarios implemented
- No integration tests exist for variant group endpoints
- No unit tests for `get_coverage_gaps` CRUD function
- Variant comparison view is not yet implemented (PRD 8.19 comparison view)
- Eval coverage gap warning on variant comparison view not wired (frontend + backend signal)
- Feedback system `detect_eval_gap()` logic is implemented — but the API endpoint does not fetch the pipeline's eval definitions, so eval_suite=[] is always passed 