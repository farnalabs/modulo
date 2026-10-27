/**
 * PrimeVue token bridge — single source of truth mapping Modulo's semantic
 * CSS custom properties (hsl triplets defined in style.css) to PrimeVue's
 * `--p-*` design tokens (defined by the Aura preset in @primevue/themes).
 *
 * This is the ONE file maintained when tokens are added/renamed during the
 * PrimeVue migration (FAR-317). It is guarded by
 * `src/__tests__/primevue-theme.spec.ts` which fails CI the moment any mapped
 * `--p-*` token stops resolving in the Aura preset OR any referenced source
 * CSS variable stops being defined in style.css — so a regression on either
 * side of the bridge is caught instantly.
 *
 * All source tokens are hsl triplets (e.g. `169 90% 45%`) consumed via
 * `hsl(var(--primary))`. All target tokens are PrimeVue `--p-*` variables.
 */

/** A single bridge entry: source = our CSS var name, target = PrimeVue `--p-*` var name. */
export interface PrimeVueTokenBridgeEntry {
  /** Our CSS custom property name, WITHOUT the leading `--` (e.g. `primary`). */
  source: string
  /** PrimeVue token, WITHOUT the leading `--p-` (e.g. `primary-color`). */
  target: string
}

/**
 * The semantic → PrimeVue token mapping.
 *
 * Source names must exist in `frontend/src/style.css` (guard-tested).
 * Target names (after the `--p-` prefix) must resolve in the Aura preset
 * (guard-tested against `@primevue/themes/aura`).
 */
export const PRIMEVUE_TOKEN_BRIDGE: PrimeVueTokenBridgeEntry[] = [
  // Primary / accent colour
  { source: 'primary', target: 'primary-color' },
  { source: 'primary-foreground', target: 'primary-contrast-color' },
  { source: 'accent', target: 'highlight-background' },
  { source: 'accent-foreground', target: 'highlight-color' },

  // Surfaces
  { source: 'background', target: 'surface-950' },
  { source: 'card', target: 'surface-900' },
  { source: 'popover', target: 'surface-900' },
  { source: 'muted', target: 'surface-800' },

  // Text
  { source: 'foreground', target: 'text-color' },
  { source: 'card-foreground', target: 'text-color' },
  { source: 'popover-foreground', target: 'text-color' },
  { source: 'muted-foreground', target: 'text-muted-color' },
  { source: 'secondary-foreground', target: 'text-color' },

  // Borders / inputs
  { source: 'border', target: 'content-border-color' },
  { source: 'input', target: 'form-field-border-color' },

  // Destructive
  { source: 'destructive', target: 'form-field-invalid-border-color' },

  // Focus ring
  { source: 'ring', target: 'focus-ring-color' },
  { source: 'focus-ring', target: 'focus-ring-color' },
]

/**
 * PrimeVue 4 density parity (FAR-2205).
 *
 * PrimeVue 5's Aura preset is denser than PrimeVue 4's: the v4 defaults are
 * roughly 8-20% roomier (form-field padding `0.75rem/0.5rem` vs
 * `0.625rem/0.375rem`, badge height `1.5rem` vs `1.25rem`, menu/list item
 * padding `0.5rem 0.75rem` vs `0.25rem 0.625rem`, dialog title `1.25rem` vs
 * `1.125rem`). Left alone, the downgrade would re-space every dialog, form,
 * badge, tab, menu and select in the app.
 *
 * These are the v4 tokens whose default differs from the v5 metrics the app
 * was designed against, pinned back to the v5 value. The values are NOT
 * hand-invented: `primevue-theme.spec.ts` asserts every entry equals the
 * recorded v5 preset value in `tests/visual-baseline/v5/tokens.json`, and that
 * every key really exists in the v4 Aura preset (so a renamed token fails
 * loudly instead of silently doing nothing).
 *
 * Only tokens whose value is a size/weight/gap belong here. Colours are not
 * pinned: v4 pre-resolves the dark branch of PrimeVue 5's `light-dark()`
 * declarations to the same values, which the capture's token diff verifies.
 *
 */
