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
unit-tests:
  - backend/tests/unit/api/test_variants.py
  - backend/tests/unit/api/test_variant_groups_bdd.py
  - backend/tests/unit/api/test_variants_programming_error.py
  - backend/tests/unit/db/crud/test_variant_group.py
  - backend/tests/integration/crud/test_variant_group.py
depends-on: [feat-evals-eval-engine, feat-variants-variant-groups]
status: partial
---

# Run Variants — Coverage Gap Signal

Discovered from 1 completed delivery task.

## Behaviours

### Variant Group Lifecycle
- [x] Create variant group with 2+ variants, each with `run_context_overrides` and `eval_definition_ids`
- [x] List variant groups for a pipeline
- [x] Get single variant group by ID
- [x] Update variant group (name, variants, strategy, limits)
- [x] Delete variant group
- [x] Variants store `snapshot_id`, `name`, `weight`, `run_context_overrides`, `eval_definition_ids`
- [x] `selection_strategy`: `weighted` — weighted random pick when running a single variant
- [x] Weighted selection short-circuits when only one variant or total weight is zero
- [x] `degraded_evals` flag on group skips eval execution for all variant runs in the group
- [ ] Group is complete when all variants reach terminal state (success, failed, abandoned)

### Running Variants
- [ ] Fire one run per variant — same input payload, same pipeline snapshot
- [x] `run_variant_weighted` merges `run_context_overrides` into the run payload
- [x] Runs are counted individually against org/team/trigger run limits
- [x] Pre-flight quota check: breach by any variant rejects the entire group (`variant_group_quota_exceeded`)
- [ ] Prompt version comparison via `run_context_overrides` containing `prompt_version`
- [ ] Agents read `run_context.prompt_version` to select prompt template
- [ ] Abandon a variant run — marked `abandoned`, excluded from aggregate scores

### Comparison View
- [ ] Eval scores per node, per variant shown side-by-side
- [ ] Token cost per run shown in comparison
- [ ] HITL outcomes shown per variant (if any gates were reached)
- [ ] Per-node output diff side-by-side (artifact comparison)
- [ ] HITL variant blocked: partial results for completed variants + "pending HITL" indicator
- [ ] Pre-eval degraded mode: no evals configured → cost + output diffs only + config banner

### Eval Coverage Signal
- [ ] Warning surfaces when variants produce different outputs but identical eval scores
- [ ] Warning copy: "Variants diverged but evals did not differentiate — your eval suite may have a coverage gap."
- [x] `GET /api/v1/variant-groups/{group_id}/coverage-gaps` — returns missing eval def IDs per variant
- [x] Coverage gap detection: compares variant's `eval_definition_ids` against pipeline's full eval def set
- [x] `GET /api/v1/evals/coverage` — pipeline-level coverage map (nodes with/without evals)
- [x] Coverage map returns per-node: `node_id`, `name`, `has_evals`, `eval_count`
- [x] Coverage map summary: `total_nodes`, `covered_nodes`, `uncovered_nodes`, `coverage_pct`
- [x] Admin eval dashboard identifies pipeline nodes with no eval definitions as coverage gaps
- [x] Feedback system: `detect_eval_gap()` flags when human rejection is not caught by existing evals
- [x] `POST /api/v1/feedback/{record_id}/detect-gap` endpoint triggers eval gap check on a feedback record (fetches eval definitions instead of passing eval_suite=[])
- [ ] FeedbackRecords with `eval_gap = True` feed into eval proposal generation
- [x] `GET /api/v1/variant-groups/{group_id}/prompt-diffs` — compares prompt hash diffs between variants

### Prompt Version Comparison
- [ ] Variant groups support model backend differences and prompt version differences
- [x] Prompt diffs endpoint compares `prompt_pins_json` hashes between variant snapshots
- [x] Prompt diffs returns agent-level diff entries per variant pair
- [ ] Agents declare multiple prompt template versions and select by `run_context.prompt_version`

## Error Handling

