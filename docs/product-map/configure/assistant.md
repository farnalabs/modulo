---
id: feat-assistant
prd: 8.23
adr:
  - ADR 007 (remy-ui-commands)
  - ADR 011 (remy-context-sources)
  - ADR 014 (remy-mcp-api-key-jwt)
code:
  - backend/src/modulo/api/routes/assistant.py
  - backend/src/modulo/api/routes/admin_assistant.py
  - backend/src/modulo/core/assistant
unit-tests:
  - backend/tests/unit/api/test_me_assistant_skills.py
bdd:
  - backend/tests/bdd/features/assistant
depends-on:
  - feat-model-backends
status: covered
---

# Assistant Configuration & Skills

Assistant is the in-app AI assistant (PRD §8.23). The user-facing surface
(`/api/v1/assistant/sessions*`, `/assistant`) provides persistent multi-turn sessions with SSE
streaming, message CRUD, permission responses, UI-command results and
permission resets (ADR 007 / ADR 011). The admin surface (`/api/v1/admin/assistant`,
`/admin/assistant`, `/settings/assistant`) manages org-level assistant config (provider,
model, system prompt, access list), reusable org/user skills, and context sources.
Access control restricts who may use Assistant (explicit user/role allow-list; admins and
listed org roles always granted).

## Behaviours

- [x] Sessions: `GET/POST /api/v1/assistant/sessions`, `GET/PATCH/DELETE
      /api/v1/assistant/sessions/{id}` — create, rename, list and delete sessions; delete
      removes the session and its messages
- [x] Messages: `GET /api/v1/assistant/sessions/{id}/messages` lists paginated messages and
      `POST` appends a user message
- [x] Streaming: `POST /api/v1/assistant/sessions/{id}/stream` returns an SSE stream of the
      LLM response for the session history
- [x] Assistant loops back to the user for permission prompts
      (`/permission-response`), executes UI commands (`/ui-command-results`, ADR 007)
      and resets accumulated permissions (`/reset-permissions`)
- [x] Admin config: `GET/PUT /api/v1/admin/assistant/config` reads and updates the
      assistant's provider/model/prompt/access settings with feature-flag + org
      scoping; `GET /available-providers` lists selectable providers
- [x] Admin skills CRUD (`GET/POST /skills`, `PUT/DELETE /skills/{id}`): org-level
      reusable skills named `org:...`, listable by users (`/api/v1/assistant/skills` via
      `test_me_assistant_skills.py`); skills inject instructions into the assistant
      context (ADR 011)
- [x] Context sources: `GET/PUT /context-sources/{key}` and `DELETE /context-sources`
      manage the extra context injected into the assistant window
- [x] Access control: the access list grants explicit user ids and org roles, admins
      always have access, and blocked users are refused (`assistant_access_control.feature`)
- [x] Auto-execute thresholds ship end to end: the org assistant config carries
      `auto_execute_threshold` (default 0.8, `AssistantConfig` in
      `core/assistant/config_service.py`); in `full_auto` permission mode a proposed
      action whose reported confidence is below the threshold is demoted to
      `requires_approval` (`_default_tool_permission` in `api/routes/assistant.py`);
      the `/admin/assistant` safety panel reads and writes the field and explains it
      via the auto-execute-threshold description (`AdminAssistantView.vue`,
      `AdminAssistantView.spec.ts`)
- [x] Guidance tuning ships end to end: `additional_guidance` is a first-class field on
      the admin assistant config (`GET/PUT /api/v1/admin/assistant/config`, model in
      `core/assistant/config_service.py`), edited through the Additional Guidance
      textarea in the admin settings view and asserted on the PUT body
      (`AdminAssistantView.spec.ts`)
- [x] BDD coverage across sessions, messages, context window, context sources, skills,
      admin config, UI commands and access control (`backend/tests/bdd/features/assistant/`)

## Known Gaps

- **No PRD section reference for the plugin/registry-adjacent assistant tool surface** — the
  MCP API-key/JWT binding is tracked under `feat-mcp` (ADR 014), not here.
- **Test breadth** — the user-session streaming/SSE surface is BDD-covered at the
  feature-file level; deeper unit coverage for the permission round-trips lives in
  `backend/tests/unit/api/test_me_assistant_skills.py` only.

## QA History
- 2026-09-26: **Improve Architecture product-map walk** — closed `feat-assistant`'s
  stale "auto-execute thresholds and guidance tuning are partially wired" gap. Both
  surfaces ship end to end: `auto_execute_threshold` (AssistantConfig default 0.8)
  is enforced in `_default_tool_permission` (`api/routes/assistant.py`) — a
  `full_auto` action below the threshold is demoted to `requires_approval` — and is
  read/written by the `/admin/assistant` safety panel; `additional_guidance` is a
  first-class `GET/PUT /api/v1/admin/assistant/config` field edited from the
  Additional Guidance textarea. The manifest registry entry is now `status: covered`
  with both behaviours ticked.
- 2026-09-12: **product-map review pass** — registered the shared
  `AnalyticsChart` surface (`components/analytics/AnalyticsChart.vue` static testids
  `analytics-chart` / `analytics-chart-canvas` / `analytics-chart-empty`) in the
  manifest `elements:` inventory for `/assistant`: `AssistantChat.vue` renders
  `<AnalyticsChart>` for analytics-chart conversation turns, so the chart shipped
  in the DOM while staying invisible to Assistant's docs indexer / `/api/v1/manifest`.
  The component is now part of the route's reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`).

- 2026-09-12: **product-map review pass** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/assistant`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/settings/assistant`: the whole-page view(s) `UserAssistantSkillsView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** — registered the
  Assistant chat surface on the `/assistant` manifest `elements:` inventory: the page
  already documented its `AssistantOnlyView.vue` chrome (`assistant-only-*`) but not the
  `assistant-analytics-card` static testid its embedded chat panel
  (`components/assistant/AssistantChat.vue`) ships. `test_mapped_route_elements_cover_owning_view_testids`
  now maps `/assistant` to `AssistantOnlyView.vue` + `AssistantChat.vue` so the chat surface
  cannot drift invisible to Assistant's docs indexer / `/api/v1/manifest`.
- 2026-08-28: **product-map review pass** — added this behaviour-tracker
  for the registered manifest feature `feat-assistant`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/assistant.py`,
  `api/routes/admin_assistant.py`, `core/assistant/*` and the `backend/tests/bdd/features/assistant/`
  suite. Status: covered.