export const PRIMEVUE_V4_DENSITY_PARITY: Record<string, string> = {
  // Shared form-field scale (inputs, selects, buttons read these).
  '--p-form-field-padding-x': '0.625rem',
  '--p-form-field-padding-y': '0.375rem',
  '--p-form-field-sm-font-size': '0.75rem',
  '--p-form-field-sm-padding-x': '0.5rem',
  '--p-form-field-sm-padding-y': '0.25rem',
  '--p-form-field-lg-font-size': '1rem',
  '--p-form-field-lg-padding-x': '0.75rem',
  '--p-form-field-lg-padding-y': '0.5rem',

  '--p-icon-size': '0.875rem',

  // Button
  '--p-button-padding-x': '0.625rem',
  '--p-button-padding-y': '0.375rem',
  '--p-button-icon-only-width': '2.25rem',
  '--p-button-sm-font-size': '0.75rem',
  '--p-button-sm-padding-x': '0.5rem',
  '--p-button-sm-padding-y': '0.25rem',
  '--p-button-sm-icon-only-width': '1.75rem',
  '--p-button-lg-font-size': '1rem',
  '--p-button-lg-padding-x': '0.75rem',
  '--p-button-lg-padding-y': '0.5rem',
  '--p-button-lg-icon-only-width': '2.625rem',

  // InputText
  '--p-inputtext-padding-x': '0.625rem',
  '--p-inputtext-padding-y': '0.375rem',
  '--p-inputtext-sm-font-size': '0.75rem',
  '--p-inputtext-sm-padding-x': '0.5rem',
  '--p-inputtext-sm-padding-y': '0.25rem',
  '--p-inputtext-lg-font-size': '1rem',
  '--p-inputtext-lg-padding-x': '0.75rem',
  '--p-inputtext-lg-padding-y': '0.5rem',

  // Select
  '--p-select-padding-x': '0.625rem',
  '--p-select-padding-y': '0.375rem',
  '--p-select-dropdown-width': '2.25rem',
  '--p-select-sm-font-size': '0.75rem',
  '--p-select-sm-padding-x': '0.5rem',
  '--p-select-sm-padding-y': '0.25rem',
  '--p-select-lg-font-size': '1rem',
  '--p-select-lg-padding-x': '0.75rem',
  '--p-select-lg-padding-y': '0.5rem',
  '--p-select-empty-message-padding': '0.25rem 0.625rem',
  '--p-select-list-header-padding': '0.5rem 0.875rem 0.125rem 0.875rem',
  '--p-select-option-padding': '0.25rem 0.625rem',
  '--p-select-option-group-padding': '0.25rem 0.625rem',
  '--p-select-checkmark-gutter-start': '-0.25rem',
  '--p-select-checkmark-gutter-end': '0.25rem',

  // Badge
  '--p-badge-font-size': '0.625rem',
  '--p-badge-height': '1.25rem',
  '--p-badge-min-width': '1.25rem',
  '--p-badge-padding': '0 0.375rem',
  '--p-badge-sm-font-size': '0.5rem',
  '--p-badge-sm-height': '1.125rem',
  '--p-badge-sm-min-width': '1.125rem',
  '--p-badge-lg-font-size': '0.75rem',
  '--p-badge-lg-height': '1.5rem',
  '--p-badge-lg-min-width': '1.5rem',
  '--p-badge-xl-font-size': '0.875rem',
  '--p-badge-xl-height': '1.75rem',
  '--p-badge-xl-min-width': '1.75rem',

  // Card
  '--p-card-body-padding': '1.125rem',
  '--p-card-title-font-size': '1.125rem',

  // Dialog / overlay
  '--p-dialog-header-padding': '1.125rem',
  '--p-dialog-content-padding': '0 1.125rem 1.125rem 1.125rem',
  '--p-dialog-footer-padding': '0 1.125rem 1.125rem 1.125rem',
  '--p-dialog-footer-gap': '0.375rem',
  '--p-dialog-title-font-size': '1.125rem',
  '--p-overlay-modal-padding': '1.125rem',
  '--p-overlay-popover-padding': '0.625rem',

  // List / navigation / menu (menus and selects render these)
  '--p-list-header-padding': '0.5rem 0.875rem 0.125rem 0.875rem',
  '--p-list-option-padding': '0.25rem 0.625rem',
  '--p-list-option-group-padding': '0.25rem 0.625rem',
  '--p-navigation-item-padding': '0.25rem 0.625rem',
  '--p-navigation-submenu-icon-size': '0.75rem',
  '--p-navigation-submenu-label-padding': '0.25rem 0.625rem',
  '--p-menu-item-padding': '0.25rem 0.625rem',
  '--p-menu-submenu-label-padding': '0.25rem 0.625rem',
  '--p-menu-transition-duration': '0s',

  // Tabs
  '--p-tabs-nav-button-width': '2.25rem',
  '--p-tabs-tab-padding': '0.875rem 1rem',
  '--p-tabs-tabpanel-padding': '0.75rem 1rem 1rem 1rem',
  // v5 removed v4's 1px tab underline (the active indicator is the separate
  // `.p-tablist-active-bar`, rendered by both versions) and the -1px bottom
  // margin that compensated for it.
  '--p-tabs-tab-border-width': '0',
  '--p-tabs-tab-margin': '0',
  '--p-tabs-active-bar-bottom': '0',

  // Tooltip
  '--p-tooltip-padding': '0.375rem 0.625rem',
}

