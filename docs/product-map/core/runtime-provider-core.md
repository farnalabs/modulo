---
id: feat-core-runtime-provider-core
prd: 
delivery-tasks: [task-nv12-runtime-provider-core]
bdd:
  - backend/tests/bdd/features/workflows/binding.feature (partial — one scenario uses provider+model_id fallback)
code:
  - backend/src/modulo/core/runtime_provider/
  - backend/src/modulo/db/models/environment_profile.py
  - backend/src/modulo/db/models/workspace_lease.py
  - backend/src/modulo/db/crud/environment_profile.py
  - backend/src/modulo/api/routes/environments.py
  - backend/src/modulo/core/graph_validator/__init__.py
  - backend/src/modulo/db/migrations/versions/0013_environment_profiles_workspace_leases.py
  - backend/src/modulo/connectors/shell/__init__.py
unit-tests:
  - backend/tests/unit/core/runtime_provider/test_abc.py
  - backend/tests/unit/core/runtime_provider/test_hub.py
  - backend/tests/unit/core/runtime_provider/test_e2b.py
  - backend/tests/unit/graph_validator/test_environment_capabilities.py
  - backend/tests/unit/api/test_environments.py
depends-on: [feat-core-pipeline-execution]
status: partial
---

# Runtime Provider Core

Agent execution environments: `RuntimeProvider` ABC, `RuntimeProviderHub` registry,
`E2BRuntimeProvider` concrete backend, `EnvironmentProfile` and `WorkspaceLease` entities,
CRUD API, graph validator integration, and Alembic migration.

Defined by `task-runtime-provider-core` in ADR-001 §4.

## Behaviours

### RuntimeProvider ABC (`core/runtime_provider/__init__.py`)
- Provided by existing tests: test_abc.py

- [x] RuntimeProvider is an ABC with 4 abstract methods: create_workspace, exec_command, destroy_workspace, get_workspace_status
- [x] RuntimeProvider cannot be instantiated directly
- [x] Incomplete subclass (missing abstract methods) raises TypeError on instantiation
- [x] Complete concrete provider create/destroy/exec/status roundtrip works end-to-end
- [x] WorkspaceSpec dataclass has all fields with correct defaults (run_id=None, image_ref="", capabilities=[], timeout_seconds=3600, resource_limits={}, egress_policy=None, persistence_policy={}, labels={})
- [x] ExecResult dataclass has exit_code, stdout, stderr, duration_ms (nullable)
- [x] RuntimeProviderFactory Protocol is defined
- [ ] RuntimeProviderFactory Protocol is used by any consumer

### RuntimeProviderHub (`core/runtime_provider/hub.py`)
- Provided by existing tests: test_hub.py

- [x] register(name, provider) stores provider under symbolic name
- [x] register duplicate name raises ValueError("already registered")
- [x] get(name) returns provider or None
- [x] unregister(name) removes provider
- [x] unregister nonexistent name is no-op
- [x] list_providers() returns a copy of the registry dict (mutating copy does not affect hub)
- [x] resolve(profile) uses provider_hint to pick matching registered provider
- [x] resolve prefers provider_hint over supports()
- [x] resolve falls through to supports() when no hint
- [x] resolve returns first registered provider when no hint and no supports() match
- [x] resolve returns None when hub is empty
- [x] resolve hint not found continues to supports() / fallback
- [x] resolve skips providers whose supports() raises an exception
- [x] resolve skips providers without a supports attribute during supports resolution
- [x] resolve handles profiles without provider_hint attribute at all
- [x] resolve hint → unregister → fall through to next match

### E2BRuntimeProvider (`core/runtime_provider/e2b.py`)
- Provided by existing tests: test_e2b.py

- [x] Constructor raises ValueError when no API key provided and MODULO_E2B_API_KEY not set
- [x] Constructor accepts explicit api_key argument
- [x] Constructor falls back to MODULO_E2B_API_KEY env var
- [x] supports() matches "e2b" hint (case-insensitive)
- [x] supports() matches "e2b" in image_ref
- [x] supports() returns False for non-matching hint
- [x] supports() returns False when neither hint nor image_ref match
- [x] create_workspace creates sandbox with image_ref as template
- [x] create_workspace uses "default" template when image_ref is empty
- [x] create_workspace stores sandbox in internal _sandboxes dict
- [x] create_workspace clones repo when repo_url in labels
- [x] create_workspace checks out repo_ref when specified
- [x] create_workspace skips clone when no repo_url in labels
- [x] create_workspace clone failure (non-zero exit) does not raise — logged as warning
- [x] create_workspace propagates E2B SDK constructor exceptions
- [x] Multiple workspaces tracked independently in _sandboxes dict
- [x] exec_command returns ExecResult with exit_code, stdout, stderr
- [x] exec_command raises ValueError("Unknown sandbox") for unrecognised provider_ref
- [x] exec_command passes timeout to E2B process API
- [x] exec_command defaults to 60s timeout when timeout=None
- [x] exec_command preserves non-zero exit codes (e.g. 127 for command not found)
- [x] exec_command handles proc object without expected attributes (fallback to empty defaults)
- [x] exec_command properly shells-quotes arguments
- [x] destroy_workspace kills sandbox
- [x] destroy_workspace removes sandbox from _sandboxes dict
- [x] destroy_workspace unknown provider_ref is no-op
- [x] destroy_workspace handles kill failure (logged, not raised)
- [x] get_workspace_status returns status from sandbox.get_info()
- [x] get_workspace_status falls back to "running" when get_info unavailable
- [x] get_workspace_status raises ValueError for unknown sandbox
- [x] get_workspace_status returns "running" when get_info raises exception
- [x] E2BRuntimeProvider is a RuntimeProvider subclass
- [x] E2BRuntimeProvider can be registered with RuntimeProviderHub and resolved by hint

