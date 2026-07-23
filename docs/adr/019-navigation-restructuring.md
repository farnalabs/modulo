# ADR 019: Navigation Restructuring

**Date:** 2026-07-22
**Status:** Accepted

## Context

The Modulo sidebar navigation had grown to ~35 items across 11 labelled groups (Core, Analysis, Evals, Schemas, Remy, Settings, Access Control, Cost Management, System, Monitoring, Extensions), controlled by a Simple/Advanced view mode toggle, a separate Dev Mode toggle, plan tier gating, and role-based visibility. This created a 5-dimensional visibility model that overwhelmed new users and made the product feel complex before they'd run their first pipeline.

Key problems identified:

1. **Information overload:** 35 items with 11 group headers. The Settings group alone had 12 items.
2. **Duplication:** The Evals group sidebar items (4 items) were also rendered as tabs on every evals page. Same for Cost Management (3 items × 2 places) and Schemas (3 items × 2 places).
3. **Serial position effect:** 12 Settings items meant items #4-9 were rarely noticed.
4. **Cognitive friction:** Simple/Advanced toggle, Dev Mode toggle, plan tier, role — 4 different reasons an item might be invisible, with no explanation.
5. **Low information scent:** Labels like "Output Diff" and "Housekeeping" didn't tell users what they'd find.

Three independent antagonistic UX reviews were conducted over the course of the design. Each was a completely fresh context with no awareness of prior rounds, ensuring maximum coverage of potential issues. The final plan addresses every ?? finding from all three reviews.

## Decision

### Sidebar Structure

The sidebar is restructured from 11 grouped sections to a flat list of 12 items across 4 visual sections:

```
 +- CORE ---------------------------------------------+
 ¦  ? Dashboard              /dashboard               ¦
 ¦  ? Cost Management        /admin/costs             ¦
 ¦  ? Pipelines              /pipelines               ¦
 ¦  ? Runs                   /runs                    ¦
 ¦  ? Evaluations            /evals/editor            ¦
 +- TOOLS --------------------------------------------¦
 ¦  ? Schemas                /schemas                 ¦
 ¦  ? Connectors             /admin/connectors        ¦
 +- SETTINGS -----------------------------------------¦
 ¦  ? Workspace Config       /settings/teams          ¦
 ¦  ? Integrations Config    /settings/mcp            ¦
 ¦  ? Developer Config       /settings/remy           ¦
 +- ADMIN --------------------------------------------¦
 ¦  ? Organization           /admin/users             ¦
 ¦  ? System                 /admin/feature-flags     ¦
 +----------------------------------------------------+
```

- **2 titled group headers** (CORE, SETTINGS) — visual grouping without collapse/expand
- **2 divider labels** (TOOLS, ADMIN) — subtle small-caps labels, not interactive
- **12 items total** — hard cap enforced by CI
- **No Simple/Advanced view mode toggle** — deleted entirely
- **No grouping collapse** — not needed at this item count

### Section Details

**CORE** — primary workflow items users interact with daily.
- Dashboard, Cost Management (moved from admin to core per UX feedback that billing visibility is important), Pipelines, Runs, Evaluations (renamed from "Evals" per UX feedback that the abbreviated form was jargon).

**TOOLS** — supporting infrastructure for the core workflows.
- Schemas, Connectors (both were previously in separate groups; Connectors absorbs Model Backends as a tab).

**SETTINGS** — all configuration items, split into 4 sidebar items to avoid the 14-tab problem that all three UX reviews flagged as critical.
- Workspace Config (Teams, License, SSO, Runtime Config, Rate Limits)
- Integrations Config (MCP, Email, Error Forwarders)
- Developer Config (Remy Skills*, HITL Review*, Feedback Inbox*, Browser Monitoring, Observability*)

**ADMIN** — administrative functions, split from the old single Admin section that had 6 tabs (flagged as overflow by UX review).
- Organization (Users, Org Settings, Audit Log)
- System (Feature Flags, Housekeeping, Error Dashboard)

### Sub-Navigation (PageTabs)

PageTabs become the primary sub-navigation mechanism. When a user clicks a sidebar item, the destination page shows a prominent pill-style tab bar. Items previously in the sidebar as separate entries now appear as tabs within their section.

| Sidebar Item | Tabs | Count |
|---|---|---|
| Dashboard | — | 0 |
| Cost Management | Overview, Spend Limits, Cost Controls | 3 |
| Pipelines | My Pipelines, Library, Stages Board, Lifecycle Maps, Triggers | 5 |
| Runs | Run History, Output Diff | 2 |
| Evaluations | Evals, Proposals*, Variants*, AB Test* | 4 |
| Schemas | Browse, Editor, Infer* | 3 |
| Connectors | Connectors, Model Backends | 2 |
| Workspace Config | Teams, License, SSO, Runtime Config, Rate Limits | 5 |
| Integrations Config | MCP, Email, Error Forwarders | 3 |
| Developer Config | Remy Skills*, HITL Review*, Feedback Inbox*, Browser Monitoring, Observability* | 5 |
| Organization | Users, Org Settings, Audit Log | 3 |
| System | Feature Flags, Housekeeping, Error Dashboard | 3 |