/**
 * Typography parity that a token cannot carry.
 *
 * PrimeVue 4 has no token for these values: it either hardcodes `1rem`, omits
 * the declaration so the component inherits the page's 16px, or (for menu item
 * labels) pins `line-height: 1`. PrimeVue 5 tokenised each of them, and the
 * recorded v5 capture shows the rendered result. Each selector below restores
 * the exact v5 metric; the comment names the v5 token it corresponds to.
 *
 * The `-sm`/`-lg` variants are excluded where they exist: the parity map pins
 * their own tokens (`--p-button-sm-font-size`, `--p-inputtext-lg-font-size`,
 * `--p-select-sm-font-size`), which v4 DOES read.
 *
 * Selectors carry two classes (`.p-menu .p-menu-item-label`) where the v4 rule
 * declares a competing value, so the parity wins regardless of the order in
 * which PrimeVue injects its per-component stylesheet (it is injected on first
 * mount, after this bootstrap stylesheet).
 */
export const PRIMEVUE_V4_BASE_FONT_STYLE_ID = 'primevue-v4-base-font-parity'

const PRIMEVUE_V4_BASE_FONT_CSS = `
/* --p-button-font-size + --p-button-label-font-weight. v4 has no font-size on
   .p-button and puts the weight on .p-button-label, which a label-only button
   (rendered as plain text) does not have. v5 puts the weight on the root for
   every size, so this rule is not size-scoped. */
.p-button {
  font-weight: 500;
}

.p-button:not(.p-button-sm):not(.p-button-lg) {
  font-size: 0.875rem;
}

/* --p-inputtext-font-size. v4 hardcodes 1rem. */
.p-inputtext:not(.p-inputtext-sm):not(.p-inputtext-lg) {
  font-size: 0.875rem;
}

/* --p-select-font-size. v4 hardcodes 1rem on the label (not the root, which
   v5 leaves at the page's 16px). The line-height is explicit because a
   text-xs utility on the select root gives the label a 1.3333 inherited
   line-height in v4, where v5's label always computes at 1.5. */
.p-select .p-select-label {
  font-size: 0.875rem;
  line-height: 1.5;
}

/* --p-select-option-font-size / --p-select-option-group-font-size. v4 leaves
   the dropdown options inheriting 16px; v5 renders them at 14px. */
.p-select-overlay .p-select-option,
.p-select-overlay .p-select-option-group,
.p-select-overlay .p-select-empty-message {
  font-size: 0.875rem;
}

/* --p-tabs-tab-font-size. v4 omits it, so a tab inherits the page's 16px. */
.p-tabs .p-tab {
  font-size: 0.875rem;
}

/* --p-tooltip-font-size. v4 omits it, so tooltip text inherits 16px. */
.p-tooltip .p-tooltip-text {
  font-size: 0.75rem;
}

/* --p-menu-item-label-font-size. v4 inherits 16px and pins line-height: 1;
   v5 uses 14px at the inherited 1.5 line-height. */
.p-menu .p-menu-item-label {
  font-size: 0.875rem;
  line-height: 1.5;
}

/* --p-menu-item-icon-size. v4 leaves the icon at the inherited 16px with no
   fixed box, which both enlarges it and pushes the label to the right; v5
   sizes it to 0.875rem in all three dimensions. */
.p-menu .p-menu-item-icon {
  font-size: 0.875rem;
  width: 0.875rem;
  height: 0.875rem;
}

/* --p-menu-submenu-icon-size (v5: 0.75rem; v4 leaves it inherited). */
.p-menu .p-menu-item-submenu-icon {
  font-size: 0.75rem;
  width: 0.75rem;
  height: 0.75rem;
}

/* v5 promotes the overlay to its own compositing layer. Without this the
   overlay's text rasterises with subpixel antialiasing and the whole menu
   reads as a pixel diff despite identical geometry and glyph metrics
   (measured: 2228 differing pixels, all on glyph edges). */
.p-menu-overlay {
  will-change: transform;
}

/* v5's badge inherits the page line-height (1.5); v4's resolves to normal,
   which shifts the label's baseline inside the fixed-height pill. */
.p-badge {
  line-height: 1.5;
}
`

