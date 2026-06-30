---
id: feat-core-runtime-provider-core
prd: 6.2
bdd:
  - backend/tests/bdd/features/workflows/binding.feature
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

Agent execution environments: `RuntimeProvider` ABC, `RuntimeProviderHub` registry, `E2BRuntimeProvider` concrete backend, `EnvironmentProfile` and `WorkspaceLease` entities CRUD API, graph validator integration, and Alembic migration. Defined by `task-runtime-provider-core` in ADR-001 4.

## Behaviours

### RuntimeProvider ABC (`core/runtime_provider/__init__.py`)

Provided by existing tests: test_abc.py

- [x] RuntimeProvider is an ABC with 4 abstract methods: create_workspace, exec_command, destroy_workspace, get_workspace_status
- [x] RuntimeProvider cannot be instantiated directly
- [x] Incomplete subclass (missing abstract methods) raises TypeError on instantiation
- [x] Complete concrete provider create/destroy/exec/status roundtrip works end-to-end
- [x] WorkspaceSpec dataclass has all fields with correct defaults (run_id=None, image_ref="", capabilities=[], timeout_seconds=3600, resource_limits={}, egress_policy=None, persistence_policy={}, labels={})
- [x] ExecResult dataclass has exit_code, stdout, stderr, duration_ms (nullable)
- [x] RuntimeProviderFactory Protocol is defined
- [ ] RuntimeProviderFactory Protocol is used by any consumer

### RuntimeProviderHub (`core/runtime_provider/hub.py`)

Provided by existing tests: test_hub.py

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

Provided by existing tests: test_e2b.py

- [x] create_workspace creates an E2B sandbox and returns sandbox ID
- [x] create_workspace sets timeout on the sandbox
- [x] exec_command runs command in sandbox and returns stdout/stderr/exit_code
- [x] exec_command raises RuntimeError when sandbox not found
- [x] destroy_workspace kills the E2B sandbox
- [x] destroy_workspace raises RuntimeError when sandbox not found
- [x] get_workspace_status returns alive/dead status
- [x] get_workspace_status raises RuntimeError when sandbox not found

### Graph Validator — environment capabilities

Provided by existing tests: test_environment_capabilities.py

- [x] Validator accepts nodes with satisfied capability requirements
- [x] Validator rejects nodes with unsatisfied capability requirements
- [x] Validator accepts profiles whose capabilities are a superset of the node's requirements
- [x] Validator handles nodes with empty requirements
- [x] Validator returns structured error messages for missing capabilities
- [x] Validator reports all missing capabilities, not just the first

### API — Environment Profiles CRUD

- [x] Create environment profile with required fields: name, provider_hint
- [x] Create with optional fields: description, capabilities, image_ref, timeout_seconds, resource_limits, egress_policy
- [x] Duplicate name per org returns 409
- [x] List profiles for org
- [x] Get profile by ID
- [x] Update profile fields
- [x] Delete profile
- [x] Delete non-existent profile returns 404
- [x] Auth required — 401 for unauthenticated
- [x] RLS org scoping on all CRUD operations
- [x] Admin-only management (operator role gets 403)

### API — Workspace Leases

- [x] Create workspace lease from profile triggers E2B sandbox creation
- [x] List leases for org with status filter
- [x] Get lease by ID
- [x] Release (destroy) workspace lease
- [x] Release triggers sandbox destroy

### Alembic migration

- [x] Migration creates environment_profiles table
- [x] Migration creates workspace_leases table
- [x] Migration is reversible (downgrade drops both tables)

## Known Gaps

- No BDD feature file for runtime provider behaviour (binding.feature is placeholder)
- E2B sandbox timeouts are not auto-renewed for long-running agent executions
- No resource limit enforcement on E2B sandboxes (memory, CPU)
- No egress policy enforcement on E2B sandboxes
- No alternative runtime provider implementations (only E2B)
- Workspace lease release on run completion is not automatic
