---
id: feat-core-run-retention
prd: N/A
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/operations/run_retention.feature
unit-tests:
  - backend/tests/unit/cleanup_jobs/test_run_retention_cleanup.py
  - backend/tests/unit/api/test_run_retention_bdd.py
code:
  - backend/src/modulo/core/cleanup_jobs/run_retention_cleanup.py
  - backend/src/modulo/core/cleanup_jobs/payload_cleanup.py
  - backend/src/modulo/api/routes/admin.py
  - frontend/src/views/AdminRunRetentionView.vue
depends-on: [feat-core-pipeline-execution]
status: partial
---

# Run Retention

Automatic cleanup of terminal-state runs after a configurable retention period (default 90 days).
Routes live in `backend/src/modulo/api/routes/admin.py` (not `admin_run_retention.py`).

## Behaviours

### Cleanup Job

- [x] Deletes runs in terminal states (complete, failed, eval_failed, cancelled)
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

## Known Gaps

- **PRD section**: Run retention has no dedicated PRD section. Reference `7.11` pointed to "GitHub Connector OAuth Scopes", which was incorrect. Removed.
- **Background loop lacks org scope**: `_run_retention_loop` in `main.py` calls `batch_delete_old_terminal_runs` without any RLS org context — it deletes runs across all orgs. Should accept an org list or be documented as a global admin-only job.

## Resolved Gaps

- [x] ~~**`batch_delete_old_terminal_runs` missing `eval_failed`**: The CRUD function now includes `eval_failed` (`["complete", "failed", "eval_failed", "cancelled"]`). Docstring was stale — fixed in this round.~~
- [x] ~~**`completed_at` vs `created_at`**: Both `cleanup_old_runs` and `batch_delete_old_terminal_runs` use `Run.created_at`. `purge_runs` uses `Run.completed_at` but that is a separate manual-purge function.~~
- [x] ~~**Error handling gaps**: All 5 admin route functions (`admin_retention_purge_runs`, `admin_manual_purge`, `admin_purge_stale_runs`, `admin_get_retention`, `admin_update_retention`) now have the complete error-handling chain: `asyncio.CancelledError` re-raise guard, `IntegrityError`→409, `ProgrammingError`→501, `SQLAlchemyError`→503, `Exception`→500 with `logger.exception`.~~

## QA History

### 2026-07-12 — Round 3 QA

- **Fixed (MAJOR):** Stale code frontmatter — `code:` referenced non-existent `admin_run_retention.py`. Actual routes are in `admin.py`.
- **Fixed (MAJOR):** Stale docstring in `batch_delete_old_terminal_runs` — listed `(complete, failed, cancelled)` but code includes `eval_failed`.
- **Fixed (MINOR):** Marked 3 resolved known gaps (eval_failed inclusion, completed_at/created_at sync, error handling chains) to match current code.
