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
  - frontend/src/views/AdminRunRetentionView.vue
depends-on: [feat-core-pipeline-execution]
status: covered
---

# Run Retention

Automatic cleanup of terminal-state runs after a configurable retention period (default 90 days).

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
- **`batch_delete_old_terminal_runs` missing `eval_failed`**: The CRUD function in `backend/src/modulo/db/crud/run.py` only filters `["complete", "failed", "cancelled"]` but the cleanup job `run_retention_cleanup.py` includes `eval_failed`. Inconsistent terminal state definition.
- **`completed_at` vs `created_at`**: `batch_delete_old_terminal_runs` uses `Run.completed_at` but `cleanup_old_runs` in `run_retention_cleanup.py` uses `Run.created_at`. Both should agree on which timestamp drives the cutoff.
- **Error handling gaps**: Five admin route functions (`admin_retention_purge_runs`, `admin_manual_purge`, `admin_purge_stale_runs`, `admin_get_retention`, `admin_update_retention`) were missing the full error-handling chain: `asyncio.CancelledError` re-raise guard, `IntegrityError`→409, `SQLAlchemyError`→503, `Exception`→500 with `logger.exception`.
- **Background loop lacks org scope**: `_run_retention_loop` in `main.py` calls `batch_delete_old_terminal_runs` without any RLS org context — it deletes runs across all orgs. Should accept an org list or be documented as a global admin-only job.
