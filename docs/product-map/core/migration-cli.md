---
id: feat-core-migration-cli
prd: 6.2
delivery-tasks: [task-nv9-migration-cli]
code:
  - backend/src/modulo/cli/migrate.py
  - backend/src/modulo/cli/migrate_org.py
bdd: []
depends-on: [feat-core-db-abstraction-core]
unit-tests:
  - backend/tests/unit/cli/test_migrate.py
  - backend/tests/unit/cli/test_migrate_org.py
status: partial
---

# Migration CLI

Two implementations exist: `modulo-migrate` (click-based, JSONL format with auth) and `modulo` (argparse-based, JSON format, no auth). Both implement `export-org` / `import-org`.

## Behaviours

### modulo-migrate (click-based, JSONL, authenticated)

- [x] `export-org` exports all org data (users, pipelines, runs, audit events, library primitives, connector instances, model backends) as JSONL
- [x] Auth via `--token` flag, `MODULO_ADMIN_TOKEN` env var, or `MODULO_ADMIN_SECRET` env var
- [x] Non-admin JWT rejected with clear error
- [x] User not in target org rejected
- [x] `--pipelines-only` flag restricts export to pipelines
- [x] `--users-only` flag restricts export to users
- [x] Each record has `__table__`, `id`, `data`, `__hash__` fields
- [x] Metadata header line contains version, export timestamp, aggregate SHA-256 hash
- [x] `verify-export` re-computes per-table hashes and compares to stored hash
- [x] Hash mismatch on verify prints per-table hash comparison and exits non-zero
- [x] `import-org` reads JSONL, verifies hash, imports records
- [x] Hash mismatch on import aborts with error
- [x] `--on-conflict skip` leaves existing records untouched (default)
- [x] `--on-conflict overwrite` replaces existing records fully
- [x] `--on-conflict merge` only fills null/empty fields on existing records
- [x] `--pipelines-only` / `--users-only` flags available on import
- [x] Progress bars via tqdm on all long-running operations
- [x] Summary output: created/skipped/overwritten/errors counts
- [x] Org not found returns error with "not found" message
- [x] UUID columns serialised as strings
- [x] Datetime columns serialised as ISO 8601
- [x] Binary / blob columns serialised as hex
- [x] Output directory created if missing
- [x] Empty database tables handled without crash
- [x] Non-existent input file returns error

### Checked after QA pass (2026-07-05)

- [x] FileNotFoundError on import file → graceful SystemExit (both CLIs handle via Click path validation + _read_jsonl / _load_bundle guards)
- [x] DB connection failure → clear error message (both CLIs now wrap async session in try/except)
- [x] session.rollback() inside import loop → removed harmful rollback; now lets session context manager handle cleanup
- [x] Auth check before file read (migrate.py import-org now verifies auth before reading input file)
- [x] Hash verification during import (migrate.py now verifies export hash before importing)
- [x] Existing output file returns error without `--force` (migrate_org.py added --force flag)

### modulo export-org / import-org (argparse-based, JSON, unauthenticated)

- [x] `export-org --org-id --output` exports org data as single JSON file
- [x] Export includes org entity + users, teams, stages, schemas, schema_versions, model_backends, library_primitives, connector_instances, agents, pipelines, runs
- [x] Export includes SHA-256 bundle hash for integrity verification
- [x] Org not found returns error message
- [x] Existing output file returns error without `--force` (added --force flag)
- [x] `import-org --file` reads JSON file and bulk-imports records
- [x] Import skips existing records (idempotent)
- [x] Import creates missing org automatically (upsert)
- [x] Non-existent input file returns error (handled by _load_bundle path.exists() check)

### Edge cases

