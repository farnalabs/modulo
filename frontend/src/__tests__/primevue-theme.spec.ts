import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import Aura from '@primevue/themes/aura'
import { toNormalizeVariable, toVariables } from '@primeuix/styled'
import {
  PRIMEVUE_TOKEN_BRIDGE,
  PRIMEVUE_V4_BASE_FONT_STYLE_ID,
  PRIMEVUE_V4_DENSITY_PARITY,
  applyPrimeVueTokenBridge,
  installPrimeVueV4BaseFontStyles,
  primeVueTokenName,
  sourceTokenName,
} from '../lib/primevue-theme'

/**
 * Guard test for the PrimeVue token bridge.
 *
 * The token bridge (`src/lib/primevue-theme.ts`) is the single source of truth
 * mapping our semantic CSS vars to PrimeVue's `--p-*` tokens. This test fails
 * CI the instant either side of the mapping breaks:
 *
 *  1. Every mapped `--p-*` target must be a REAL token that the Aura preset
 *     actually defines (a renamed/removed PrimeVue token breaks the bridge).
 *  2. Every referenced source CSS variable must be actually defined in
 *     `src/style.css` (a renamed/removed theme token breaks the bridge).
 *
 * This is the instant-regression catch required by ADR 024 — the token bridge
 * is owned by a named owner and guarded here so regressions are caught at
 * commit/CI time, never at runtime.
 */

/** The set of `--p-*` CSS variable names the Aura preset defines. */
function auraTokenSet(): Set<string> {
  // toVariables returns dotted token keys (e.g. `primary.color`); the actual
  // CSS variable name normalises dots to dashes (`--p-primary-color`).
  const { tokens } = toVariables(
    Aura as unknown as Parameters<typeof toVariables>[0],
    { prefix: 'p' },
  )
  return new Set((tokens || []).map((t) => `--p-${toNormalizeVariable(t)}`))
}

/** The set of CSS custom property names declared in style.css. */
function definedCssVars(): Set<string> {
  // Read the source file from disk (not a bundled `?raw` import, which Vite
  // returns empty here) so the guard checks the real on-disk token set.
  const stylePath = resolve(process.cwd(), 'src/style.css')
  const css = readFileSync(stylePath, 'utf8')
  const vars = new Set<string>()
  const re = /(--[\w-]+)\s*:/g
  let m: RegExpExecArray | null
  while ((m = re.exec(css)) !== null) {
    vars.add(m[1])
  }
  return vars
}

/** Resolve `light-dark(light, dark)` to its dark branch (the app's default). */
function resolveLightDark(value: unknown): unknown {
  if (typeof value !== 'string' || !value.includes('light-dark(')) return value
  const oneLevel = /light-dark\(([^()]*(?:\([^()]*\)[^()]*)*),([^()]*(?:\([^()]*\)[^()]*)*)\)/
  let current = value
  for (let i = 0; i < 8 && current.includes('light-dark('); i++) {
    const next = current.replace(oneLevel, (_m, _light, dark) => dark.trim())
    if (next === current) break
    current = next
  }
  return current
}

/** The v5 Aura token values recorded before the downgrade (FAR-2205). */
function v5BaselineTokens(): Record<string, string> {
  const baselinePath = resolve(process.cwd(), 'tests/visual-baseline/v5/tokens.json')
  return JSON.parse(readFileSync(baselinePath, 'utf8')).tokens as Record<string, string>
}

