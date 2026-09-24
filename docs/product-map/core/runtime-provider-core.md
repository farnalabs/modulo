---
id: feat-core-runtime-provider-core
prd: 6
adr: [ADR 044 (agent-dispatch-model), ADR 040 (runtime-provider-errors)]
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
  - backend/tests/unit/core/runtime_provider/test_default_hub.py
  - backend/tests/unit/core/runtime_provider/test_provider_type_coverage.py
  - backend/tests/unit/core/runtime_provider/test_provider_close.py
  - backend/tests/unit/core/runtime_provider/test_error_family.py
  - backend/tests/unit/core/runtime_provider/test_e2b.py
  - backend/tests/unit/core/runtime_provider/test_e2b_conformance_slice2.py
  - backend/tests/unit/core/runtime_provider/test_e2b_read_log_tail.py
  - backend/tests/unit/core/runtime_provider/test_e2b_apply_isolation.py
  - backend/tests/unit/core/runtime_provider/test_file_io_primitives.py
  - backend/tests/unit/core/runtime_provider/test_local.py
  - backend/tests/unit/core/runtime_provider/test_workspace_network_validation.py
  - backend/tests/unit/core/runtime_provider/test_docker_endpoint_tls.py
  - backend/tests/unit/core/runtime_provider/test_far1128_cap_environments_dispatch.py
  - backend/tests/unit/runtime_provider/test_docker_provider.py
  - backend/tests/unit/pipeline_engine/test_e2b_isolation_flag.py
  - backend/tests/unit/graph_validator/test_environment_capabilities.py
  - backend/tests/unit/api/test_environment_profiles_routes.py
bdd:
  - backend/tests/bdd/features/environments/environment_profiles.feature
  - backend/tests/bdd/features/runtime_providers/provider_matrix.feature
  - backend/tests/bdd/features/runtime_providers/file_io.feature
  - backend/tests/bdd/features/workflows/binding.feature
depends-on: []
status: covered
---

# Runtime Provider Core

Provider abstraction that executes `sandbox_agent` nodes and manages workspaces
(ADR 044 — Agent Dispatch Model). Runtime providers (`local`, `runner_docker` with
`docker`/`local_docker` aliases, `e2b`) expose the same capability surface, gated
per-environment via environment profiles and validated at graph-validation time.
`ShellConnector` (the legacy command connector) is deprecated since ADR 044 and maps
onto the same runtime-provider surface; its product-map entry carries the ADR 044
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
- [x] ShellConnector is deprecated (ADR 044, 2026-07-16) with a runtime
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
- [x] ADR 040 additive `RuntimeProviderError` family (FAR-1050 slice 1): typed
      members (`ProviderCapabilityUnsupportedError`, `WorkspaceGoneError`,
      `StreamingUnsupportedError`, `ArtifactTooLargeError`, `RateLimitedError`,
      `SdkMissingError`, `ProvisionTimeoutError`, `UnknownRefError`,
      `BackendUnreachableError`) with an explicit dual hierarchy — the
      pre-existing `ProviderNotConfiguredError` / `UnknownProviderTypeError`
      config tree is deliberately NOT re-parented under the new base, so each
      dispatch catch-site stays reconciled
- [x] `exec_command_stream` (D4 primitive) with a kill handle: async decoded
      chunks, `done` on every stream end, `exit_code` stays `None` until the END
      of a healthy stream (never a fabricated zero exit on a mid-stream
      engine/proxy drop), and the ABC default raises the typed
      `StreamingUnsupportedError` — never a raw `NotImplementedError` (ADR 040
      error-honesty carve-out)
- [x] `destroy_workspace_by_ref` (ADR 040 substrate-level destroy): idempotent on
      already-destroyed / foreign refs (no-op success), confirmed-gone `True` /
      unconfirmed `False` best-effort (never raises), overridable via
      `AsyncSandbox.connect` on E2B, typed `ProviderCapabilityUnsupportedError`
      default
- [x] `read_log_tail` (FAR-1050 R1): bounded tail read (`max_bytes` newest-end
      cap, 4k raw fallback, `b""` on invalid ref / fetch failure — never raises),
      with E2B's legacy `_fetch_sandbox_log_tail` content parity pinned
- [x] `apply_isolation` + the frozen `IsolationPolicy` carrier (FAR-1050 R3): the
      single owner of the three in-sandbox controls — git-credential scoping,
      the selected-mode egress allowlist, and the read-only seal — with
      flag-gated parity to the legacy `sandbox_policy.apply_sandbox_policy`
      (enforcement-critical-raise vs egress-best-effort split) and a typed
      `ProviderCapabilityUnsupportedError` refusal on non-overriding providers
- [x] File-I/O primitives (FAR-1050 R2a): `read_file` / `write_file` /
      `list_files` / `get_info` (+ the frozen `WorkspaceFileInfo` value object)
      on the ABC — exec-based binary-safe defaults (base64 over the text exec
      channel, shlex-quoted paths, `mkdir -p` parent creation, sorted full
      child paths, `stat` parsing, 30s per-command bound, typed
      `RuntimeProviderError` on a non-zero exit) with E2B native `sandbox.files`
      SDK overrides, all unit-covered and BDD-exercised against the REAL
      `LocalRuntimeProvider` exec backend (`file_io.feature`)

## Known Gaps

- **E2B provider is V3-deferred / environment-dependent** — runs only where the E2B
  integration is configured.

## QA History

- 2026-09-25: **product-map walk** — walked the FAR-1050 runtime-provider
  primitive series into this entry: the ADR 040 `RuntimeProviderError` family
  (slice 1), `exec_command_stream` + `destroy_workspace_by_ref` (slice 2),
  `read_log_tail` (R1), `apply_isolation` + `IsolationPolicy` (R3), and the
  file-I/O primitives `read_file` / `write_file` / `list_files` / `get_info` +
  `WorkspaceFileInfo` (R2a) were shipped without behaviour-tracker lines or
  citation updates; added the behaviour lines, the missing unit-test citations,
  the ADR 040 reference, and new executing BDD coverage
  (`runtime_providers/file_io.feature`, steps in
  `features/runtime_providers/test_file_io_steps.py`) driving the REAL
  `LocalRuntimeProvider` exec-based defaults. Status: covered.
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
  rebuilding the `docs/product-map/` feature graph. This entry is the one ADR 044
  requires to carry the ShellConnector deprecation notice
  (ADR 044 (agent-dispatch-model)). Re-verified the runtime_provider package
  layout, environment-profile CRUD routes, workspace-lease model, and ShellConnector
  deprecation notice against the current tree. Status: covered.