- [x] Export with zero records in a table produces empty array for that table
- [x] Import into org with partially overlapping data skips existing records
- [x] Network/auth failure during modulo-migrate export produces non-zero exit
- [x] Binary/blob data in table columns serialized as hex strings
- [x] Empty output directory creates it before writing
- [x] Import bundle root is not a JSON object → rejected with a structural error (no AttributeError crash in `_verify_hash`)
- [x] Import bundle missing `__meta__` / `organisation` → rejected with a structural error
- [x] Import entity table is not an array (e.g. a dict/string) → rejected with a structural error
- [x] Import entity row is not a JSON object → rejected with a structural error
- [x] Import entity row `id` is not a string (null accepted — row re-created fresh) → rejected with a structural error
- [x] Structural validation runs before hash verification — corrupt input reports the shape problem, not a hash mismatch
- [x] Multiple structural problems accumulated into one error message (all reported, not fail-fast on the first)
- [x] Unknown top-level bundle keys ignored (not treated as tables)
- [x] Interrupted export: partial output file left on disk (both CLIs) — fixed for migrate.py with try/finally cleanup in _async_export_org

### Resilience (QA pass 2026-07-05)

- [x] Auth verified before input file is read — prevents unauthenticated file probing
- [x] Export hash verified before import begins — detection of corrupted file
- [x] DB connection failure produces clear error message instead of raw stack trace
- [x] Flush failure no longer calls `session.rollback()` — avoids nested transaction corruption
- [x] Existing output file protected unless `--force` is passed

## Known Gaps

- No BDD feature files for migration/export behaviour
- modulo-migrate requires auth token or admin secret — no interactive login
- modulo (argparse) has no auth — runs with direct DB access
- No import conflict resolution for audit events (append-only constraint)
- ~~No data validation before import — corrupt JSONL/JSON is accepted~~ — **RESOLVED (2026-08-13)**: `_validate_bundle()` in `migrate_org.py` structurally validates an import bundle before any import work — non-object root, missing `__meta__`/`organisation`, entity tables that aren't arrays, rows that aren't objects, and non-string row `id`s are all rejected with a combined, human-readable error (null `id` accepted — the row is re-created fresh). Wired into `_load_bundle` ahead of hash verification so corrupt input reports the shape problem first. This also fixes a latent crash: a JSON-array root file previously raised `AttributeError` inside `_verify_hash` (`list` has no `.get`). Unit tests: `TestValidateBundle` (10 cases incl. valid, empty, multi-error accumulation, unknown-key tolerance, non-object root) + 5 new `TestLoadBundle` corrupt-file cases.
- No integration tests for full export→verify→import cycle
- No concurrency tests (parallel export/import, partial failure during import)
- No tests for orgs with 500+ records (pagination boundary for migrate_org.py) — unit-level pagination boundary now covered (2026-08-13)
- migrate.py loads all rows in memory (`session.execute(query)).scalars().all()`) — OOM risk on very large orgs; migrate_org.py paginates safely (2026-08-15 verified)
- No timeout on DB operations in either CLI — a slow/unresponsive DB blocks indefinitely
- SHA-256 hash collision during import is not handled — theoretical (cryptographically infeasible) and both CLIs would treat a collided bundle as valid

## QA History

### 2026-08-13 — improve-architecture (data-validation-before-import gap → resolved)

