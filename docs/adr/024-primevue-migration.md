# ADR 024 — PrimeVue migration: Phase 0 foundation (FAR-316)

**Date:** 2026-08-18
**Status:** Accepted

---

## Context

Modulo's frontend UI primitives are built on **shadcn-vue** (`src/components/ui/`),
a copy-paste library whose primitives are thin wrappers over **reka-ui**
(formerly **radix-vue**) styled with Tailwind + CSS custom properties. This
stack has served the product, but it carries a structural cost that grows with
every new surface:

- **Two headless runtimes in the tree.** `reka-ui` v2 is the successor to
  `radix-vue` v1 — the same library lineage. Almost all primitives were migrated
  to `reka-ui` v2, but `OwnershipPicker.vue` is the **only remaining `radix-vue`
  v1 consumer**. Two versions of one lineage, with different APIs and different
  TypeScript shims, must be kept working and maintained side by side.
- **Copy-paste maintenance burden.** shadcn-vue primitives are vendored into
  `src/components/ui/` and upgraded by hand. Every new component (select,
  combobox, dialog, table, date picker, dropdown, menu, ...) must be copied in,
  wired to the theme, and kept in sync with upstream — repeated, mechanical
  effort.
- **Remy `select()` breakage.** The Remy UI-command executor's `select` scopes
  its option query to the trigger element and never opens the popover, so it
  fails for any teleported overlay. This is a symptom of hand-rolled primitive
  wrappers whose overlay structure is not a stable, documented contract.

The goal of this migration is **reduced future development and maintenance
effort and one component system**. PrimeVue ships a full, maintained component
library (buttons, inputs, selects, tables, date pickers, dialogs, menus, ...)
with a single design-token system, styled mode, and a CSS-variable API that maps
cleanly onto Modulo's existing theme.

