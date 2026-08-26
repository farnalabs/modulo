---
id: feat-core-runtime-provider-core
prd: 6
adr: [docs/adr/003-agent-dispatch-model.md]
delivery-tasks: []
code:
  - backend/src/modulo/core/runtime_provider/
  - backend/src/modulo/db/models/environment_profile.py
  - backend/src/modulo/db/models/workspace_lease.py
  - backend/src/modulo/db/crud/environment_profile.py
  - backend/src/modulo/api/routes/environment_profiles.py
  - backend/src/modulo/api/routes/environments.py
  - backend/src/modulo/core/graph_validator/__init__.py
  - backend/src/modulo/connectors/shell/__init__.py
  - frontend/src/views/environment-profiles/
  - frontend/src/views/AdminEnvironmentProfilesView.vue
unit-tests:
  - backend/tests/unit/core/runtime_provider/test_abc.py
  - backend/tests/unit/core/runtime_provider/test_hub.py
  - backend/tests/unit/core/runtime_provider/test_e2b.py
  - backend/tests/unit/core/runtime_provider/test_local.py
  - backend/tests/unit/runtime_provider/test_docker_provider.py
  - backend/tests/unit/graph_validator/test_environment_capabilities.py
  - backend/tests/unit/api/test_environments.py
bdd:
  - backend/tests/bdd/features/environments/environment_profiles.feature
  - backend/tests/bdd/features/workflows/binding.feature
depends-on: []
status: covered
---

# Runtime Provider Core

Provider abstraction that executes `sandbox_agent` nodes and manages workspaces
(ADR 003 — Agent Dispatch Model). Runtime providers (`local`, `local_docker`, `docker`,
`e2b`) expose the same capability surface, gated per-environment via environment
profiles and validated at graph-validation time. `ShellConnector` (the legacy command
connector) is deprecated since ADR 003 and maps onto the same runtime-provider surface;
its product-map entry carries the ADR 003 deprecation notice.

## Behaviours

- [x] Runtime provider base contract (`base.py`): lifecycle, health, execution, teardown
- [x] Provider registry/hub resolves the configured provider by profile
- [x] Built-in providers: `local`, `local_docker`, `docker`, `e2b`
- [x] Environment profiles CRUD (`/api/v1/environment-profiles`): list, create, get,
      update, delete, restore — input-validated, org-scoped
- [x] Run environments surface (`/api/v1/environments`) with workspace leases
- [x] Graph validator rejects pipelines whose nodes need a capability the profile lacks
      (`test_environment_capabilities`)
- [x] Workspace leases are released/expired and reaped (`workspace_lease`)
- [x] `sandbox_agent` node dispatch, crash-resume, and output-handling contracts
      (run model fields: node retry/resume markers)
- [x] ShellConnector is deprecated (ADR 003, 2026-07-16) with a runtime
      `DeprecationWarning` and doc notice; existing ShellConnector pipelines continue
      running, and the node type is marked deprecated in the UI — new pipelines should
      use `sandbox_agent`

## Known Gaps

- **No BDD coverage for the platform-provider matrix** — environment-profile BDD exists,
  but no `.feature` file exercises each provider backend end to end.
- **E2B provider is V3-deferred / environment-dependent** — runs only where the E2B
  integration is configured.

## QA History

- 2026-08-25: **improve-architecture (product-map walk)** — restored this entry as part of
  rebuilding the `docs/product-map/` feature graph. This entry is the one ADR 003
  requires to carry the ShellConnector deprecation notice
  (`docs/adr/003-agent-dispatch-model.md`). Re-verified the runtime_provider package
  layout, environment-profile CRUD routes, workspace-lease model, and ShellConnector
  deprecation notice against the current tree. Status: covered.
