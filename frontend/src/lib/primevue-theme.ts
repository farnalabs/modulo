/**
 * PrimeVue token bridge — single source of truth mapping Modulo's semantic
 * CSS custom properties (hsl triplets defined in style.css) to PrimeVue's
 * `--p-*` design tokens (defined by the Aura preset in @primeuix/themes).
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
 * (guard-tested against `@primeuix/themes/aura`).
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
  for (const entry of PRIMEVUE_TOKEN_BRIDGE) {
    root.style.setProperty(primeVueTokenName(entry), `hsl(var(${sourceTokenName(entry)}))`)
  }
}