/**
 * Inject the base-font-size parity rule. Idempotent by element id, so a
 * bootstrap retry or a hot reload cannot accumulate duplicate <style> tags.
 */
export function installPrimeVueV4BaseFontStyles(doc: Document = document): void {
  if (doc.getElementById(PRIMEVUE_V4_BASE_FONT_STYLE_ID)) return
  const style = doc.createElement('style')
  style.id = PRIMEVUE_V4_BASE_FONT_STYLE_ID
  style.textContent = PRIMEVUE_V4_BASE_FONT_CSS
  doc.head.appendChild(style)
}

/** PrimeVue token target (with `--p-` prefix) from a bridge entry. */
export function primeVueTokenName(entry: PrimeVueTokenBridgeEntry): string {
  return `--p-${entry.target}`
}

/** Our CSS var name (with `--` prefix) from a bridge entry. */
export function sourceTokenName(entry: PrimeVueTokenBridgeEntry): string {
  return `--${entry.source}`
}

/**
 * Apply the token bridge onto the document root, writing PrimeVue's `--p-*`
 * variables as `hsl(var(--<source>))` so PrimeVue components consume our theme.
 *
 * `applyPrimeVueTokenBridge()` must run once at app bootstrap (after style.css
 * is loaded) and again after any theme override (e.g. the `html.light` toggle)
 * so the source hsl values are re-read.
 */
export function applyPrimeVueTokenBridge(root: HTMLElement = document.documentElement): void {
  // Density parity first: the semantic colour bridge below intentionally wins
  // for any token it shares with the parity map (it does not, today, but the
  // order makes that guarantee explicit rather than incidental).
  for (const [token, value] of Object.entries(PRIMEVUE_V4_DENSITY_PARITY)) {
    root.style.setProperty(token, value)
  }
  for (const entry of PRIMEVUE_TOKEN_BRIDGE) {
    root.style.setProperty(primeVueTokenName(entry), `hsl(var(${sourceTokenName(entry)}))`)
  }
}