- **RESOLVED the "No data validation before import — corrupt JSONL/JSON is accepted" known gap** (`cli/migrate_org.py`). `_load_bundle` verified the bundle hash but said nothing about shape — a hand-edited file carrying a recomputed hash could smuggle malformed tables/rows into the import, which then failed unpredictably mid-import (iterating a dict's keys as rows) or crashed outright.
- (1) New `_validate_bundle(bundle)` returns a list of structural problems (empty = well-formed): root must be a JSON object (a JSON-array root previously crashed with `AttributeError` inside `_verify_hash`); `__meta__` and `organisation` must be objects; every `ENTITY_ORDER` table present must be an array of objects; each row `id` must be a string or null (null rows are re-created fresh). All problems are accumulated into one report rather than failing on the first. (2) Wired into `_load_bundle` before hash verification — corrupt input now reports the shape problem first, and the "hash verification failed" path is reserved for genuinely tampered-but-well-formed files. (3) Unknown top-level keys are ignored.
- Added 10 `TestValidateBundle` cases (valid, empty, non-object root, missing meta/organisation, non-array table, non-object row, non-string id, null-id accepted, multi-error accumulation, unknown-key tolerance) + 5 new `TestLoadBundle` corrupt-file cases (array root, hash-consistent-but-corrupt table, corrupt row id, structural-error-precedes-hash-mismatch). 74/74 `test_migrate_org.py` + 284/284 CLI unit tests pass, ruff check + format clean, mypy --strict clean on src+tests.
- Updated product map: 8 edge-case behaviours `[ ]`→`[x]` (incl. the latent JSON-array-root crash), Known Gap → RESOLVED, QA History. Status: partial (audit-event append-only conflicts, no interactive login for modulo-migrate, no integration/export→verify→import tests, no concurrency tests, OOM on large orgs in migrate.py, DB timeouts, hash collision remain).

### 2026-08-13 — improve-tests QA lens pass on migrate_org CLI test package

- Raised `migrate_org.py` line coverage 54% → 100% (+21 tests, 38 → 59).
- Added tests for the previously-uncovered DB layer: `_export_entity` (batch + pagination boundary + empty table), `_export_organisation` (found/not found), `_do_export` (bundle/hash structure, DB-connection failure, cancellation propagation), `_do_import` (create, skip, overwrite, rename, rename-exhaustion, per-row error counting, DB failure, cancellation propagation), `_write_bundle` OSError branch, and the `__main__` guard.
- Fixed latent `AttributeError: type object 'Account' has no attribute 'organisation_id'`: `_export_entity` / `_do_import` unconditionally referenced `model_cls.organisation_id`, but the `users` entity (`Account`) is org-scoped via `OrgMembership`, not a column. Export/import of users crashed. Guarded with `hasattr(model_cls, "organisation_id")`, mirroring the existing click-based `migrate.py`.
- `migrate_org.py` now passes ruff + mypy strict; `test_migrate_org.py` mypy-clean too.

### 2026-07-XX — Cross-cutting QA (index 308)

**MAJOR fixes applied:**
- Created `backend/tests/unit/cli/test_migrate_org.py` with 10 unit tests covering export-org (basic, not-found, existing-file-without-force, existing-file-with-force), import-org (basic, file-not-found, hash-mismatch, skip-existing), and parsing edge cases (valid UUID, invalid UUID). Previously migrate_org.py had zero unit tests.
- Fixed partial-output-file leak: `_async_export_org` now removes the output file on failure via try/finally cleanup.
- Fixed `_read_jsonl` ASYNC240 linter violation: replaced `os.path.exists(str(path))` with `path.exists()`.
- Removed 2 dead-code lines (`_ = sum(...)`) from `cmd_export` and `cmd_import` in migrate_org.py.
- Changed `_log.warning` to `_log.exception` in migrate.py import row loop to capture tracebacks on import errors.
- Added TODO comments for known limitations (OOM on large orgs in migrate.py, no DB timeout) matching unchecked edge cases in product map.

**Status:** partial (8 known gaps remain — 1 partially resolved by new tests)

### 2026-08-15 — coverage sweep (partial-small-a)

- Verified the 3 unchecked edge-case behaviours are genuine gaps and moved them into Known Gaps as plain bullets: (1) `migrate.py` loads all rows via `.scalars().all()` (`cli/migrate.py:162`) — OOM risk on orgs >500 rows while `migrate_org.py` paginates safely; (2) neither CLI imposes a DB-operation timeout; (3) SHA-256 hash collision on import is theoretical and unhandled. None are PRD-mandated for the MVP. Status: partial (all remaining unchecked items are documented gaps).
