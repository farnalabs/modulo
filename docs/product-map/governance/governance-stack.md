---
id: feat-governance-stack
prd: N/A
adr: []
code:
  - frontend/src/views/GovernanceStackView.vue
  - frontend/src/manifest.yaml
  - frontend/src/config/navigation.ts
  - frontend/src/router/index.ts
unit-tests:
  - frontend/src/__tests__/GovernanceStackView.spec.ts
bdd: []
delivery-tasks: []
depends-on: []
status: covered
---

# Governance Stack (`/governance`)

A read-only, navigable **Governance Stack** page that expresses the
organisation's agentic stack as one hierarchy of levels. Each level shows what
exists today in Modulo's own primitives, with a live count and a
configured/empty/restricted state, and deep-links to the management surface
for that level.

This is naming and composition, not a new engine. Every level is backed by an
entity and a list API that already exists; the page composes those existing
endpoints client-side (the established `SettingsMcpView` / `AdminCostControlsView`
pattern). There is **no new backend endpoint, no migration, and no API-schema
change**, so `pnpm run generate:api` is not required.

## The seven levels

| # | Level | Modulo primitive | Backing API | Management surface |
|---|---|---|---|---|
| 1 | Models | Model Backends | `GET /api/v1/model-backends` | `/admin/model-backends` |
| 2 | Tools | Connectors | `GET /api/v1/connectors` | `/admin/connectors` |
| 3 | MCP | MCP access surface: OAuth clients + API keys | `GET /api/v1/mcp/oauth/clients`, `GET /api/v1/api-keys` | `/settings/mcp` |
| 4 | Agents | Agents (prompt templates, runner bindings) | `GET /api/v1/agents` | `/library` (no dedicated page) |
| 5 | Commands | Composite templates + library primitives | `GET /api/v1/composite-templates`, `GET /api/v1/libraries` | `/library` |
| 6 | Autonomy | Earned-autonomy ladder per pipeline | `GET /api/v1/pipelines` | `/pipelines` |
| 7 | Telemetry | Observability: OTel export + error dashboard + notification endpoints | `GET /api/v1/settings/observability`, `GET /api/v1/errors`, `GET /api/v1/notifications` | `/settings/observability`, `/admin/errors`, `/admin/notification-delivery` |

## Behaviours

- [x] `/governance` renders the seven-level hierarchy as one read-only
      inventory, each level composing an existing list API client-side.
- [x] Each level loads independently and shows a live count plus a
      configured/empty state, or its own `restricted` (402 feature-gated / 403
      permission) state, or a retry-able per-level error state. One level's
      failure or restriction never blanks another level or the page.
- [x] Levels deep-link to their existing management surfaces (the table above).
- [x] Level 3 (MCP) today is what Modulo *serves* to agents (OAuth clients,
      API keys); Level 4 (Agents) is agents Modulo dispatches; Level 5
      (Commands) is a rename over composite templates + library primitives.
- [x] Level 6 (Autonomy) summarises the pipeline list as total pipelines plus
      the distribution across the three rungs from `earned_autonomy_level`,
      falling back to `default_autonomy_level` when earned is null.
- [x] Plan-gated levels reuse the same `FeatureGate` as their management page
      (`model_backend_management`, `plugin_management`, `mcp_server`,
      `observability`); every user-facing string is i18n-backed and per-level
      status regions use `role="status"` / `aria-live` (assertive for errors).
- [x] Covered by the `GovernanceStackView` Vitest spec.

## Known Gaps

- Upstream MCP-server inventory and a pinned tool-definition allowlist are not
  yet governed (planned for Beta); the MCP card states the boundary and draws
  no fabricated data.
- An org-wide inventory of agents Modulo did not dispatch is not yet governed
  (planned for Beta); the Agents card states the boundary.
- The interactive earned-autonomy panel and promote/demote controls are out of
  scope here (a separate slice); this surface is a readout only.

## Data sources

All values are read live from the list endpoints in the table above; counts use
the `items`/`total` pagination envelope each list endpoint already returns. No
data is fabricated for an ungoverned surface: a level whose backing primitive
does not yet exist carries a clearly-labelled "not yet governed" note instead
of a number.