describe('PrimeVue token bridge guard', () => {
  it('every mapped --p-* target is a real token in the Aura preset', () => {
    const auraTokens = auraTokenSet()
    const missing = PRIMEVUE_TOKEN_BRIDGE.filter(
      (entry) => !auraTokens.has(primeVueTokenName(entry)),
    )
    expect(missing).toEqual([])
  })

  it('every referenced source CSS variable is defined in style.css', () => {
    const defined = definedCssVars()
    const missing = PRIMEVUE_TOKEN_BRIDGE.filter(
      (entry) => !defined.has(sourceTokenName(entry)),
    )
    expect(missing).toEqual([])
  })

  it('every density-parity token is a real token in the Aura preset', () => {
    const auraTokens = auraTokenSet()
    const missing = Object.keys(PRIMEVUE_V4_DENSITY_PARITY).filter((token) => !auraTokens.has(token))
    expect(missing).toEqual([])
  })

  it('every density-parity value matches the recorded PrimeVue 5 baseline', () => {
    // The parity map's whole purpose is "restore the v5 metric". If someone
    // changes a value to something that was never v5's, the map silently stops
    // being a parity map - so pin each entry to the recorded v5 preset value.
    const baseline = v5BaselineTokens()
    const mismatched = Object.entries(PRIMEVUE_V4_DENSITY_PARITY)
      .filter(([token]) => token in baseline)
      .filter(([token, value]) => resolveLightDark(baseline[token]) !== value)
      .map(([token, value]) => ({ token, parity: value, v5: resolveLightDark(baseline[token]) }))
    expect(mismatched).toEqual([])
    // Coverage: the recorded baseline is a union of tokens actually rendered
    // on the captured routes, so a handful of parity tokens (e.g. hover-only
    // sizes) can legitimately be absent. Fail if that stops being a handful.
    const covered = Object.keys(PRIMEVUE_V4_DENSITY_PARITY).filter((token) => token in baseline)
    expect(covered.length).toBeGreaterThanOrEqual(Object.keys(PRIMEVUE_V4_DENSITY_PARITY).length - 5)
  })

  it('applies the bridge as hsl(var(--source)) values on the document root', () => {
    // Guard that the mapping entries are non-trivial (real work exists) and
    // that the source names are unique (no duplicate mappings that would make
    // one source silently override another).
    expect(PRIMEVUE_TOKEN_BRIDGE.length).toBeGreaterThan(10)
    const sources = PRIMEVUE_TOKEN_BRIDGE.map((e) => e.source)
    expect(new Set(sources).size).toBe(sources.length)

    // Actually invoke the bridge on a real element and assert the DOM mapping,
    // so the central deliverable of ADR 024 Decision 4 is proven to do what it
    // claims: every target `--p-*` variable written as `hsl(var(--<source>))`.
    // Several sources intentionally share a target (e.g. card/popover → surface),
    // so later entries override earlier ones for the same `--p-*` variable —
    // mirror that loop to compute the expected DOM, exactly as the bridge runs.
    const root = document.createElement('div')
    applyPrimeVueTokenBridge(root)
    const expected = new Map<string, string>()
    for (const entry of PRIMEVUE_TOKEN_BRIDGE) {
      expected.set(entry.target, `hsl(var(${sourceTokenName(entry)}))`)
    }
    for (const [target, value] of expected) {
      expect(root.style.getPropertyValue(`--p-${target}`)).toBe(value)
    }
    // Spot-check one full mapping for readability of failures.
    expect(root.style.getPropertyValue('--p-primary-color')).toBe('hsl(var(--primary))')

    // Density parity is applied by the same bootstrap call.
    for (const [token, value] of Object.entries(PRIMEVUE_V4_DENSITY_PARITY)) {
      expect(root.style.getPropertyValue(token)).toBe(value)
    }
  })

  it('injects the v4 base-font parity rule exactly once', () => {
    document.getElementById(PRIMEVUE_V4_BASE_FONT_STYLE_ID)?.remove()
    installPrimeVueV4BaseFontStyles(document)
    installPrimeVueV4BaseFontStyles(document)

    const styles = document.querySelectorAll(`#${PRIMEVUE_V4_BASE_FONT_STYLE_ID}`)
    expect(styles.length).toBe(1)

    const css = styles[0].textContent ?? ''
    // Every selector restores a v5 metric PrimeVue 4 cannot carry as a token.
    expect(css).toContain('.p-button:not(.p-button-sm):not(.p-button-lg)')
    expect(css).toContain('.p-inputtext:not(.p-inputtext-sm):not(.p-inputtext-lg)')
    expect(css).toContain('.p-select .p-select-label')
    expect(css).toContain('.p-tabs .p-tab')
    expect(css).toContain('.p-tooltip .p-tooltip-text')
    expect(css).toContain('.p-menu .p-menu-item-label')
    expect(css).toContain('.p-menu .p-menu-item-icon')
    expect(css).toContain('.p-menu .p-menu-item-submenu-icon')
    expect(css).toContain('.p-badge')
    expect(css).toContain('font-size: 0.875rem')
    expect(css).toContain('font-weight: 500')
    expect(css).toContain('font-size: 0.75rem')
    styles[0].remove()
  })
})
