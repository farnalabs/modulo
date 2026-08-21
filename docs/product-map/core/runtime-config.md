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
bdd:
  - backend/tests/bdd/features/admin/runtime-config.feature
depends-on: []
unit-tests:
  - backend/tests/unit/core/runtime_config/test_store.py
  - backend/tests/unit/api/test_runtime_config_routes.py
status: partial
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
- [x] Unknown key in overrides returns 400 with descriptive error message
- [x] Reload called with no env var changes returns has_drift false
- [x] All overrides cleared returns provenance to environment or default
- [x] Frontend error state on API failure with retry
- [x] Frontend loading state during API calls
- [x] Non-dict overrides body rejected with 400
- [x] Non-string override value rejected with 400
- [x] Non-list clear body rejected with 400
- [x] Non-string clear key rejected with 400
- [x] Unknown key in clear list rejected with 400
- [x] Keys with no default return None (no env, no override)

### Error Handling

- [x] GET route returns 500 with descriptive message on unexpected Exception (RuntimeError, ValueError, TypeError)
- [x] PUT route returns 500 with descriptive message on unexpected Exception
- [x] POST /reload route returns 500 with descriptive message on unexpected Exception
- [x] All 3 routes propagate HTTPException without transformation (except HTTPException: raise)
- [x] All 3 routes log exception via logger.exception before returning 500

### Resilience

- [x] Sensitive values (SECRET_KEY, FERNET_KEY, DATABASE_URL, etc.) masked in API response as `●●●●●●`
- [x] Sensitive masking applied to all four value fields (current, default, env, override)
- [x] Unknown key in overrides logged as warning but does not crash the request
- [x] Reload does not clear existing overrides (override > env priority preserved)
- [x] Empty-string defaults handled correctly (no falsey fallback)
- [x] RLock ensures thread-safe concurrent access to store state
- [x] Hot-reloadable flag is consistent with HOT_RELOADABLE_KEYS set

## Known Gaps

- No website docs page exists for runtime configuration (needs Website repo worktree)
- No degraded-mode fallback — RuntimeConfigStore is process-global with no persistence layer (verified: `core/runtime_config/store.py` holds key config in a module-level dict; nothing is written to disk/DB, so a restart loses runtime overrides)
- Route handler has no DB error handling (in-memory store, so no 501/503 paths exist)

## QA History

### 2026-07-08 — Cross-cutting QA (index 268)

**CRITICAL fixes applied:**
- All 3 route handlers (GET, PUT, POST /reload) in `admin_runtime_config.py` added `try/except Exception → 500` with `except HTTPException: raise` guard — previously Python-level errors (TypeError, KeyError, ValueError) propagated to CatchAllMiddleware as opaque 500 with no structured detail.

**MAJOR fixes applied:**
- Added Error Handling section (5 behaviour checkboxes covering exception→500 for all 3 routes, HTTPException propagation, and logging)
- Added Resilience section (7 behaviour checkboxes covering sensitive masking, unknown-key warning, reload override preservation, thread safety)
- Expanded Edge Cases section from 6 to 12 checkboxes (added non-dict overrides, non-string value, non-list clear, non-string clear key, unknown clear key, no-default keys)
- Resolved 2 stale Known Gaps: (1) "Frontend i18n gaps: Key, Default..." — all template strings are `$t()` wrapped; (2) "Backend is_sensitive_env_key does not match DATABASE_URL" — `DATABASE_URL` is in `_SENSITIVE_ENV_KEYS` set and matches `is_sensitive_key()` pattern
- Added 10 unit tests in `test_runtime_config_routes.py` covering exception→500 for all 3 routes and all PUT input validation paths
- Added `test_runtime_config_routes.py` to `unit-tests` frontmatter

**Status:** partial (3 known gaps remain)

### 2026-08-15 — coverage sweep (partial-small-a)

- Confirmed the 3 Known Gaps are genuine (all verified against `core/runtime_config/store.py` + `admin_runtime_config.py`): (1) no website docs page; (2) store is process-global with no persistence layer — module-level dict, nothing persisted to disk/DB; (3) no DB error handling because the store is in-memory (no 501/503 paths exist by design). None are PRD-mandated for the current MVP. Converted the 3 unchecked gap checkboxes to plain Known Gap bullets (gaps belong in Known Gaps, not the behaviour checklist). Status: partial (36/39 — all remaining unchecked items are documented gaps).
