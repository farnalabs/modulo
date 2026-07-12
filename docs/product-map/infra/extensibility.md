---
id: feat-infra-extensibility
prd: 10,10.2,10.3,10.3a,10.4,10.5
delivery-tasks:
  - task-fable-first-run
bdd:
  - backend/tests/bdd/features/plugins/plugin_registry.feature
  - backend/tests/bdd/features/orgs/org_onboarding.feature
code:
  - backend/src/modulo/core/plugin_registry/__init__.py
  - backend/src/modulo/api/routes/plugins.py
  - backend/src/modulo/api/routes/onboarding.py
  - backend/src/modulo/api/main.py
  - backend/src/modulo/core/runtime_config/store.py
  - backend/src/modulo/otel_bridge/handler.py
  - backend/src/modulo/otel_bridge/export.py
unit-tests:
  - backend/tests/unit/plugin_registry/test_plugin_registry.py
  - backend/tests/unit/otel_bridge/test_export.py
  - backend/tests/unit/otel_bridge/test_telemetry_toggle.py
  - backend/tests/unit/core/runtime_config/test_store.py
  - backend/tests/integration/test_demo_first_run.py
depends-on:
  - feat-pipelines-core
  - feat-core-runtime-config
status: partial
---

# Extensibility and Distribution

Extensibility architecture — plugin system (§10.2), first-run experience/onboarding (§10.3), alpha documentation (§10.3a), alpha exit criteria (§10.3b), public launch documentation (§10.4), and opt-in telemetry (§10.5).

## Behaviours

- [x] §10.2 Plugin / Extension API — Python entry-point groups for connectors, evals, model backends, schema types
- [x] §10.3 First-Run Experience — Pre-loaded demo pipeline, guided walkthrough, MODULO_DEMO_MODE
- [x] §10.3a Alpha Documentation — dev-setup.md, architecture.md, CONTRIBUTING.md
- [ ] §10.3b Alpha Exit Criteria — 6 conditions including walkable demo, green CI, non-demo pipeline, multi-user HITL
- [ ] §10.4 Documentation (Public Launch) — Quickstart, deployment guide, connector authoring guide, model backend guide, schema reference
- [x] §10.5 Opt-In Telemetry — OTel bridge enabled via MODULO_TELEMETRY_ENABLED, off by default

## Known Gaps

- **§10.3b Alpha Exit Criteria**: The `verify-alpha-exit.ps1` script exists but 5 of 6 criteria require human sign-off. No CI workflow triggers the report automatically.
- **§10.4 Documentation (Public Launch)**: The plugin API guide (`docs/plugin-api.md`) is comprehensive, but the full suite (quickstart, deployment guide, connector/model backend authoring guides, schema reference) is not yet assembled for public launch.
- **Onboarding state stored in local file, not DB**: `onboarding.py` uses `.onboarding-state.json` instead of the database. Per-instance, lost on restart, not shared across replicas.
- **4 of 7 plugin BDD scenarios tagged `@awaiting-implementation`**: No step definitions exist for plugin discovery, detail, startup scanning, or manifest validation.
- **SDLC onboarding BDD feature describes unimplemented 5-step flow**: The feature file at `backend/tests/bdd/features/onboarding/sdlc_onboarding.feature` describes a schema-inference-based flow that doesn't match the implemented 4-step general onboarding.
- **`modulo.evals` and `modulo.schema_types` entry-point groups documented but not implemented**: Only `modulo.connectors` and `modulo.model_backends` are supported.
- **Anonymous startup ping not implemented**: PRD §10.5 described a startup ping; only OTel exporter config exists.
