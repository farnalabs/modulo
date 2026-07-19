# Database Schema Review — Modulo

Date: 2026-07-15
Scope: All 57 SQLAlchemy models in `backend/src/modulo/db/models/`
Reviewer: Automated deep review

## Executive Summary

Modulo's database schema is well-architected overall, with consistent patterns that avoid most of the common pitfalls described in the reference Reddit post. The codebase uses:

- **Native UUID types** everywhere (no VARCHAR(36) for UUIDs)
- **DateTime(timezone=True)** on all timestamps (no naive timestamps)
- **Numeric/Decimal** for all monetary values (no FLOAT for money)
- **Proper Boolean columns** (no string "true"/"false")
- **Consistent snake_case** naming throughout
- **Multi-tenancy via OrgScoped base class** with RLS on Postgres
- **Extensive CHECK constraints** on enum-like string columns
- **Composite unique constraints** for business-logic uniqueness

However, several issues were found across 4 categories:

- **1 critical**: Missing FK constraints where UUID columns reference other tables without DB-level enforcement
- **2 major**: Missing indexes on frequently-queried FK columns; a `Float` column used for scoring where precision type should be consistent
- **8 minor**: String length inconsistencies, migration numbering gap, missing `server_default` on some boolean columns

The schema does NOT suffer from: VARCHAR(255) everywhere, FLOAT for money, string booleans, naive timestamps, UUIDs as VARCHAR, hard DELETE without soft-delete, naming chaos, or localStorage-as-database.

## Findings by Category

### 1. Indexes

**Reddit warning:** "No indexes beyond the primary key."

**Current state:** Good. The `OrgScoped` base adds an index on `organisation_id`. Many FK columns have explicit `index=True`. Several unique constraints provide implicit indexes.

**Specific gaps found:**

| File | Column | Issue | Severity |
|---|---|---|---|
| `notification.py:24` | `target_user_id` (FK→accounts) | No explicit index. This is commonly queried for per-user notifications. | **Major** |
| `scheduled_report.py:22` | `created_by` (FK→accounts) | No explicit index. Admin views filter by creator. | **Minor** |
| `variant_group.py:35` | `degraded_evals` | No index. Frequently filtered in eval monitoring queries. | **Minor** |
| `saved_view.py:21` | `view_type` | No index. Filtered by type in listing views. | **Minor** |
| `node_observation.py:18` | `account_id` (FK→accounts) | No explicit index. | **Minor** |
| `primitive_rating.py:19` | `account_id` (FK→accounts) | No explicit index. | **Minor** |
| `connector_instance.py:27` | `account_id` (FK→accounts) | No explicit index. | **Minor** |
| `lifecycle_map.py:32` | `account_id` (FK→accounts) | No explicit index. | **Minor** |

**Note:** PostgreSQL automatically creates an index on any column with a `UNIQUE` constraint, so columns with `UniqueConstraint` (like `slug`, `client_id`) are covered even without explicit `index=True`. FK columns without unique constraints rely on explicit indexes.

### 2. Foreign Keys & Constraints

**Reddit warning:** "No foreign keys, no constraints."

**Current state:** Very strong. The vast majority of FK relationships have explicit `ForeignKey` with proper `ondelete` actions. Extensive `CheckConstraint` usage on string enum columns. Composite `UniqueConstraint` for business-logic uniqueness.

**Specific gaps found:**

| File | Column | Referenced Table | Issue | Severity |
|---|---|---|---|---|
| `mcp_setup_token.py:24` | `created_by` (UUID) | `accounts.id` | UUID column that stores an account ID but has **no FK constraint**. Should have `ForeignKey("accounts.id", ondelete="RESTRICT")`. | **Critical** |
| `system_config.py:23` | `updated_by` (UUID) | `accounts.id` | UUID column that stores an account ID but has **no FK constraint**. | **Major** |
| `composite_template.py:17-18` | `input_schema_id`, `output_schema_id` (UUID) | `schemas.id` | UUID columns that reference schemas but have **no FK constraints**. | **Major** |
| `lifecycle_map.py:31` | (N/A) | N/A | `archived_at` without any `is_archived` boolean — relies on NULL check. Minor consistency concern. | **Minor** |

**Note:** `Organisation.created_by` is deliberately not an FK (comment says "the first organisation must exist before its first user"). This is an acceptable design choice, not a bug.

### 3. Column Types

**Reddit warning:** "VARCHAR(255) for everything. Money stored as FLOAT. Booleans as strings. Timestamps without timezone. UUIDs stored as VARCHAR(36)."

