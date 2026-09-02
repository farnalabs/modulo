---
id: feat-plugins
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/plugins.py
  - backend/src/modulo/core/plugin_registry
unit-tests:
  - backend/tests/unit/api/test_plugin_registry_bdd.py
  - backend/tests/unit/plugin_registry/test_plugin_registry.py
bdd:
  - backend/tests/bdd/features/plugins/plugin_registry.feature
depends-on:
  - feat-connectors
  - feat-model-backends
status: covered
---

# Plugin Registry

_Deferred from the MVP nav (hidden via `visibility: private_preview`). Behaviour tracker removed for the MVP cut — restore from git history when re-enabling. See FAR-544._