### EnvironmentProfile entity (`db/models/environment_profile.py`)
- No dedicated unit tests (tested via CRUD integration tests)

- [x] Entity is OrgScoped
- [x] Fields: name, description, image_ref, capabilities (JSON), egress_policy, persistence_policy (JSON), timeout_seconds, resource_limits_json (JSON), created_by (FK to users, SET NULL), is_active (default true)

### WorkspaceLease entity (`db/models/workspace_lease.py`)
- No dedicated tests

- [x] Entity is OrgScoped
- [x] FK to environment_profiles (RESTRICT on delete)
- [x] FK to runs (SET NULL on delete)
- [x] Check constraint: status IN ('pending', 'provisioning', 'active', 'completed', 'failed', 'expired')
- [x] Fields: provider_ref, status, started_at, expires_at, resource_usage_json (JSON)

### EnvironmentProfile CRUD (`db/crud/environment_profile.py`)
- [x] create_environment_profile creates row with all fields
- [x] get_environment_profile returns profile by id or None
- [x] list_environment_profiles returns paginated results ordered by created_at desc
- [x] update_environment_profile applies partial updates via apply_updates
- [x] update_environment_profile returns None when profile not found
- [x] delete_environment_profile deletes row by id
- [x] delete_environment_profile returns False when profile not found

### REST API (`api/routes/environments.py`)
- Provided by existing tests: test_environments.py (unit)

- [x] GET /api/v1/environments — list profiles (paginated, RLS-scoped)
- [x] POST /api/v1/environments — create profile (201)
- [x] GET /api/v1/environments/{id} — get single profile
- [x] PATCH /api/v1/environments/{id} — partial update profile
- [x] DELETE /api/v1/environments/{id} — delete profile (204)
- [x] POST /api/v1/environments/{id}/test — SSE sandbox test stream
- [x] Sandbox test streams: provisioning, provisioned, command_start, command_complete, destroying, destroyed events
- [x] Sandbox test failure streams "failed" event and cleans up sandbox
- [x] Missing profile returns 404 on all CRUD endpoints
- [x] egress_policy validated against regex pattern `^(deny_all|allow_all|allow_listed)$`
- [x] timeout_seconds validated between 60 and 86400

### Alembic migration (`0013_environment_profiles_workspace_leases`)
- [x] Creates environment_profiles table with all columns and indexes
- [x] Creates workspace_leases table with FKs, indexes, and status check constraint
- [x] Adds environment_profile_id FK column to pipeline_snapshots
- [x] Migration is reversible (downgrade drops all three additions)

### Graph validator — environment capability checks (`core/graph_validator/__init__.py`)
- Provided by existing tests: test_environment_capabilities.py

- [x] No environment_profile_id skips capability check
- [x] Missing EnvironmentProfile returns ENV_PROFILE_NOT_FOUND error
- [x] Graph with no agent_id references skips agent DB query
- [x] Agent with empty required_capabilities is always valid
- [x] All agent capabilities satisfied by profile is valid
- [x] Missing agent capabilities returns ENV_MISSING_CAPABILITIES error
- [x] Multiple agents checked independently against same profile
- [x] validate_for_run includes environment capability check
- [x] Snapshot validate() includes environment capability check
- [x] Non-UUID agent_id is skipped without error

### Integration tests (`tests/integration/crud/test_environment_profiles.py`)
- [x] Create profile rounds all fields correctly
- [x] Read profile by id
- [x] Update profile modifies fields in-place
- [x] Minimal fields get sensible defaults (description=None, egress_policy=None, timeout_seconds=3600, persistence_policy={}, resource_limits_json={}, is_active=True)
- [ ] RLS isolation between organisations — **SKIPPED** (awaiting-implementation)

### Shell connector with RuntimeProvider (`connectors/shell/__init__.py`)
- [x] ShellConnector accepts optional RuntimeProvider
- [x] ShellConnector raises ValueError("RuntimeProvider required") when missing on initialise

## Known Gaps

- **No BDD feature file**: No `.feature` file covers runtime provider behaviour. The `binding.feature` has one related scenario (model backend resolved by provider+model_id fallback) but this is tangential.
- **`test_rls_isolation` skipped**: Integration test for RLS isolation between organisations is marked `@pytest.mark.skip(reason="awaiting-implementation — RLS isolation needs investigation")`.
- **`RuntimeProviderFactory` Protocol unused**: The Protocol is defined at `core/runtime_provider/__init__.py:71` but never referenced anywhere in the codebase — no consumer type-checks against it, no implementations conform to it.
- **No PRD section assigned**: `prd:` frontmatter is blank. This feature is documented in ADR-001 rather than the PRD.
- **No WorkspaceLease CRUD**: The model exists but there is no `db/crud/workspace_lease.py` — unlike EnvironmentProfile which has full CRUD.
- **WorkspaceLease not wired to run lifecycle**: The entity exists but is not created/updated during run execution (no service layer integration).
- **Sandbox test cleanup creates fresh provider**: On error in `_sandbox_test_stream` (environments.py:269-271), a fresh `E2BRuntimeProvider()` is constructed for cleanup instead of reusing the existing `provider` — will fail if the original was created with an explicit `api_key` not available via env var.
- **E2BRuntimeProvider in-memory only**: `_sandboxes` dict is instance-local with no persistence or recovery — provider restarts lose all tracked sandboxes.
- **No `supports()` on `RuntimeProvider` ABC**: The base ABC doesn't declare `supports()` as abstract; it's duck-typed in only on `E2BRuntimeProvider` and checked via `getattr` in `hub.resolve`. Inconsistency in the interface contract.

