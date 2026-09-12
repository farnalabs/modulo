---
id: feat-remy
prd: 8.23
adr:
  - Repos/devtools/adr/007-remy-ui-commands.md
  - Repos/devtools/adr/011-remy-context-sources.md
  - Repos/devtools/adr/014-remy-mcp-api-key-jwt.md
code:
  - backend/src/modulo/api/routes/remy.py
  - backend/src/modulo/api/routes/admin_remy.py
  - backend/src/modulo/core/remy
unit-tests:
  - backend/tests/unit/api/test_me_remy_skills.py
bdd:
  - backend/tests/bdd/features/remy
depends-on:
  - feat-model-backends
status: covered
---

# Remy Assistant Configuration & Skills

Remy is the in-app AI assistant (PRD §8.23). The user-facing surface
(`/api/v1/remy/sessions*`, `/remy`) provides persistent multi-turn sessions with SSE
streaming, message CRUD, permission responses, UI-command results and
permission resets (ADR 007 / ADR 011). The admin surface (`/api/v1/admin/remy`,
`/admin/remy`, `/settings/remy`) manages org-level assistant config (provider,
model, system prompt, access list), reusable org/user skills, and context sources.
Access control restricts who may use Remy (explicit user/role allow-list; admins and
listed org roles always granted).

## Behaviours

- [x] Sessions: `GET/POST /api/v1/remy/sessions`, `GET/PATCH/DELETE
      /api/v1/remy/sessions/{id}` — create, rename, list and delete sessions; delete
      removes the session and its messages
- [x] Messages: `GET /api/v1/remy/sessions/{id}/messages` lists paginated messages and
      `POST` appends a user message
- [x] Streaming: `POST /api/v1/remy/sessions/{id}/stream` returns an SSE stream of the
      LLM response for the session history
- [x] Assistant loops back to the user for permission prompts
      (`/permission-response`), executes UI commands (`/ui-command-results`, ADR 007)
      and resets accumulated permissions (`/reset-permissions`)
- [x] Admin config: `GET/PUT /api/v1/admin/remy/config` reads and updates the
      assistant's provider/model/prompt/access settings with feature-flag + org
      scoping; `GET /available-providers` lists selectable providers
- [x] Admin skills CRUD (`GET/POST /skills`, `PUT/DELETE /skills/{id}`): org-level
      reusable skills named `org:...`, listable by users (`/api/v1/remy/skills` via
      `test_me_remy_skills.py`); skills inject instructions into the assistant
      context (ADR 011)
- [x] Context sources: `GET/PUT /context-sources/{key}` and `DELETE /context-sources`
      manage the extra context injected into the assistant window
- [x] Access control: the access list grants explicit user ids and org roles, admins
      always have access, and blocked users are refused (`remy_access_control.feature`)
- [x] BDD coverage across sessions, messages, context window, context sources, skills,
      admin config, UI commands and access control (`backend/tests/bdd/features/remy/`)

## Known Gaps

- **No PRD section reference for the plugin/registry-adjacent remy tool surface** — the
  MCP API-key/JWT binding is tracked under `feat-mcp` (ADR 014), not here.
- **Test breadth** — the user-session streaming/SSE surface is BDD-covered at the
  feature-file level; deeper unit coverage for the permission round-trips lives in
  `backend/tests/unit/api/test_me_remy_skills.py` only.

## QA History
- 2026-09-12: **improve-architecture (product-map walk)** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/remy`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Remy's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **improve-architecture (product-map walk)** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/settings/remy`: the whole-page view(s) `UserRemySkillsView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Remy's docs indexer /
  `/api/v1/manifest`.

- 2026-09-11: **improve-architecture (product-map walk)** — registered the
  Remy chat surface on the `/remy` manifest `elements:` inventory: the page
  already documented its `RemyOnlyView.vue` chrome (`remy-only-*`) but not the
  `remy-analytics-card` static testid its embedded chat panel
  (`components/remy/RemyChat.vue`) ships. `test_mapped_route_elements_cover_owning_view_testids`
  now maps `/remy` to `RemyOnlyView.vue` + `RemyChat.vue` so the chat surface
  cannot drift invisible to Remy's docs indexer / `/api/v1/manifest`.
- 2026-08-28: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-remy`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/remy.py`,
  `api/routes/admin_remy.py`, `core/remy/*` and the `backend/tests/bdd/features/remy/`
  suite. Status: covered.
