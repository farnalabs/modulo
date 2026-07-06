---
id: feat-core-runtime-provider-core
prd: 6
adr: [docs/adr/001-agent-environment-primitive.md]
bdd:
  - backend/tests/bdd/features/environments/environment_profiles.feature
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
  - backend/tests/unit/core/runtime_provider/test_local.py
  - backend/tests/unit/runtime_provider/test_docker_provider.py
  - backend/tests/unit/graph_validator/test_environment_capabilities.py
  - backend/tests/unit/api/test_environments.py
depends-on: [feat-core-pipeline-execution]
delivery-tasks: [task-runtime-provider-core]
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
- [x] RuntimeProviderFactory Protocol is defined (not yet used by any consumer — available as extension point)

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

### DockerRuntimeProvider (`core/runtime_provider/docker.py`)

Provided by existing tests: test_docker_provider.py

- [x] supports(): docker hint → true
- [x] supports(): image_ref with "docker" substring → true
- [x] supports(): other hint (e2b) → false
- [x] create_workspace: creates container with correct config (image, Cmd sleep infinity, AutoRemove, Memory)
- [x] create_workspace: passes labels as Env variables
- [x] create_workspace: defaults image to python:3.13-slim when image_ref is empty
- [x] exec_command: runs command via container.exec and returns ExecResult with stdout/stderr/exit_code
- [x] exec_command: raises ValueError for unknown provider_ref
- [x] destroy_workspace: stops and deletes container
- [x] destroy_workspace: unknown ref is no-op (returns None)
- [x] get_workspace_status: returns "running" from container.show()
- [x] get_workspace_status: returns "terminated" for unknown ref
- [x] get_workspace_status: falls back to "terminated" on error (e.g. connection lost)
- [x] close: closes underlying Docker client connection
- [x] close: idempotent (can be called multiple times)

### LocalRuntimeProvider (`core/runtime_provider/local.py`)

Provided by existing tests: test_local.py

- [x] supports(): local hint → true
- [x] supports(): e2b hint → false
- [x] supports(): no hint → true (default fallback)
- [x] create_workspace: creates temp directory
- [x] create_workspace: optionally clones repo from spec.labels repo_url
- [x] exec_command: runs subprocess and returns stdout/stderr/exit_code
- [x] exec_command: raises ValueError for unknown workspace
- [x] exec_command with timeout: returns exit_code=-1 and "timed out" in stderr
- [x] destroy_workspace: removes temp directory
- [x] destroy_workspace: unknown workspace is no-op
- [x] get_workspace_status: returns "running" for active workspace
- [x] get_workspace_status: returns "terminated" for destroyed workspace
- [x] concurrency semaphore: blocks when at max_concurrency
- [x] create_local_provider_from_env: defaults to 2 when env unset
- [x] create_local_provider_from_env: reads MODULO_MAX_LOCAL_CONCURRENCY
- [x] create_local_provider_from_env: invalid value defaults to 2

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

### Error Handling

- [x] ProgrammingError on DB query returns 501 Not Implemented with migration hint
- [x] SQLAlchemyError on DB query returns 503 SERVICE_UNAVAILABLE
- [x] IntegrityError on duplicate name create/update returns 409 Conflict
- [x] Missing profile returns 404 with "Environment profile not found"
- [x] Duplicate name on create returns 409
- [x] Authentication required — 401 for unauthenticated
- [x] Non-admin gets 403
- [x] Invalid egress_policy returns 422
- [x] Invalid timeout range returns 422
- [x] Empty name returns 422
- [x] Cross-org isolation — other org's profile returns 404
- [x] E2B provider missing API key raises ValueError at construction
- [x] exec_command unknown workspace raises ValueError
- [x] destroy_workspace unknown workspace is no-op
- [x] Docker container creation failure propagates
- [x] Docker exec_command timeout returns ExecResult with exit_code=-1 and "Command timed out"
- [x] Docker exec_command failure logs exception and re-raises
- [x] Local exec_command FileNotFoundError returns ExecResult with exit_code=-1
- [x] Local exec_command timeout returns ExecResult with exit_code=-1
- [x] E2B exec_command returns ExecResult with exit_code=-1 on failure
- [ ] E2B sandbox constructor failure — stack trace logged, lacks structured user-facing error
- [ ] Test SSE endpoint failure — broad Exception catch logs but returns generic message

