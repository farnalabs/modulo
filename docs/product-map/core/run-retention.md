---
id: feat-core-run-retention
prd: 7.10
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/operations/run_retention.feature
unit-tests:
  - backend/tests/bdd/steps/test_run_retention.py
code:
  - backend/src/modulo/api/main.py
  - backend/src/modulo/db/crud/run.py
  - backend/src/modulo/api/routes/admin.py
  - frontend/src/views/AdminRunRetentionView.vue
depends-on: [feat-core-pipeline-execution]
status: partial
---

# Run Retention

Automatic cleanup of terminal-state runs after a configurable retention period (default 90 days).
Routes live in `backend/src/modulo/api/routes/admin.py` (not `admin_run_retention.py`).

The purge deletes `runs` rows only. **`run_daily_facts` is exempt** (no FK from
`run_daily_facts.run_id` to `runs`, ADR 020): the facts table is retained **13 months**
(config-driven via `analytics_facts_retention_months`), deleted in **chunked day-slices**
(7-day chunks) by the analytics maintenance cron's `retention_facts` step — see
`docs/product-map/core/analytics.md` and PRD §8.32.

## Behaviours

### Cleanup Job

- [x] Deletes runs in terminal states (complete, failed, eval_failed, cancelled, stalled — the `TERMINAL_STATUSES` set)
- [x] Batch deletion (500 at a time)
- [x] Configurable retention period (org-level config)
- [x] Active runs are preserved

### BDD Coverage

- [x] Auto-delete after TTL
- [x] Active run preservation
- [x] Batch deletion with locking
- [x] Configurable retention period

### Frontend

- [x] AdminRunRetentionView.vue — admin configuration UI

## Error Handling

- [x] All 5 admin route functions have complete error-handling chain: `asyncio.CancelledError` re-raise guard, `IntegrityError` → 409, `ProgrammingError` → 501, `SQLAlchemyError` → 503, `Exception` → 500 with `logger.exception`
- [x] Background cleanup loop catches and logs exceptions to prevent crash termination
- [x] Batch deletion with locking prevents concurrent cleanup contention
- [ ] Background cleanup loop failures silently logged — no alerting

## Edge Cases

- [x] Active runs are preserved (not deleted)
- [x] Batch size caps deletion at 500 per cycle
- [x] Configurable retention period per org
- [x] `eval_failed` status included in terminal state list (fixed per QA history)
- [ ] Background loop runs without any org context — global deletion, not org-scoped
- [ ] Concurrent admin retention config changes while cleanup is running
- [ ] Retention period set to 0 (delete immediately) — not validated

## Security

- [x] Admin-only routes (operator role required)
- [ ] Background cleanup bypasses RLS — runs as system, not as any org
- [ ] No audit logging for auto-deleted runs

## Known Gaps

- [x] ~~**`batch_delete_old_terminal_runs` missing `eval_failed`**: The CRUD function now includes `eval_failed` (`["complete", "failed", "eval_failed", "cancelled"]`). Docstring was stale — fixed in this round.~~
- [x] ~~**`completed_at` vs `created_at`**: Both `cleanup_old_runs` and `batch_delete_old_terminal_runs` use `Run.created_at`. `purge_runs` uses `Run.completed_at` but that is a separate manual-purge function.~~
- [x] ~~**Error handling gaps**: All 5 admin route functions (`admin_retention_purge_runs`, `admin_manual_purge`, `admin_purge_stale_runs`, `admin_get_retention`, `admin_update_retention`) now have the complete error-handling chain: `asyncio.CancelledError` re-raise guard, `IntegrityError`→409, `ProgrammingError`→501, `SQLAlchemyError`→503, `Exception`→500 with `logger.exception`.~~

## QA History

### 2026-07-12 — Round 3 QA

- **Fixed (MAJOR):** Stale code frontmatter — `code:` referenced non-existent `admin_run_retention.py`. Actual routes are in `admin.py`.
- **Fixed (MAJOR):** Stale docstring in `batch_delete_old_terminal_runs` — listed `(complete, failed, cancelled)` but code includes `eval_failed`.
- **Fixed (MINOR):** Marked 3 resolved known gaps (eval_failed inclusion, completed_at/created_at sync, error handling chains) to match current code.

### 2026-07-31 — improve-architecture (product-map walk)

- Fixed stale CODE refs: cleanup jobs moved from `core/cleanup_jobs/` into `api/main.py` (`_run_retention_loop`) + `db/crud/run.py` (`batch_delete_old_terminal_runs`). Unit-test refs → `bdd/steps/test_run_retention.py` (only surviving test file).