**Current state: Excellent.** None of the classic anti-patterns are present:
- **Money**: All monetary values use `Numeric(14, 6)` (8 occurrences) ✓
- **Booleans**: Proper `Boolean` type, never strings ✓
- **Timestamps**: All `DateTime(timezone=True)` ✓
- **UUIDs**: All native `Uuid()` type ✓

**Specific issues found:**

| File | Column | Type | Issue | Severity |
|---|---|---|---|---|
| `eval_definition.py:34` | `pass_threshold` | `Float` | Score threshold should use `Numeric` for consistent precision with other decimal values in the schema. `Float` is an IEEE 754 binary float, while `Numeric` is exact decimal. | **Major** |
| `lifecycle_map.py:31` | `archived_at` | `DateTime` | Has `TimeZone` set correctly, but `nullable=True` is redundant with `Mapped[datetime \| None]` — no type issue, just redundant typing. | **Minor** |
| `feedback_record.py:42` | `correction_run_id` (FK) | UUID | FK to `runs.id` — correct type. | ✓ |

**String length usage:**
- `String(255)` is the most common string length for names (normal and acceptable)
- `String(320)` for email (RFC 5321 max) — correct
- `String(2000)` for description fields — reasonable
- `String(64)` for hashes (SHA-256 fits in 64 hex chars) — correct
- `String(128)` for SHA-512 — correct
- `String(2048)` for URLs — generous but acceptable
- `String(5000)` for error_detail — could be `Text` but acceptable on Postgres

### 4. Soft Delete & Audit Trail

**Reddit warning:** "Hard DELETE everywhere. No deleted_at, no created_by, no updated_at."

**Current state:** Mixed. The `TimestampMixin` provides `created_at`/`updated_at` on all OrgScoped entities. `account_id` serves as the creator reference on most entities.

**Soft delete coverage:**

| Entity | `deleted_at` / `archived_at` | Type |
|---|---|---|
| `Organisation` | `deleted_at` | Soft-delete ready |
| `Pipeline` | `archived_at` | Archive-only (not hard delete) |
| `LifecycleMap` | `archived_at` | Archive-only |
| `Account` | `active` boolean only | Soft-delete via flag, no timestamp |

**Entities that could benefit from soft-delete:**
- `Agent` — agents can be removed but should be soft-deletable for history
- `Trigger` — triggers could be deactivated but deleted triggers lose audit trail
- `Schema` / `SchemaVersion` — schema deletion could break historical references

**Audit trail gaps:**

| Entity | Has `created_by`/`account_id`? | Has `updated_by`? |
|---|---|---|
| `Agent` | `account_id` ✓ | Missing |
| `Pipeline` | `account_id` ✓ | Missing |
| `ConnectorInstance` | `account_id` ✓ | Missing |
| `ModelBackend` | `account_id` ✓ | Missing |
| `EvalDefinition` | `account_id` ✓ | Missing |
| `Organisation` | `created_by` ✓ | Missing `updated_by` |
| `SystemConfig` | — | `updated_by` ✓ |
| `ScheduledReport` | `created_by` ✓ | Missing |
| `Secret` | — (no creator) | Missing |

The `TimestampMixin` provides `updated_at`, but there's no `updated_by` pattern. Adding `updated_by` to mutable entities would provide full audit trail coverage.

### 5. Naming Conventions

**Reddit warning:** "Naming chaos — userId, user_id, UserID mixed."

**Current state: Excellent.** All naming is consistent:
- **Tables:** snake_case plural (`accounts`, `pipeline_edges`, `org_api_keys`)
- **Columns:** snake_case (`organisation_id`, `account_id`, `created_at`)
- **Models:** PascalCase (`OrgApiKey`, `PipelineEdge`, `ChatSession`)
- **Constraints:** Prefixed snake_case (`ck_`, `uq_`, `fk_`, `ix_`)
- **Classes:** Python PEP-8 compliant

No naming inconsistencies found.

### 6. Multi-Tenancy

**Reddit warning:** "No multi-tenancy thinking."

**Current state: Excellent.** Multi-tenancy is a first-class design principle:
- `OrgScoped` base class provides `organisation_id` with FK to `organisations.id` and index
- PostgreSQL RLS via `set_config('app.organisation_id', :oid, true)` inside transactions
- Non-Postgres backends use `do_orm_execute` listener for automatic tenant filtering
- Cross-tenant FK enforcement via trigger function `enforce_same_organisation()`
- Pool-level RLS reset hook prevents org context leakage between requests
- RLS policies cover both strict-isolation and bootstrap-context tables