This reverses the recorded rationale in **PRD §12** ("Component library |
shadcn-vue + Radix Vue ... Chosen because it uses the same `[data-theme]` CSS
custom property token approach as the Modulo theme system - no theming
conflict"). That reasoning was sound at the time, but the long-term cost of
maintaining a copy-paste library across two runtime versions now outweighs the
theming convenience.

## Decision 1 — Migrate the frontend UI primitives to PrimeVue entirely

Modulo adopts **PrimeVue** as its single component system and migrates all UI
primitives off shadcn-vue / reka-ui / radix-vue. `src/components/ui/` shadcn-vue
wrappers are replaced (phase by phase in FAR-317) with equivalent PrimeVue
components. At the end of the migration, `reka-ui` and `radix-vue` are removed
from `package.json` and the headless-runtime duality disappears.

Scope note: the goal is a single component system for **Modulo-authored UI**.
Third-party integrations that happen to use a headless runtime internally (e.g.
`vue-flow`) are out of scope — we do not rewrite what we do not own.

## Decision 2 — reka-ui and radix-vue are the same lineage; OwnershipPicker is the last v1 consumer

`radix-vue` v1 and `reka-ui` v2 are the same library lineage (radix-vue v1 is
the old name, reka-ui v2 the successor). This matters for the migration plan:
migrating the reka-ui v2 primitives also implies retiring the remaining
`radix-vue` v1 consumer, `OwnershipPicker.vue`, in the same sweep so the old
runtime is fully removed rather than leaving a stale v1 dependency behind.

## Decision 3 — Styled mode with the Aura preset; darkModeSelector mapped to our theme system

PrimeVue is configured in **styled mode** with the **Aura** preset
(`@primeuix/themes/aura`) as the base design token set. Styled mode is chosen
over unstyled so we keep PrimeVue's polished component styling out of the box
while still overriding tokens through CSS variables.

**Dark mode mapping.** PrimeVue's `darkModeSelector` selects the **dark** token
set. Modulo's app is **dark by default** (`:root` in `style.css` is dark) and
light is toggled via the `html.light` class. The Aura preset resolves the dark
selector as a custom rule, so we set:

```ts
theme: { preset: Aura, options: { darkModeSelector: ':root:not(.light)' } }
```

`darkModeSelector: ':root:not(.light)'` selects the dark token set whenever the
`.light` class is **absent** (i.e. the app is dark by default) and the light
token set when `html.light` is present. This is the correct inverse of pointing
the selector at `html.light` (which would wrongly treat light mode as the dark
theme). The `[data-theme="agent"]` theme is **not** wired in Phase 0 — that is a
Phase 1 concern; Phase 0 only requires standard light/dark to work.

## Decision 4 — Single source-of-truth token bridge + guard test

PrimeVue components consume `--p-*` tokens; Modulo's theme is defined in
`style.css` as hsl triplets (`--primary`, `--popover`, ...). The bridge between
the two is a single file, `frontend/src/lib/primevue-theme.ts`, that:

- Exports a mapping of Modulo semantic tokens (`--primary`, `--popover`,
  `--foreground`, `--muted-foreground`, `--border`, `--ring`, `--accent`,
  `--focus-ring`, ...) to PrimeVue `--p-*` token names.
- Exports `applyPrimeVueTokenBridge()` which writes those mappings as
  `hsl(var(--<source>))` on the document root, so PrimeVue components consume
  our theme live.

This is the **one** file maintained when tokens change during the migration.

**Guard test.** `frontend/src/__tests__/primevue-theme.spec.ts` asserts, on
every CI run:

1. Every mapped `--p-*` target is a **real** token the Aura preset actually
   defines (imports `@primeuix/themes/aura` and resolves its token set), and
2. Every referenced source CSS variable is actually defined in `style.css`.

The test fails CI the instant a mapped token stops resolving in **either**
direction. This is the instant-regression catch: a renamed PrimeVue token or a
renamed Modulo theme token breaks the bridge at commit/CI time, never silently
at runtime.

## Decision 5 — Token-bridge ownership

The token bridge is owned by a **named owner** (currently the frontend platform
lead) whose responsibility it is to update `primevue-theme.ts` whenever either
side of the bridge changes. The guard test in Decision 4 is the enforcement
mechanism: it is not optional, it runs in CI, and it makes a breaking change to
either token system a build failure the moment it lands. No new `--p-*` token
or renamed Modulo theme token may be introduced without updating the bridge and
its guard test in the same change.

## Decision 6 — Big-bang migration, no feature flag, styling changes acceptable

The migration is a **big-bang** component swap (FAR-317) rather than a
feature-flagged A/B rollout. Rationale: a feature flag would double the UI
surface (two component systems live simultaneously) and defeat the "one
component system" goal. Because PrimeVue's default Aura styling differs from the
current Tailwind/shadcn styling in places, **styling changes are acceptable** —
there are **no pixel-parity gates** against the old implementation. Visual
regressions are reviewed on their merits (does the new component look/behave
correctly and consistently), not measured as a delta against the old primitive.

## Alternatives considered and rejected

**(a) Stay on shadcn-vue + reka-ui.** Rejected — the copy-paste maintenance
burden and the dual-runtime (reka-ui v2 + the lone radix-vue v1 `OwnershipPicker`)
cost persist and grow with every new surface.

**(b) Migrate to another copy-paste headless library (e.g. a different
reka-ui-based kit).** Rejected — this repeats the same maintenance problem with
different vendor wrappers; it does not deliver "one maintained component
system".

**(c) Feature-flagged A/B migration.** Rejected — doubles the UI surface and
defeats the one-system goal (Decision 6).

**(d) Unstyled PrimeVue + full hand-theming.** Rejected — styled mode + the Aura
preset gives us a coherent base style out of the box, with the token bridge for
our overrides. Unstyled would reproduce the "style everything by hand" burden we
are trying to remove.

## Consequences

- One component system going forward; `src/components/ui/` shadcn-vue wrappers
  are retired and eventually `reka-ui` + `radix-vue` are removed from
  `package.json`.
- PrimeVue ships with the Aura preset in styled mode; standard light/dark
  follow the existing `html.light` theme toggle via
  `darkModeSelector: ':root:not(.light)'`. The `[data-theme="agent"]` theme is a
  Phase 1 follow-up.
- The token bridge (`frontend/src/lib/primevue-theme.ts`) is the single point
  of integration between our theme and PrimeVue, owned by a named owner and
  guarded by `primevue-theme.spec.ts`. A token rename on either side is a CI
  failure until the bridge is updated — regressions are caught instantly.
- Styling changes between the old and new component implementations are
  accepted; there are no pixel-parity gates (Decision 6).
- Phase 0 ships no component migrations — it installs/configured PrimeVue,
  adds the token bridge + guard, fixes the Remy `select()` executor (needed
  regardless of library), and extends the jsdom test setup so PrimeVue
  components can mount in unit tests. The actual component migration is FAR-317.
- PRD §12's recorded rationale ("shadcn-vue chosen because it uses the same
  `[data-theme]` CSS custom property token approach ... no theming conflict")
  is **reversed** by this ADR. The PRD decision-log entry is updated to reflect
  the PrimeVue decision.
