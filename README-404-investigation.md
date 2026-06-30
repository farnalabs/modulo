# 404 Route Investigation

## Routes tested

### `GET /api/v1/admin/runs/retention`
- **Result:** 500 Internal Server Error (was reported as 404)
- **Root cause:** Query uses `Run.settings_json` but `Run` model has no `settings_json` column. This column exists on `Organisation`.
- **Fix:** Changed to `select(Organisation.settings_json).where(Organisation.id == current_user.organisation_id)`
- **File:** `backend/src/modulo/api/routes/admin.py:1978`

### `GET /api/v1/admin/runs/storage`
- **Result:** 200 OK — works correctly
- **Deployment SHA:** `1012bc0` (contains the routes)

### `GET /api/v1/admin/costs/controls`
- **Result:** 200 OK — works correctly
- **Deployment SHA:** `1012bc0` (contains the routes)

## Summary

Two of the three routes (`/runs/storage` and `/costs/controls`) work correctly in the production deployment at `1012bc0`.

The third route (`/runs/retention`) was returning a 500 error (not a 404) because it queried a non-existent column `Run.settings_json`. The `settings_json` column exists on the `Organisation` model, not `Run`. Fixed by correcting the model reference and column name.