**Specific notes:**
- `ChatMessage` and `RemySkill` have `organisation_id` but extend `Base`, not `OrgScoped` — they manage the FK independently with proper FK constraint
- `OAuthAuthorizationCode` and `OAuthTokenFamily` also manage `organisation_id` independently — correct
- No evidence of tenant context leakage in any model

### 7. Migrations

**Current state:** 9 migration files, with a numbering gap.

**Issues found:**

| Issue | Details | Severity |
|---|---|---|
| **Numbering gap** | Migration `0004` is missing. Sequence goes `0001_v2_identity_org` → `0002_v2_teams_library` → `0003_v2_pipeline_runtime` → `0005_v2_features_system`. This suggests `0004` was created and removed, or merged out of order. Alembic uses revision IDs not numbers, so this is cosmetic — but it makes the migration sequence harder to reason about. | **Minor** |
| **Revision ID length** | The `env.py` widens `alembic_version.version_num` to VARCHAR(255) at migration time to handle long revision IDs. Good defensive practice. | ✓ |
| **Trigger function** | `enforce_same_organisation()` has been properly fixed in migration 0010 to handle non-UUID columns. | ✓ |
| **Data migrations** | No large data migrations found — all are DDL-only. Good. | ✓ |
| **NOT NULL on populated columns** | No ALTER TABLE ADD NOT NULL on populated columns found. | ✓ |

### 8. Commit / Rollback Patterns

**Current state: Correct.**
- `AsyncSessionLocal` configured with `autobegin=False` (matches the DI pattern documented in lessons learned)
- Pool settings: `pool_size=20`, `max_overflow=10`, `pool_recycle=3600`, `pool_timeout=30`, `pool_pre_ping=True`
- Connection timeout and command_timeout configured for Postgres
- RLS `set_config` requires an active transaction (enforced by `_ensure_active_transaction`)

## Recommendations

### Critical (must fix)

1. **Add FK constraint on `mcp_setup_tokens.created_by` → `accounts.id`**
   - File: `backend/src/modulo/db/models/mcp_setup_token.py:24`
   - Change: Add `ForeignKey("accounts.id", ondelete="RESTRICT")` to `created_by` column
   - Migration: `ALTER TABLE mcp_setup_tokens ADD CONSTRAINT fk_mcp_setup_tokens_created_by FOREIGN KEY (created_by) REFERENCES accounts(id) ON DELETE RESTRICT;`

### Major (should fix)

1. **Add index on `notifications.target_user_id`**
   - Per-user notification queries will scan without it
   - Migration: `CREATE INDEX ix_notifications_target_user_id ON notifications(target_user_id);`

2. **Add FK constraints on `system_config.updated_by` → `accounts.id`**
   - File: `backend/src/modulo/db/models/system_config.py:23`
   - Change: Add `ForeignKey("accounts.id", ondelete="SET NULL")` to `updated_by` column

3. **Add FK constraints on `composite_templates.input_schema_id` / `output_schema_id` → `schemas.id`**
   - Files: `backend/src/modulo/db/models/composite_template.py:17-18`
   - Change: Add `ForeignKey("schemas.id", ondelete="SET NULL")` to both columns

4. **Change `eval_definitions.pass_threshold` from `Float` to `Numeric`**
   - File: `backend/src/modulo/db/models/eval_definition.py:34`
   - Change: Replace `Float` with `Numeric(8, 4)` for consistent decimal precision

5. **Add indexes on `connector_instances.account_id`**
   - FK column without explicit index — common join path
   - Migration: `CREATE INDEX ix_connector_instances_account_id ON connector_instances(account_id);`

### Minor (fix opportunistically)

1. Add indexes on `scheduled_reports.created_by`, `variant_groups.degraded_evals`, `saved_views.view_type`
2. Add missing `server_default` on `FeedbackRecord.needs_human_review` (uses `default=False` instead of `server_default="false"`)
3. Add missing `server_default` on `WorkspaceLease.lease_expires_at` (uses Python `default=None` — ORM-only)
4. Consider adding `is_archived` boolean to entities with `archived_at` for query clarity
5. Consider adding `updated_by` to mutable OrgScoped entities for full audit trail

## Summary of Findings

| Category | Critical | Major | Minor |
|---|---|---|---|
| Indexes | 0 | 1 | 7 |
| Foreign Keys | 1 | 2 | 0 |
| Column Types | 0 | 1 | 0 |
| Soft Delete & Audit | 0 | 0 | 3 |
| Naming | 0 | 0 | 0 |
| Multi-Tenancy | 0 | 0 | 0 |
| Migrations | 0 | 0 | 1 |
| **Total** | **1** | **4** | **11** |