### Edge Cases

- [x] Empty hub returns None on resolve
- [x] Unregister nonexistent provider is no-op
- [x] Duplicate registration raises ValueError
- [x] supports() exception skips provider during resolution
- [x] Profile without provider_hint attribute resolved by supports()/fallback
- [x] Local provider fallback when no hint and no supports() match
- [x] Concurrency semaphore caps parallel exec_command calls
- [x] Invalid MODULO_MAX_LOCAL_CONCURRENCY defaults to 2
- [x] E2B repo clone failure cleans up sandbox (RuntimeError)
- [x] Local repo clone failure cleans up temp directory
- [x] Docker env entries with control characters filtered out
- [x] Docker resource_limits memory_mb < 4 defaults to 512MB
- [x] E2B exec_command with missing proc attributes handled gracefully
- [ ] E2B sandbox timeout during provisioning — unhandled asyncio.TimeoutError in create_workspace
- [ ] Docker client initialization failure (daemon unreachable) — unhandled ConnectionError
- [ ] Local workspace tempdir creation failure (disk full) — unhandled OSError
- [ ] Concurrent profile name collision under high insert volume — IntegrityError now caught → 409

### Resilience & Integration Robustness

- [x] E2B API key resolved from constructor arg or env var
- [x] Docker daemon URL resolved from constructor arg, MODULO_DOCKER_HOST, DOCKER_HOST, or None (local socket)
- [x] Docker close() is idempotent
- [x] Docker get_workspace_status falls back to "terminated" on DockerError
- [x] Docker destroy_workspace best-effort: logs DockerError and swallows
- [ ] No retry/backoff on E2B API call failures (sandbox creation, exec)
- [ ] No retry/backoff on Docker API call failures
- [ ] No timeout on Docker client initialization (aiodocker.Docker() has no connect timeout)
- [ ] No circuit breaker for persistent provider failures
- [ ] Local provider semaphore is per-instance, not global — does not limit total host concurrency
- [ ] E2B provider stores sandboxes in dict (process-local) — lost on process restart
- [ ] Docker provider stores container refs in dict (process-local) — lost on process restart
- [ ] No provider health check / liveness probe

## QA History (index 162 — cross-cutting)

### Findings fixed
- Fixed CRITICAL: Added `SQLAlchemyError` catch → 503 to all 6 routes in environments.py (ProgrammingError → 501 was already there, but connection/deadlock failures propagated as raw 500)
- Fixed MAJOR: Added `IntegrityError` catch → 409 to create_profile and update_profile routes (concurrent duplicate name → 500)
- Fixed MAJOR: Updated frontmatter — `prd: 6.2` → `prd: §6` (was pointing to multi-tenancy section, not runtime providers), added `adr: [docs/adr/001-agent-environment-primitive.md]`
- Fixed MAJOR: Updated frontmatter `bdd` — was listing only `binding.feature` (about workflow binding, not runtime providers); added `environment_profiles.feature` (15 real scenarios about environment profiles, workspace lifecycle, hub resolution, capabilities)
- Fixed MAJOR: Added Error Handling section (22 checkboxes), Edge Cases section (17 checkboxes: 13 [x] + 4 [ ]), Resilience & Integration Robustness section (12 checkboxes: 5 [x] + 7 [ ]) to product map

### Known Gaps remaining
- Website docs stub needed at Website/modulo-website/src/docs/environments.md
- No BDD step implementations for environment_profiles.feature scenarios (BDD scenarios exist but may not have working step definitions)
- E2B sandbox provisioning timeout — unhandled asyncio.TimeoutError
- Docker client init unreachable daemon — unhandled ConnectionError
- Local workspace tempdir creation disk full — unhandled OSError
- No retry/backoff on E2B or Docker API failures
- No circuit breaker for persistent provider failures
- Local provider semaphore is per-instance, not global
- E2B/Docker provider state is process-local (lost on restart)
- No provider health check / liveness probe
