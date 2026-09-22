---
id: feat-core-runtime-provider-core
prd: 6
adr: [ADR 003 (agent-dispatch-model)]
delivery-tasks: []
code:
  - backend/src/modulo/core/runtime_provider/
  - backend/src/modulo/db/models/environment_profile.py
  - backend/src/modulo/db/crud/environment_profile.py
  - backend/src/modulo/api/routes/environment_profiles.py
  - backend/src/modulo/core/graph_validator/__init__.py
  - backend/src/modulo/connectors/shell/__init__.py
  - frontend/src/views/environment-profiles/
  - frontend/src/stores/environmentProfiles.ts
unit-tests:
  - backend/tests/unit/core/runtime_provider/test_abc.py
  - backend/tests/unit/core/runtime_provider/test_hub.py
  - backend/tests/unit/core/runtime_provider/test_e2b.py
  - backend/tests/unit/core/runtime_provider/test_local.py
  - backend/tests/unit/runtime_provider/test_docker_provider.py
  - backend/tests/unit/graph_validator/test_environment_capabilities.py
  - backend/tests/unit/api/test_environment_profiles_routes.py
bdd:
  - backend/tests/bdd/features/environments/environment_profiles.feature
  - backend/tests/bdd/features/runtime_providers/provider_matrix.feature
  - backend/tests/bdd/features/workflows/binding.feature
depends-on: []
status: covered
---

# Runtime Provider Core

Provider abstraction that executes `sandbox_agent` nodes and manages workspaces
(ADR 003 — Agent Dispatch Model). Runtime providers (`local`, `runner_docker` with
`docker`/`local_docker` aliases, `e2b`) expose the same capability surface, gated
per-environment via environment profiles and validated at graph-validation time.
`ShellConnector` (the legacy command connector) is deprecated since ADR 003 and maps
onto the same runtime-provider surface; its product-map entry carries the ADR 003
deprecation notice. The WorkspaceLease scaffolding was removed in FAR-587 (ADR 029)
— workspace state lives in `runs.sandbox_dispatch_state`.

## Behaviours

- [x] Runtime provider base contract (`base.py`): lifecycle, health, execution, teardown
- [x] Provider registry/hub resolves the configured provider deterministically
      (explicit provider_type/hint match; `ProviderNotConfiguredError` otherwise)
- [x] Built-in providers: `local`, `runner_docker` (aliases `docker`, `local_docker`), `e2b`
- [x] Environment profiles CRUD (`/api/v1/environment-profiles`): list, create, get,
      update, delete, restore, and `POST /{id}/test` (SSE sandbox connectivity check) —
      input-validated, org-scoped, gated on the `environment_profiles` feature
- [x] Graph validator rejects pipelines whose nodes need a capability the profile lacks
      (`test_environment_capabilities`)
- [x] `sandbox_agent` node dispatch, crash-resume, and output-handling contracts
      (run model fields: node retry/resume markers)
- [x] ShellConnector is deprecated (ADR 003, 2026-07-16) with a runtime
      `DeprecationWarning` and doc notice; existing ShellConnector pipelines continue
      running, and the node type is marked deprecated in the UI — new pipelines should
      use `sandbox_agent`
- [x] Platform-provider matrix is BDD-exercised against the REAL runtime-provider
      seams network-free and DB-free (`provider_matrix.feature`):
      `build_hub` registers `local` unconditionally and gates `e2b` /
      the docker family on their documented env signals (an unrelated
      `MODULO_RUNNER_*` var never registers Docker — FAR-996); the hub resolves
      deterministically (hint wins, docker-family aliases share one provider,
      known-but-unregistered types raise `ProviderNotConfiguredError` naming the
      remediation env var, unknown types raise `UnknownProviderTypeError` naming
      the valid vocabulary, a missing type is unresolvable); and the factory
      `initialise` loads docker-family configs under their config name, skips e2b
      without an api_key, and rejects unknown types

## Known Gaps

- **E2B provider is V3-deferred / environment-dependent** — runs only where the E2B
  integration is configured.

## QA History

- 2026-09-22: **product-map walk** — closed the "No BDD coverage for the
  platform-provider matrix" gap (`provider_matrix.feature`, steps in
  `features/runtime_providers/test_provider_matrix_steps.py`), driving the REAL
  `build_hub` / `RuntimeProviderHub.resolve` / factory `initialise` seams
  network-free and DB-free (real `LocalRuntimeProvider` / `DockerRuntimeProvider`
  / `E2BRuntimeProvider(api_key=...)` constructors, which open no connections):
  the env-gated registration matrix (local always; e2b / docker family gated on
  their documented signals; unrelated `MODULO_RUNNER_*` never registers Docker,
  FAR-996), the deterministic resolve matrix (hint-wins, docker-family aliases →
  one provider, known-but-unregistered → `ProviderNotConfiguredError` naming the
  remediation env var, unknown → `UnknownProviderTypeError` naming the valid
  vocabulary, missing type → unresolvable), and the config-driven `initialise`
  (docker-family aliases under a config name, e2b skipped without an api_key,
  unknown config types rejected). 13 scenarios execute in CI.
- 2026-09-02: **FAR-551** — collapsed the duplicate `/admin/environments` UI +
  `environments.py` router into `/environment-profiles`; ported the `/test`
  connectivity check; added the missing API feature-gate.
- 2026-08-25: **product-map review pass** — restored this entry as part of
  rebuilding the `docs/product-map/` feature graph. This entry is the one ADR 003
  requires to carry the ShellConnector deprecation notice
     (ADR 003 (agent-dispatch-model)). Re-verified the runtime_provider package
  layout, environment-profile CRUD routes, workspace-lease model, and ShellConnector
  deprecation notice against the current tree. Status: covered.
