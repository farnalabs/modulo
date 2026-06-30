---
id: feat-library-auto-update
prd: 8.14
delivery-tasks: [task-prd-community-library-no-auto-update]
code:
  - backend/src/modulo/core/library_service/__init__.py
  - backend/src/modulo/db/models/library_primitive.py
  - backend/src/modulo/db/crud/library_primitive.py
  - backend/src/modulo/api/routes/library.py
  - frontend/src/views/LibraryView.vue
bdd:
  - backend/tests/bdd/features/library/auto_update.feature
depends-on: [feat-core-contribution-update]
status: covered
---
# Community Library Auto-Update Control

Users can disable auto-update on adapted community library primitives to pin a specific version. When auto-update is off, `notify_importers_of_update` skips the fork copy, so `update_available_version_id` stays null.

## Behaviours

### Model & DB
- [x] `auto_update` column exists on `library_primitives` with default `TRUE` and NOT NULL
- [x] Migration `0044_library_auto_update.py` adds the column
- [x] `auto_update` is not in `_IMMUTABLE_FIELDS` so it can be updated via PATCH

### API — Response and Update
- [x] `LibraryPrimitiveResponse` includes `auto_update` field (defaults to `True`)
- [x] `LibraryPrimitiveUpdate` accepts `auto_update: bool | None` — omitting it leaves the current value unchanged

### API — Copy-to-adapt
- [x] Adapted primitives are created with `auto_update=True` by default
- [x] `create_library_primitive` CRUD accepts `auto_update` parameter (default `True`)

### Update notification respect
- [x] `notify_importers_of_update` skips fork copies with `auto_update=False`
- [x] Fork copies with `auto_update=True` (default) still receive `update_available_version_id`

### Frontend
- [x] Library card shows auto-update toggle (toggle switch) on adapted primitives (those with `forked_from`)
- [x] Toggle calls `PATCH /api/v1/libraries/{id}` with `auto_update` value
- [x] Toggle updates optimistically in the UI
- [x] Only shown for forked/imported primitives, not community or original org primitives

### States
- [x] Default: auto_update is true for new adapted primitives
- [x] Toggle off: auto_update set to false, primitive pinned to current version
- [x] Toggle on: auto_update set to true, primitive will receive future update notifications
- [x] Community primitives do not show the toggle (they are read-only in the UI context)

### API endpoints
- [x] `PATCH /api/v1/libraries/{id}` accepts `auto_update` in body

## Known Gaps

- No integration test verifying that `notify_importers_of_update` actually skips auto_update=false copies in a real DB transaction
- No Playwright test for the frontend toggle interaction
