---
id: feat-core-run-retention
prd: 7.11
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/operations/run_retention.feature
unit-tests:
  - backend/tests/unit/cleanup_jobs/test_run_retention_cleanup.py
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
