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

- [ ] User opens popover and sees "Org-wide" option as first entry
- [ ] Selecting "Org-wide" emits `{ owner_team_id: null, visibility: 'org' }` and closes popover
- [ ] Selecting a team row emits `{ owner_team_id: team.id, visibility: 'team' }` and closes popover
- [ ] Selected option highlighted in popover (bg-accent class) for both org-wide and team
- [ ] Trigger label shows "Org-wide" when visibility=org
- [ ] Trigger label shows team name when a team is selected and team exists
- [ ] Trigger shows "Select ownership..." placeholder when modelValue is null/undefined
- [ ] Popover opens on trigger click; closes on selection or outside click (radix-vue default)

### Loading States

- [ ] Popover shows "Loading teams..." centered text while API fetch is in flight
- [ ] Loading text replaces team list until fetch completes

### Error States

- [ ] API failure displays "Failed to load teams" error message in popover in place of team list
- [ ] Error message uses `text-destructive` colour
- [ ] Error does not crash or break parent component
- [ ] Teams section header and divider hidden when in error state

### Empty State

- [ ] Zero teams in org: only "Org-wide" option shown, no divider, no "Teams" section header
- [ ] Popover content never empty — always shows at least "Org-wide"

### Data Fetching

- [ ] Teams fetched via `GET /api/v1/admin/teams` on component mount
- [ ] Response items mapped from `AdminTeamItem` — `{ id, name, member_count }` per row
- [ ] Each team row displays name and member count (singular/plural)
- [ ] No caching or Pinia store — fresh fetch per mount

### UI / Interaction

- [ ] ChevronDown icon rotates 180° when popover is open
- [ ] Trigger button hover and focus-visible ring styles applied
- [ ] Disabled state renders `disabled:cursor-not-allowed disabled:opacity-50`
- [ ] Popover width matches trigger width (`--radix-popover-trigger-width`)
- [ ] Label rendered above trigger when `label` prop is provided
- [ ] Focus-visible visible on all interactive elements

### Integration (LibraryPipelineWizard consumer)

- [ ] OwnershipPicker used in pipeline creation wizard with `v-model` bound to `ownership` ref
- [ ] Default ownership on wizard mount: `{ owner_team_id: null, visibility: 'org' }`
- [ ] `owner_team_id` and `visibility` sent to `POST /api/v1/libraries/{id}/create-pipeline` on create
- [ ] Created pipeline carries the selected ownership fields

### BDD Coverage (backend — no frontend BDD exists)

- [ ] Import assigns owner_team_id from bundle selection (import.feature:46-49)
- [ ] Export strips owner_team_id from bundle (export.feature:21-24)
- [ ] Copy-to-adapt propagates target_team_id as owner_team_id (copy_to_adapt.feature:21-23)

### Edge Cases

- [ ] Very long team names do not break popover layout (flex-1 text shrink)
- [ ] modelValue owner_team_id set to non-existent/deleted team — "Unknown team" label, not crash
- [ ] Teams list reflects current state on each mount (fresh fetch per mount)
- [ ] Popover closes cleanly on selection before API response arrives (race-safe)
- [ ] modelValue optionally null — trigger shows placeholder, no active selection
- [ ] `owner_team_id: null` + `visibility: 'team'` is an invalid state — handled or prevented

### Security

- [ ] Picker uses `/api/v1/admin/teams` — non-admin users receive 403, mapped to fetchError
- [ ] Ownership value applied literally by consumer — no client-side RBAC enforcement in picker

## Known Gaps

- No unit tests for OwnershipPicker component exist
- No BDD feature file for the frontend picker flow (only backend ownership propagation tested)
- No validation of the `owner_team_id: null + visibility: 'team'` invalid state
- No team/org Pinia store — data is refetched on every mount with no caching
- Picker relies on `GET /api/v1/admin/teams` — non-admin users see an error rather than a read-only view
- No search/filter for teams with many entries
- Popover items lack a `disabled` state for teams the user cannot select
- `depends-on` references `feat-teams-team-ownership` (product map feature ID);
  the value was previously the delivery task `task-nv1-team-ownership`
