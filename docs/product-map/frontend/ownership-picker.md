---
id: feat-frontend-ownership-picker
prd: 9.3
delivery-tasks: [task-nv1-ownership-picker]
bdd:
  - backend/tests/bdd/features/workflows/import.feature
  - backend/tests/bdd/features/workflows/export.feature
  - backend/tests/bdd/features/library/copy_to_adapt.feature
code:
  - frontend/src/components/OwnershipPicker.vue
  - frontend/src/views/LibraryPipelineWizard.vue
unit-tests:
  - tests/unit/api/test_ownership_picker_bdd.py
  - tests/bdd/steps/test_ownership_picker.py
depends-on: [feat-teams-team-ownership]
status: partial
---

# Ownership Picker

Popover-based team ownership selector for pipelines, bundles, and library
primitives. Supports org-wide and team-scoped ownership with teams fetched
from the admin API.

## Behaviours

### Happy Paths (Selection Flow)

- [x] User opens popover and sees "Org-wide" option as first entry
- [x] Selecting "Org-wide" emits `{ owner_team_id: null, visibility: 'org' }` and closes popover
- [x] Selecting a team row emits `{ owner_team_id: team.id, visibility: 'team' }` and closes popover
- [x] Selected option highlighted in popover (bg-accent class) for both org-wide and team
- [x] Trigger label shows "Org-wide" when visibility=org
- [x] Trigger label shows team name when a team is selected and team exists
- [x] Trigger shows "Select ownership..." placeholder when modelValue is null/undefined
- [x] Popover opens on trigger click; closes on selection or outside click (radix-vue default)

### Loading States

- [x] Popover shows "Loading teams..." centered text while API fetch is in flight
- [x] Loading text replaces team list until fetch completes

### Error States

- [x] API failure displays "Failed to load teams" error message in popover in place of team list
- [x] Error message uses `text-destructive` colour
- [x] Error does not crash or break parent component
- [x] Teams section header and divider hidden when in error state

### Empty State

- [x] Zero teams in org: only "Org-wide" option shown, no divider, no "Teams" section header
- [x] Popover content never empty — always shows at least "Org-wide"

### Data Fetching

- [x] Teams fetched via `GET /api/v1/admin/teams` on component mount
- [x] Response items mapped from `AdminTeamItem` — `{ id, name, member_count }` per row
- [x] Each team row displays name and member count (singular/plural)
- [x] No caching or Pinia store — fresh fetch per mount

### UI / Interaction

- [x] ChevronDown icon rotates 180° when popover is open
- [x] Trigger button hover and focus-visible ring styles applied
- [x] Disabled state renders `disabled:cursor-not-allowed disabled:opacity-50`
- [x] Popover width matches trigger width (`--radix-popover-trigger-width`)
- [x] Label rendered above trigger when `label` prop is provided
- [x] Focus-visible visible on all interactive elements

### Integration (LibraryPipelineWizard consumer)

- [x] OwnershipPicker used in pipeline creation wizard with `v-model` bound to `ownership` ref
- [x] Default ownership on wizard mount: `{ owner_team_id: null, visibility: 'org' }`
- [x] `owner_team_id` and `visibility` sent to `POST /api/v1/libraries/{id}/create-pipeline` on create
- [x] Created pipeline carries the selected ownership fields

### BDD Coverage (backend — no frontend BDD exists)

- [x] Import assigns owner_team_id from bundle selection (import.feature:46-49)
- [x] Export strips owner_team_id from bundle (export.feature:21-24)
- [x] Copy-to-adapt propagates target_team_id as owner_team_id (copy_to_adapt.feature:21-23)

### Edge Cases

- [x] Very long team names do not break popover layout (flex-1 text shrink)
- [x] modelValue owner_team_id set to non-existent/deleted team — "Unknown team" label, not crash
- [x] Teams list reflects current state on each mount (fresh fetch per mount)
- [x] Popover closes cleanly on selection before API response arrives (race-safe)
- [x] modelValue optionally null — trigger shows placeholder, no active selection
- [x] `owner_team_id: null` + `visibility: 'team'` is an invalid state — handled or prevented

### Security

- [x] Picker uses `/api/v1/admin/teams` — non-admin users receive 403, mapped to fetchError
- [x] Ownership value applied literally by consumer — no client-side RBAC enforcement in picker

### Error Handling

- [x] API 403 error on non-admin user shown as "Failed to load teams" message in popover
- [x] API network failure shows error message, does not crash component
- [x] Missing visibility/owner_team_id modelValue gracefully shows placeholder text
- [x] Non-existent/deleted team owner_team_id shows "Unknown team" label (no crash)

## Known Gaps

### Test Coverage
- No dedicated frontend unit tests for OwnershipPicker component — always stubbed in parent tests
- No Playwright E2E test for ownership picker selection flow
- BDD coverage exists only for backend ownership propagation (import/export/copy-to-adapt) — no BDD for frontend picker interaction

### Component Limitations
- No `disabled` prop — CSS classes exist but are never triggered
- No search/filter for teams with many entries
- Popover items lack `disabled` state for teams the user cannot select
- `owner_team_id: null + visibility: 'team'` invalid state is not explicitly prevented at the component level (DB enforces at backend)
- No team/org Pinia store — data refetched on every mount with no caching

### UX
- Picker relies on `GET /api/v1/admin/teams` — non-admin users see an error rather than a read-only view
- No i18n for `label="Owner"` prop passed from parent views (LibraryPipelineWizard, PipelineTemplateGallery, CopyPipelineWizard)