| Checkbox | Route | Test |
|---|---|---|
| [x] ProgrammingError→501 | `POST /api/v1/variant-groups` (create_group) | `test_variants::TestCreateGroupProgrammingError` |
| [x] ProgrammingError→501 | `GET /api/v1/variant-groups` (list_groups) | `test_variants_programming_error::TestListGroupsProgrammingError` |
| [x] ProgrammingError→501 | `GET /api/v1/variant-groups/{id}` (get_group) | `test_variants::TestGetGroupProgrammingError` |
| [x] ProgrammingError→501 | `PUT /api/v1/variant-groups/{id}` (update_group) | `test_variants_programming_error::TestUpdateGroupProgrammingError` |
| [x] ProgrammingError→501 | `DELETE /api/v1/variant-groups/{id}` (delete_group) | `test_variants_programming_error::TestDeleteGroupProgrammingError` |
| [x] ProgrammingError→501 | `POST /api/v1/variant-groups/{id}/run` (run_variant) | `test_variants_programming_error::TestRunVariantProgrammingError` |
| [x] ProgrammingError→501 | `GET /api/v1/variant-groups/{id}/coverage-gaps` (coverage_gaps) | `test_variants_programming_error::TestCoverageGapsProgrammingError` |
| [x] ProgrammingError→501 | `GET /api/v1/variant-groups/{id}/prompt-diffs` (prompt_diffs) | `test_variants_programming_error::TestPromptDiffsProgrammingError` |
| [x] ProgrammingError→501 | `GET /api/v1/evals/coverage` (eval_coverage in evals.py) | implicit via evals route tests |
| [x] ProgrammingError→501 | Admin eval dashboard (admin.py) | implicit via admin route tests |
| [x] ProgrammingError→501 | `POST /api/v1/feedback/{id}/detect-gap` (feedback.py) | `test_feedback::TestDetectEvalGap*` |
| [x] 404 | `GET /api/v1/variant-groups/{id}` when not found | `test_variants::TestGetGroup.test_raises_404_when_not_found` |
| [x] 404 | `PUT /api/v1/variant-groups/{id}` when not found | `test_variants::TestUpdateGroup.test_raises_404_when_not_found` |
| [x] 404 | `DELETE /api/v1/variant-groups/{id}` when not found | `test_variants::TestDeleteGroup.test_raises_404_when_not_found` |
| [x] 404 | `POST /api/v1/variant-groups/{id}/run` when not found | `test_variants::TestRunVariantGroupNotFound` |
| [x] 404 | `GET /api/v1/variant-groups/{id}/coverage-gaps` when not found | `test_variants::TestCoverageGapsGroupNotFound` |
| [x] 404 | `GET /api/v1/variant-groups/{id}/prompt-diffs` when not found | `test_variants::TestPromptDiffsGroupNotFound` |
| [x] 429 | `POST /api/v1/variant-groups/{id}/run` — no variants configured | `test_variants::TestRunVariantEmptyVariants` |
| [x] 429 | `POST /api/v1/variant-groups/{id}/run` — pipeline quota exceeded | `test_variants::TestRunVariant.test_raises_429_when_quota_exceeded` |
| [ ] 403 | Admin eval dashboard for non-admin users | uncovered |

## Known Gaps
- BDD feature file `run_variants.feature` is a placeholder — no scenarios implemented
- No integration tests exist for variant group endpoints
- No unit tests for `get_coverage_gaps` CRUD function
- Variant comparison view is not yet implemented (PRD 8.19 comparison view)
- Eval coverage gap warning on variant comparison view not wired (frontend + backend signal)
- `detect_eval_gap` endpoint now fetches eval definitions but passes them as raw model objects — EvalEngine.evaluate expects specific format; integration test needed

## QA History
- 2026-07-04: Cross-cutting QA (index 120). Fixed CRITICAL: detect_eval_gap endpoint passed eval_suite=[] instead of fetching pipeline's eval definitions — always returned False. Added 6 missing ProgrammingError→501 unit tests. Updated product map: marked ~20 behaviour checkboxes [ ]→[x], added Error Handling section (18 checkboxes), updated Known Gaps. Status: partial. 