(*) = dev-mode gated tab, hidden when dev mode off

Max tabs in any single row: 5 (Pipelines, Workspace Config).

### PageTabs Visual Design

The PageTabs component receives a visual overhaul:

1. **Pill-style layout:** Tabs render as distinct button-like pills in a muted container, not as a thin text-only bar. The active tab has an elevated card appearance (border + shadow + background).
2. **Icon support:** Tabs may include optional Lucide icons for visual scannability.
3. **Badge support:** Optional count/status badges (e.g., "Runs (12)").
4. **Scroll-fade overflow:** When tabs exceed container width, a gradient fade indicates scrollability. No wrapping.
5. **Overflow dropdown:** At configurable threshold, extra tabs collapse into a "More" dropdown (DropdownMenu component).

### Notification Bell

A notification bell is added to the app header (replacing the old sidebar Notifications item):
- **Unfilled** — no notifications
- **Gold filled** — pending notifications
- **Bright red filled** — error-level notifications
- Clicking always navigates to `/notifications`

This is Phase 1. Phase 2 (deferred) adds a dropdown menu with WebSocket-delivered events.

### Dev Mode Gating

Dev mode (controlled via MODULO_DEV_MODE env var or Admin > Feature Flags toggle) gates experimental tabs:

- **Dev mode OFF:** Preview/gated tabs are NOT rendered at all. Tab bar adjusts shape. Sidebar items are also hidden. No badge or "experimental" label — the item simply doesn't exist for the user.
- **Dev mode ON:** Tabs render normally with no experimental badge. Clean appearance.

Gated items: Proposals, Variants, AB Test, Schema Infer, Remy Skills, HITL Review, Feedback Inbox, Observability.

### Tier Gating

Tier-gated tabs (team-tier features visible to community-tier users) are shown with a padlock icon. Clicking shows a tooltip: "Available on the Team plan" with a link to `/settings/license`. Tab bar keeps its shape — locked tabs take up space. This was the consensus resolution after two reviewers disagreed on whether to show or hide locked items.

### Cmd+K Command Palette

A command palette (Cmd+K / Ctrl+K) ships alongside the restructure:
- Phase 1: fuzzy search of nav items + tab labels
- Phase 2 (deferred): search user data — pipeline names, run IDs, schema names, connector names, recently visited items

### Mobile

Mobile uses a bottom tab bar (5 items: Dashboard, Pipelines, Runs, Settings, More). The "More" tab is an overflow drawer with remaining items. Page tabs become horizontally scrollable with minimum 44px touch targets. Settings items collapse to a select dropdown.

### Growth Cap

The navigation is structurally protected by CI-enforced rules:
- Maximum 12 sidebar items
- Maximum 5 visible tabs per section
- Maximum 8 total tabs per section
- CI reads manifest.yaml and fails on violation

## Consequences

### Positive

1. **Reduced cognitive load:** 12 items flat vs 35 items in 11 groups — 66% reduction in scanning surface.
2. **Eliminated duplication:** No item appears in both sidebar and tabs. PageTabs are the sub-nav; sidebar is the top-level section selector.
3. **Eliminated view mode toggle:** Simple/Advanced removed entirely, removing one dimension of visibility confusion.
4. **Cleaner information architecture:** Related items grouped under intuitive parent labels (Pipelines gets Lifecycle Maps and Triggers as tabs — both are pipeline-specific features).
5. **Scanner-friendly Settings:** 4 sidebar items vs 14 flat tabs — each section has 2-5 tabs (within Material Design 3 recommendations).
6. **Notifications visible:** Bell in header is always accessible, not buried in sidebar.
7. **Growth safe:** Hard cap of 12 items prevents silent bloat.

### Negative

1. **Re-learning curve:** Users who memorised the 11-group layout will need to learn the new structure. Mitigated by one-time onboarding overlay post-migration.
2. **Serendipitous discovery reduced:** Users scanning the sidebar won't discover features they don't know to look for. Mitigated by Cmd+K search and onboarding overlay.
3. **Tab bar shape changes with dev mode:** When dev mode is toggled, up to 8 tabs appear/disappear across 4 sections. This is an accepted trade-off — users who toggle dev mode are power users who understand the scope change.
4. **Notifications deferred:** Phase 1 has only a bell + link to /notifications page. Full dropdown + WebSocket delivery is deferred to Phase 2.
5. **Cost Management as a top-12 item:** UX feedback flagged this as not a daily concern, but the decision to keep it at top level was confirmed based on its importance for team leads and org admins who need quick billing access.

## Related Documents

- PR #233: Dev mode toggle + preview gating for MVP descope
- Delivery plan tasks: `task-nav-*` (phase: phase-navigation)
- PageTabs component: `frontend/src/components/PageTabs.vue`
- Navigation config: `frontend/src/config/navigation.ts`
- Manifest: `frontend/src/manifest.yaml`

## ADRs Referenced

- ADR 009: Frontend Monitor Backend Abstraction (first to use divider-style section headers)
- ADR 010: Integration Tier Classification (established feature flag registry pattern)
