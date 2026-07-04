---
id: feat-core-runtime-config
prd: 6
delivery-tasks:
  - task-nv18-runtime-config-backend
  - task-nv18-runtime-config-frontend
code:
  - backend/src/modulo/core/runtime_config/
  - backend/src/modulo/api/routes/admin_runtime_config.py
  - frontend/src/views/SettingsRuntimeConfigView.vue
bdd: []
depends-on: []
unit-tests: []
status: covered
---

# Runtime Configuration UI

RuntimeConfigStore — inspect and override application configuration at runtime
without restarting the server. Displays config values with provenance tracking
(default / environment / override), drift detection between in-memory and
environment-variable values, and hot-reloadable vs static settings.

## Behaviours

### Happy Paths

- [x] GET /api/v1/admin/runtime-config returns all known config keys with current value, env value, default, provenance, and hot-reloadable flag
- [x] PUT with overrides dict stores in-memory overrides that take effect immediately
- [x] PUT with clear list removes overrides for specified keys
- [x] POST /reload re-reads os.environ and detects drift
- [x] has_drift flag is true when env value differs from current value on non-overridden keys
- [x] Admin-only access (403 for non-admin users)
- [x] Frontend table renders all keys with editable inputs for hot-reloadable configs
- [x] Frontend shows "Apply" button only after editing a value
- [x] Frontend shows "Reset" button when an override exists
- [x] Frontend shows drift warning banner when has_drift is true
- [x] Frontend displays colour-coded provenance badges (blue=override, purple=env, gray=default)
- [x] Frontend displays hot/static badges with tooltip explaining restart requirement

### Edge Cases

- [x] Override set to empty string is stored (clearing = explicit unset via clear list)
- [x] Unknown key in overrides dict is stored but not returned in get_all (only KNOWN_KEYS are returned)
- [x] Reload called with no env var changes returns has_drift false
- [x] All overrides cleared returns provenance to environment or default
- [x] Frontend error state on API failure with retry
- [x] Frontend loading state during API calls


## Known Gaps

- No known gaps documented yet
